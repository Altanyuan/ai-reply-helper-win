# -*- coding: utf-8 -*-
"""微信 4.x 消息采集：WGC 窗口捕获 + 像素锚点定位 + 说话人分类。

为什么单独一条路径（背景，实测结论）：
  微信 4.x 用 Qt 重写后**不向 UIA 暴露聊天内容**：4.2.0.12 唤醒后只给出 2 个
  界面节点（窗口标题 + Qt 内部 Pane），4.1.0.34 给 3 个，都拿不到消息列表，
  3.x 的「控件树直读」在 4.x 上必然拿不到东西。
  旧方案（PIL.ImageGrab 抓屏 + 按 1920x1030 基准等比裁剪）有三个硬伤：
    1) 抓的是**屏幕矩形** —— 窗口被遮挡、最小化、不在前台时，截到的是别的东西；
    2) 提前台靠裸调 SetForegroundWindow，后台线程里会被系统**静默拒绝**（返回 0）；
    3) 裁剪按等比换算，而微信左侧会话列表宽度基本固定（不随窗口缩放），
       窗口尺寸一变裁剪就偏：窄窗口混入联系人名、高窗口切掉最新几条消息。
  这里改成与 jev-chat-windows 相同的采集方式：
    · WGC（Windows Graphics Capture）抓**窗口本身** —— 被遮挡也能截、不需要抢前台、
      抢焦点（用户正在打字时不会被打断）；
    · 帧从 BGRA 缓冲区直接转 numpy，全程内存、不落盘；
    · 消息区靠**像素锚点**动态定位（面板底色 + 分隔线），不写死坐标、深浅主题通用；
    · 说话人靠**气泡底色 / 文字对比度**分类，图片里的字与灰字过滤掉；
    · 连续取帧直到**画面停稳**（滚动动画、新消息滑入的半截气泡全跳过）。

与 3.x 的关系（重要，回归红线）：
  本模块只在 UIA 直读失败后才会被 `capturer.read_wechat_recent` 调用。
  微信 3.x 在第一步就命中并返回，**永远不会走到这里** —— 3.x 行为零变化。

OCR 由 `ocr_reader` 统一处理：
  RapidOCR 作为主引擎，WinRT OCR 在主引擎不可用、识别无文本框或无法形成聊天消息时回退；
  两个后端都返回文字框坐标，供本模块按气泡像素分类说话人。
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time

import numpy as np

log = logging.getLogger("polish")

# 显式声明 argtypes/restype：64 位下 ctypes 默认按 c_int 传参，而 HWND 是 8 字节指针，
# 不声明就可能被截断成垃圾值（paste_back.py 里踩过同一个坑）。
_u32 = ctypes.windll.user32
_u32.IsIconic.argtypes = [ctypes.c_void_p]
_u32.IsIconic.restype = ctypes.c_int
_u32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
_u32.ShowWindow.restype = ctypes.c_int
_u32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_uint]
_u32.SetWindowPos.restype = ctypes.c_int

# ---- WGC 取帧参数 ----
SETTLE = 0.25          # 画面连续稳定这么久才算「停稳」
MAX_WAIT = 1.2         # 死活停不稳（动图表情）也最多等到这里
GRAB_TIMEOUT = 3.0     # 整个抓取（含启动 WGC）的上限秒数

# ---- 消息区定位参数（移植自 jev-chat-windows 的 chat_area，阈值都是实测值）----
_SAMPLE = 8            # 算底色的抽样步长：整帧太宽，隔点采样够用且快
_BG_TOL = 6            # 与底色差异之和 <= 它 → 算「底色像素」
_COL_RATIO = 0.3       # 一列里底色占比超过它 → 这一列在面板内（第一档阈值）
_ROW_RATIO = 0.9       # 一行里底色占比超过它 → 这一行是纯面板
_SEP_STD = 4           # 整行标准差 < 它 → 单色行（横向分隔线候选）
_SEP_ROW = 0.1         # 分隔线所在行的底色占比要低于它
_MIN_W, _MIN_H = 100, 40

# ---- 定位鲁棒性参数（2026-11-09 「收下右侧空白带」事故后加，见 _longest_span）----
_COL_RATIO_LADDER = (_COL_RATIO, 0.15, 0.08)  # 阈值逐级放宽：内容越密，面板列的底色占比越低
_GAP_RATIO = 0.06      # 连续段之间允许的空洞（占帧宽）；小于它就并成一段
_MIN_W_RATIO = 0.30    # 消息区宽度下限（占帧宽）：真实面板实测占 60%~85%
_MIN_H_RATIO = 0.25    # 消息区高度下限（占帧高）
_WIDE_LEFT_RATIO = 0.34  # 4.x 布局：左图标栏 + 会话列表约占帧宽 34%，重试时从这里起裁
_HEADER_RATIO = 0.085  # 标题栏（含会话名）占客户区高度的比例（1920x1030 实测 86px）
_HEADER_MIN, _HEADER_MAX = 40, 140
_TITLE_OCR_MIN = 64   # 小窗口仍需覆盖微信标题文字的完整行高

# 锚点定位失败时的兜底裁剪比例（沿用旧版本的实测标定，聊胜于无）
_FALLBACK = (0.197, 0.083, 0.608, 0.474)   # left, top, right, bottom


def wgc_available() -> bool:
    """WGC 采集是否可用。依赖缺失（没装 windows-capture）时返回 False，
    调用方会回落到原有的截屏 OCR 路径，不影响主流程。"""
    try:
        import windows_capture  # noqa: F401
        return True
    except Exception as e:
        log.warning(
            "WGC 不可用（%s: %s），4.x 采集将回落到带说话人分类的截屏 OCR；"
            "源码运行请执行 python -m pip install -r requirements.txt，"
            "打包版需在安装依赖后重新构建",
            type(e).__name__, e,
        )
        return False


def unminimize(hwnd):
    """最小化的窗口 DWM 不渲染，WGC 同样拿不到画面。

    无激活还原（SW_SHOWNOACTIVATE）后压到所有窗口最底下：看起来仍像收起来了，
    但 DWM 继续画、WGC 也继续能截；全程不抢焦点、不改大小位置。返回是否动了手。

    还原是异步的（ShowWindow 只是把请求交给窗口线程），所以这里等它真正落地再返回 ——
    不等就去抓帧只会拿到黑帧（实测：最小化状态下直接抓帧必失败）。
    """
    try:
        if not _u32.IsIconic(hwnd):
            return False
        _u32.ShowWindow(hwnd, 4)                      # SW_SHOWNOACTIVATE
        for _ in range(20):                           # 最多等 0.6s
            if not _u32.IsIconic(hwnd):
                break
            time.sleep(0.03)
        _u32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x13)  # HWND_BOTTOM|NOSIZE|NOMOVE|NOACTIVATE
        time.sleep(0.12)                              # 再给 DWM 一点时间开始出帧
        return True
    except Exception:
        return False


# ==================== 消息区定位（像素锚点） ====================

def _bg_candidates(full):
    """面板底色的候选（去重，按先验可信度排序）。

    1) **窗口上半部的右半边**众数 —— 这一段在微信里必然是消息区（标题栏之下、
       输入框之上）。必须单列出来（实测踩坑）：底部输入框的底色往往比消息区**更白**，
       若把整个右半边一起统计，输入框会反客为主变成众数，整块定位就全错了。
    2) 整个右半边众数 —— 兜底：布局特殊（输入框很矮 / 上半部内容杂乱）时用。
    """
    H, W = full.shape[:2]
    out = []
    for region in (full[H // 12:H // 2:_SAMPLE, W // 2::_SAMPLE],
                   full[::_SAMPLE, W // 2::_SAMPLE]):
        if region.size == 0:
            continue
        vals, cnt = np.unique(region.reshape(-1, 3), axis=0, return_counts=True)
        cand = vals[cnt.argmax()]
        if not any(np.array_equal(cand, o) for o in out):
            out.append(cand)
    return out


def _runs_of(mask):
    """把布尔序列切成连续 True 段，返回 [(start, end), ...]（end 不含）。"""
    runs = []
    start = None
    for i, flag in enumerate(mask):
        if flag:
            if start is None:
                start = i
        elif start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(mask)))
    return runs


def _longest_span(mask, gap=0):
    """返回最长的 True 段 [start, end)：相隔不超过 gap 的段先合并再取最长。

    为什么不能取「第一个 / 最后一个 True」（实测踩坑）：
      微信 4.x 最左边有一条 63px 图标竖栏，底色与聊天面板**完全相同**
      （237,237,237），中间隔着白色的会话列表。按「第一个超过阈值的列」取左边界
      会把这条竖栏当成面板起点（x0≈1），右边界再取到窗口最右 —— 面板横跨整个
      窗口，接着按整行统计「纯面板行」永远到不了 90%，定位失败并退化到比例裁剪，
      而那套比例是按 1920x1030 标定的，别的尺寸上会把会话列表文字裁进消息区。

    为什么还要允许 gap 合并（2026-11-09 另一台机器实测）：
      聊天内容密集时（长文本、满屏气泡、大图），中间若干列的底色占比会掉到阈值
      以下，把面板切成碎片。只取「最长连续段」会掉进右侧那条 165px 的空白边
      （占帧宽 15%），OCR 自然一个文本框都读不到。会话列表与图标栏之间的空洞
      通常是 200~300px，远大于 gap，不会被误合并。
    """
    if len(mask) == 0:
        return 0, 0
    merged = []
    for start, end in _runs_of(mask):
        if merged and start - merged[-1][1] <= gap:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    best = (0, 0)
    for span in merged:
        if span[1] - span[0] > best[1] - best[0]:
            best = span
    return best


def _area_with_bg(full, bg):
    """给定面板底色，用像素锚点算出消息区；算不出返回 None。"""
    H, W = full.shape[:2]
    # 底色掩码 → 列/行占比
    isbg = np.abs(full.astype(np.int16) - bg).sum(-1) <= _BG_TOL
    col = isbg[H // 4: H * 3 // 4].mean(0)
    gap = max(8, int(W * _GAP_RATIO))
    min_w = max(_MIN_W, int(W * _MIN_W_RATIO))
    min_h = max(_MIN_H, int(H * _MIN_H_RATIO))

    # 左右边界：阈值逐级放宽，取第一档「宽度够得像面板」的结果。
    # 会话列表是另一种底色（占比≈0），放宽阈值也不会被并进来。
    x0 = x1 = 0
    for ratio in _COL_RATIO_LADDER:
        start, end = _longest_span(col > ratio, gap)
        if end - start >= min_w:
            x0, x1 = int(start), int(end)
            break
    if x1 - x0 < 1:
        log.info("消息区定位失败：逐列底色占比不足（面板被内容切断，或底色候选不对）")
        return None

    row = isbg[:, x0:x1].mean(1)
    if not (row > _ROW_RATIO).any():
        # 一行都没到 90% → 这块区域根本不是面板（典型的「右侧空白带」误定位）
        log.info(f"消息区定位失败：候选区域 {x1 - x0}x{H} 里没有纯面板行")
        return None
    y0 = int(np.argmax(row > _ROW_RATIO))
    y1 = H - int(np.argmax(row[::-1] > _ROW_RATIO))
    if y1 - y0 < min_h:
        return None

    # 横向分隔线 → 输入框顶 / 消息区顶
    band = full[y0:y1, x0:x1].astype(np.int16)
    seps = y0 + np.where((band.std(axis=(1, 2)) < _SEP_STD) & (row[y0:y1] < _SEP_ROW))[0]
    # 相邻几行算同一根线，只留第一行
    seps = [int(s) for i, s in enumerate(seps) if i == 0 or s - seps[i - 1] > 3]

    below = [s for s in seps if s > y0 + 0.45 * (y1 - y0)]
    y_in = below[0] if below else y1
    header_h = min(_HEADER_MAX, max(_HEADER_MIN, int(H * _HEADER_RATIO)))
    above = [s for s in seps if y0 + header_h < s < y_in - 50]
    y_top = above[-1] if above else y0 + header_h

    if y_in - y_top < min_h:
        return None
    return x0, y_top, x1, y_in, bg


def chat_area(full):
    """从整帧里定位「消息区」，返回 (x0, y_top, x1, y_in, 面板底色) 或 None。

    全像素锚点，不写死坐标、不做基准等比换算，所以窗口任意缩放、深浅主题都成立：
      · 面板底色 = 候选色里**算出来的消息区最靠上**的那个（见 _bg_candidates）
      · 左右边界 = 最长的一段「底色占比 > 阈值」的列，允许中间有 ≤6% 帧宽的空洞
        （见 _longest_span；左侧会话列表是另一种底色，占比为 0，天然被排除）
      · 消息区上下界 = 「底色占比 > 90%」的纯面板行
      · 横向分隔线 = 整行单色（标准差 < 4）且几乎不含底色；
        输入框顶 = 面板 45% 高度以下的第一根，再往上一根是公告条下沿，
        没有就用标题栏高度兜底
    定位结果还要过形状校验（宽度 ≥ 帧宽 30%、高度 ≥ 帧高 25%）：真实面板实测占
    60%~85%，过不了就是拿错了区域（例如聊天内容把面板切断后剩下的右侧空白带）。
    认不出（窗口太小 / 布局还没铺好 / 形状不合理）返回 None，由调用方走兜底裁剪。
    """
    H, W = full.shape[:2]
    if H < 120 or W < 200:
        return None
    best = None
    for bg in _bg_candidates(full):
        area = _area_with_bg(full, bg)
        if area is None:
            continue
        # 消息区必然比输入框区更靠上：谁算出来的 y_top 更小，谁才抓对了面板。
        # （用「面积最大」当判据会选错 —— 底部输入框区往往又高又干净。）
        if best is None or area[1] < best[1]:
            best = area
    return best


def recognize_chat_header(full, area):
    """实验性识别微信聊天顶部标题区域，返回 OCR 文本框和引擎信息。

    只读取整帧顶部中央聊天面板，不包含消息区；暂不用于提示词切换。
    标题栏范围按当前 WGC 帧高度估算，并限制在现有实测边界内。
    """
    if full is None or area is None:
        return [], "无窗口帧或聊天区域"

    x0, y_top, x1, _y_in, _pane_bg = area
    height, width = full.shape[:2]
    header_h = min(_HEADER_MAX, max(_TITLE_OCR_MIN, int(height * _HEADER_RATIO)))
    x0 = max(0, min(int(x0), width))
    x1 = max(x0, min(int(x1), width))
    # WGC captures the non-client caption on windowed WeChat. In that mode
    # the title row sits lower than the height estimate, so prefer the
    # detected chat-area top (the bottom edge of the header), capped to avoid
    # pulling message text into this OCR pass.
    header_bottom = max(header_h, min(int(y_top), _HEADER_MAX))
    y1 = max(0, min(header_bottom, height))
    if x1 - x0 < 40 or y1 < 12:
        return [], f"标题区域过小：{x1 - x0}x{y1}"

    header = full[:y1, x0:x1]
    try:
        import ocr_reader
        from PIL import Image

        boxes, engine_info = ocr_reader.recognize_boxes_scaled(Image.fromarray(header))
        return boxes, f"{engine_info}；标题区域 {header.shape[1]}x{header.shape[0]}"
    except Exception as e:
        log.warning("聊天标题 OCR 失败: %s: %s", type(e).__name__, e)
        return [], f"识别异常：{type(e).__name__}"


def select_chat_title(boxes, header_width):
    """从标题栏 OCR 框中取左侧聊天名称，排除右侧工具按钮文字。"""
    left_limit = max(100, int(header_width * 0.25))
    candidates = [
        (str(text).strip(), rect)
        for text, rect in (boxes or [])
        if str(text).strip() and rect[0] <= left_limit
    ]
    if len(candidates) != 1:
        return None
    return candidates[0][0]


def fallback_area(shape):
    """锚点定位失败时的兜底：按旧版本实测标定的比例切一块出来。

    比例取自 1920x1030 客户区的逐像素标注（左 378 / 上 86 / 右 1168 / 下 488），
    只在极端尺寸或布局异常时用得上；宁可裁得粗糙，也好过整条链拿不到上下文。
    """
    H, W = shape[:2]
    l, t, r, b = _FALLBACK
    x0, x1 = int(W * l), int(W * r)
    y_top, y_in = int(H * t), int(H * b)
    if x1 - x0 < _MIN_W or y_in - y_top < _MIN_H:
        return None
    return x0, y_top, x1, y_in, None


def widen_area(shape, area):
    """消息区被判为「偏窄」时的第二选择：按 4.x 布局估算一块更宽的区域。

    只在区间宽度 < 帧宽 55% 时启用（真实面板实测占 60%~85%）。走到这里通常意味着
    拿到的是一条空白带（例如聊天内容把面板切断后剩下的右侧 165px），此时把左边界
    放宽到「图标栏 + 会话列表」之后（约 34% 帧宽）再试一次，比直接放弃有用：
    会话列表里的联系人名是灰字小字，`parse_messages` 按气泡像素分类时会丢掉，
    不会污染上下文。返回 (x0, y_top, x1, y_in, 底色) 或 None。
    """
    if not area:
        return None
    H, W = shape[:2]
    x0, y_top, x1, y_in, bg = area
    if x1 - x0 >= W * 0.55:
        return None
    nx0 = max(0, int(W * _WIDE_LEFT_RATIO))
    nx1 = min(W, max(x1, int(W * 0.99)))
    if nx1 - nx0 < max(_MIN_W, int(W * _MIN_W_RATIO)):
        return None
    if nx0 >= x1 or nx1 <= x0:
        return None
    return nx0, y_top, nx1, y_in, bg


_LAST_DUMP = 0.0


def dump_debug_frame(full, tag):
    """把「定位失败 / 识别不到消息」的现场帧存一份，供事后复盘（限频 10s）。

    没有现场帧时，只能靠日志里的坐标猜画面内容（2026-11-09 那次 165x677 的空白带
    就是猜出来的）。存一张图，下次直接看就行。失败不影响主流程。
    """
    global _LAST_DUMP
    if full is None:
        return
    now = time.time()
    if now - _LAST_DUMP < 10:
        return
    _LAST_DUMP = now
    try:
        import paths
        from PIL import Image

        out = paths.data_dir() / "debug_frame_last.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(full).save(out)
        log.info(f"已保存可疑帧供排查（{tag}）: {out}")
    except Exception as e:
        log.warning(f"保存可疑帧失败（不影响主流程）: {type(e).__name__}: {e}")


# ==================== 说话人分类 ====================

def who_said(chat, rect):
    """按 OCR 框内的像素判断这句话是谁说的，返回 (谁, 底色, 墨高)。

    先看底色平不平：框内众数颜色占比 < 45% 就是**图片里的字**（头像、照片、表情包）
    → (None, bg, 0)，上层丢掉。
    绿底（G 明显高于 R/B）= 我（微信发出的气泡是绿的）；
    文字与底色对比度 >= 150 = 对方；
    其余（对比度 80~95 的灰字：引用块、时间戳、系统提示、链接卡片）= "gray"。
    实测：气泡正文对比度 178~208，我方绿泡 142~150，灰字 <= 93 —— 深浅主题都吃这套。
    墨高 = 框内最长一段连续有字的行数：OCR 框对小字有固定 padding、还会蹭到上下行，
    不能拿框高直接比大小。
    """
    x0, y0, x1, y1 = rect
    reg = chat[max(0, y0):y1, max(0, x0):x1].astype(np.int16)
    if reg.size == 0:
        return None, None, 0
    vals, cnt = np.unique(reg.reshape(-1, 3), axis=0, return_counts=True)
    bg = vals[cnt.argmax()]
    if cnt.max() / float(reg.shape[0] * reg.shape[1]) < 0.45:
        return None, bg, 0

    gray = reg @ np.array([0.299, 0.587, 0.114])
    diff = np.abs(gray - float(bg @ np.array([0.299, 0.587, 0.114])))
    ink_h = best = 0
    for r in (diff > 60).any(axis=1):
        best = best + 1 if r else 0
        ink_h = max(ink_h, best)

    b, g, r_ = int(bg[0]), int(bg[1]), int(bg[2])
    if g > r_ + 40 and g > b + 40:
        return "me", bg, ink_h
    return ("her" if float(diff.max()) >= 150 else "gray"), bg, ink_h


def parse_messages(chat, boxes, pane_bg):
    """OCR 结果 → [(who, name, text, y)]，同一气泡的多行已合并。

    who ∈ me/her；name 是群聊里的发言人（单聊为 None）。
    boxes: [(text, (x0, y0, x1, y1))]，坐标已相对 chat 裁剪图。
    """
    rows = []
    name = None           # 群聊：气泡上方那行灰色发言人名，带给它下面的气泡
    for text, rect in sorted(boxes, key=lambda b: b[1][1]):
        kind, bg, ink_h = who_said(chat, rect)
        if kind == "gray":
            # 靠左、短、不带冒号、且印在面板底色上的灰字 = 发言人名
            on_pane = bg is not None and pane_bg is not None and \
                abs(int(bg[0]) - int(pane_bg[0])) + abs(int(bg[1]) - int(pane_bg[1])) + \
                abs(int(bg[2]) - int(pane_bg[2])) <= 6
            if on_pane and rect[0] < 0.25 * chat.shape[1] and len(text) <= 16 \
                    and not any(c in text for c in ":："):
                name = text
            continue
        if kind is None:
            continue      # 图片（头像/表情包/照片）里的字
        rows.append({"who": kind, "name": name if kind == "her" else None,
                     "text": text, "top": rect[1], "bottom": rect[3], "h": ink_h})

    # 正常气泡字高：头一帧拿不到上一帧的参照，这里用本帧中位数两遍过滤
    heights = [r["h"] for r in rows if r["h"] > 0]
    lh = float(np.median(heights)) if len(heights) >= 3 else 0.0

    merged = []
    for r in rows:
        if kind_small(r, lh):
            continue      # 比正常气泡小得多的字 = 图片里的字
        if merged and merged[-1]["who"] == r["who"] and merged[-1]["name"] == r["name"] \
                and r["top"] - merged[-1]["bottom"] < 0.6 * (lh or r["h"]):
            merged[-1]["text"] += r["text"]      # 同一气泡的下一行
            merged[-1]["bottom"] = r["bottom"]
        else:
            merged.append(dict(r))
    return [(m["who"], m["name"], m["text"]) for m in merged]


def kind_small(row, lh):
    """字高明显小于正常气泡 → 判为图片里的字（截图/表情包上的说明文字）。"""
    return bool(lh and row["h"] and row["h"] < 0.6 * lh)


def format_context(msgs):
    """[(who, name, text)] → 喂给模型的上下文文本。

    对方的话带发言人名（群聊），自己的话标「我」—— 这样模型知道哪句是谁说的，
    比自己项目 3.x 那条纯文本路径更有信息量（3.x 的输出格式保持不变）。
    """
    lines = []
    for who, name, text in msgs:
        text = (text or "").strip()
        if not text:
            continue
        if who == "me":
            lines.append(f"我：{text}")
        elif name:
            lines.append(f"对方（{name}）：{text}")
        else:
            lines.append(f"对方：{text}")
    return "\n".join(lines)


# ==================== WGC 抓帧 ====================

class _Grabber:
    """一次性抓帧器：边收帧边比消息区像素，连续稳定 SETTLE 秒就把帧交出去。

    只比消息区、不比头部 —— 公告条会滚动，带上它就永远停不稳（jev 的经验）。
    超时兜底把手上最新的一帧交出去：有总比没有强。
    """

    def __init__(self, hwnd, settle=SETTLE, max_wait=MAX_WAIT):
        self.hwnd = hwnd
        self.settle = settle
        self.max_wait = max_wait
        self.area = None            # 消息区（整帧坐标）
        self._lock = threading.Lock()
        self._ctl = None
        self._shape = None
        self._last_chat = None      # 上一帧的消息区像素
        self._pending = None        # 待交出的整帧
        self._latest = None         # 最新一帧（超时兜底用）
        self._t_change = 0.0
        self._t0 = 0.0
        self._frames = 0

    def start(self):
        from windows_capture import WindowsCapture
        # cursor_capture / draw_border 显式传 None：走系统默认、不去切换这两个开关。
        # （Win10 1904 及更早没有这些属性，包装层的默认值会直接抛 CursorConfigUnsupported）
        cap = WindowsCapture(cursor_capture=None, draw_border=None,
                             window_hwnd=self.hwnd)
        # 两个回调都得注册：漏掉 on_closed 时 start 会直接报
        # "on_closed Event Handler Is Not Set"（2.x 的硬性要求）。
        cap.event(self.on_frame_arrived)
        cap.event(self.on_closed)
        self._ctl = cap.start_free_threaded()

    def on_closed(self):
        """窗口关闭 / 采集会话结束：这里不做事，stop() 由调用方负责。"""
        pass

    def on_frame_arrived(self, frame, control):
        try:
            # frame_buffer 是 BGRA 零拷贝视图，回调返回后缓冲区就没了，必须立刻拷贝
            full = np.ascontiguousarray(frame.frame_buffer[:, :, :3][:, :, ::-1])
        except Exception:
            return
        if full.size == 0 or int(full.max()) == 0:
            return                  # 黑帧（窗口还没画出来）
        with self._lock:
            now = time.perf_counter()
            self._frames += 1
            if self.area is None or full.shape != self._shape:
                self._shape = full.shape
                self.area = chat_area(full)
            self._latest = full
            if self.area is None:
                # 消息区认不出也照样交帧（超时兜底），只是没法做停稳 diff
                if self._pending is None:
                    self._pending, self._t0 = full, now
                return
            x0, y_top, x1, y_in = self.area[:4]
            chat = full[y_top:y_in, x0:x1]
            same = (self._last_chat is not None
                    and chat.shape == self._last_chat.shape
                    and np.array_equal(chat, self._last_chat))
            self._last_chat = chat
            if not same:
                self._t_change = now
                if self._pending is None:
                    self._t0 = now
                self._pending = full
            elif self._pending is None:
                self._pending, self._t0 = full, now

    def settled(self, deadline):
        """等到画面停稳，返回 (整帧, 消息区)；超时则交出手上最新的一帧。"""
        while True:
            with self._lock:
                now = time.perf_counter()
                if self._pending is not None and (
                        now - self._t_change >= self.settle
                        or now - self._t0 >= self.max_wait):
                    return self._pending, self.area
                if now >= deadline:
                    return self._latest, self.area
            time.sleep(0.04)

    def stop(self):
        ctl, self._ctl = self._ctl, None
        if ctl is not None:
            try:
                ctl.stop()
            except Exception:
                pass


def grab_frame(hwnd, timeout=GRAB_TIMEOUT):
    """抓一张「停稳」的整帧，返回 (full, area)；失败返回 (None, None)。

    抓之前先做最小化还原：Windows 不渲染最小化的窗口，任何截图法都拿不到画面
    （还原成「无激活 + 压到最底层」，不抢焦点、不打断用户正在做的事）。
    """
    unminimize(hwnd)
    g = _Grabber(hwnd)
    try:
        g.start()
    except Exception as e:
        log.warning(f"WGC 启动失败，回落到截屏 OCR: {type(e).__name__}: {e}")
        return None, None
    try:
        full, area = g.settled(time.perf_counter() + timeout)
    except Exception as e:
        log.warning(f"WGC 抓帧失败: {type(e).__name__}: {e}")
        return None, None
    finally:
        g.stop()
    if full is None:
        log.info("WGC 没拿到帧（窗口不可见？被最小化？）")
        return None, None
    if area is None:
        area = fallback_area(full.shape)
    return full, area


def _stopped(cancel):
    """上层已放弃本次抓取（超时）→ True。cancel 为 None 时永远 False。"""
    try:
        return bool(cancel is not None and cancel.is_set())
    except Exception:
        return False


def _recognize_crop(crop, pane_bg, ocr_reader):
    """对一块消息区图像做 OCR + 说话人分类，返回 (msgs, boxes, pass_info)。

    RapidOCR 出框但形不成聊天消息时回退 WinRT（两个引擎的框坐标口径一致）。
    识别不到时返回空列表，但**保留** boxes/pass_info —— 排查「上下文为空」时，
    「识别到几个框、走的哪个引擎」是唯一线索。
    """
    try:
        from PIL import Image
        boxes, pass_info = ocr_reader.recognize_boxes_scaled(Image.fromarray(crop))
    except Exception as e:
        log.warning(f"消息区 OCR 失败: {type(e).__name__}: {e}")
        return [], [], "OCR 异常"
    if not boxes:
        log.info("WGC 采集：消息区没识别到文字")
        return [], [], pass_info
    msgs = parse_messages(crop, boxes, pane_bg)
    if not msgs and pass_info.startswith("RapidOCR ") and not pass_info.startswith("RapidOCR 0,"):
        log.info("WGC 采集：RapidOCR 文本框未形成聊天消息，回退到 WinRT OCR")
        boxes, fallback_info = ocr_reader.recognize_boxes_winrt_scaled(crop)
        pass_info = f"RapidOCR 无消息；{fallback_info}"
        if boxes:
            msgs = parse_messages(crop, boxes, pane_bg)
    return msgs, boxes, pass_info


def read_recent_with_title(hwnd, n=None, timeout=GRAB_TIMEOUT,
                           include_messages=True, cancel=None):
    """同一张 WGC 帧中识别聊天标题与可见消息。

    返回 ``(messages, title_or_none)``。标题/聊天图像仅在内存处理，标题文字
    不写日志。include_messages=False 时仍可单独读取标题。

    cancel: 可选的 threading.Event。被置位说明上层已经放弃本次抓取（超时报错），
    这里在 OCR 前后各查一次并尽快返回 —— 否则后台线程会继续跑十几秒 OCR，
    把 CPU 抢给下一次热键（实测两台机器都出现过「越点越慢」）。
    """
    if not hwnd:
        return [], None

    full, area = grab_frame(hwnd, timeout=timeout)
    if full is None or area is None:
        dump_debug_frame(full, "未定位到消息区")
        return [], None
    if _stopped(cancel):
        return [], None

    header_boxes, _header_info = recognize_chat_header(full, area)
    title = select_chat_title(header_boxes, area[2] - area[0])
    if not include_messages:
        return [], title

    import ocr_reader
    if not ocr_reader.ocr_available():
        return [], title

    x0, y_top, x1, y_in, pane_bg = area
    crop = full[y_top:y_in, x0:x1]
    if crop.size == 0:
        return [], title
    log.info(f"WGC 采集：整帧 {full.shape[1]}x{full.shape[0]} → 消息区 "
             f"({x0},{y_top})-({x1},{y_in}) 尺寸 {x1 - x0}x{y_in - y_top}")

    msgs, boxes, pass_info = _recognize_crop(crop, pane_bg, ocr_reader)
    if _stopped(cancel):
        return [], title

    # 一条消息都没识别出来、且这块区域明显偏窄 → 换一块更宽的区域再试一次。
    # （定位拿成右侧空白带时，窄区域里永远只有 0~1 个框，重试比直接放弃有用。）
    if not msgs:
        wide = widen_area(full.shape, area)
        if wide is not None:
            wx0, wy_top, wx1, wy_in, _wbg = wide
            log.info(
                f"WGC 采集：消息区偏窄（占帧宽 {(x1 - x0) * 100.0 / full.shape[1]:.0f}%），"
                f"改用宽区域 ({wx0},{wy_top})-({wx1},{wy_in}) 重试一次"
            )
            wcrop = full[wy_top:wy_in, wx0:wx1]
            if wcrop.size:
                msgs, wboxes, wpass = _recognize_crop(wcrop, pane_bg, ocr_reader)
                if msgs:
                    crop, boxes, pass_info = wcrop, wboxes, wpass
    if not msgs:
        log.info(f"WGC 采集：识别到 {len(boxes)} 个文本框，但没有可用的消息")
        dump_debug_frame(full, f"消息区 {x1 - x0}x{y_in - y_top} 无可用消息")
        return [], title
    if n is None:
        picked = msgs
        selection = f"全部传入 {len(picked)} 条"
    else:
        count = int(n)
        picked = msgs[-count:] if count > 0 else []
        selection = f"取最近 {len(picked)} 条"
    log.info(
        f"WGC 采集：{pass_info}；识别 {len(boxes)} 框 → {len(msgs)} 条消息，"
        f"{selection}"
    )
    return picked, title


def read_recent(hwnd, n=None, timeout=GRAB_TIMEOUT, cancel=None):
    """兼容旧调用方：返回 WGC 识别到的可见消息列表。"""
    messages, _title = read_recent_with_title(
        hwnd, n=n, timeout=timeout, cancel=cancel,
    )
    return messages


if __name__ == "__main__":
    # 离线自测：不碰窗口、不联网，用合成图验证锚点定位与分类。
    import numpy as np

    def _text_row(img, y, x0, x1, ink=(20, 20, 20), char=14, mark=7, h=6):
        """画一行"像文字"的短墨块（模拟汉字墨迹，占框面积约 20%）。

        不能直接涂一条黑带 —— 那会让框内众数变成黑色，分类逻辑就不是在测真实情况了。
        """
        for x in range(x0, x1, char):
            img[y:y + h, x:x + mark] = ink

    def _synthetic(pane_bg=(245, 245, 245), green=(149, 236, 105)):
        """造一张「左会话列表 + 中间消息区 + 底部输入框」的假帧。"""
        H, W = 1030, 1920
        img = np.zeros((H, W, 3), np.uint8)
        img[:, :378] = (235, 235, 235)              # 左侧会话列表（另一种底色）
        # 消息区与右侧留白同色 —— 真实微信就是这样（留白只是没有内容），
        # 算法会把它们一起当成面板，多截一段空白无妨。
        img[:, 378:] = pane_bg
        img[488:, 378:] = (255, 255, 255)           # 输入框区
        img[487, 378:1168] = (200, 200, 200)        # 分隔线（整行单色、非底色）
        img[86, 378:1168] = (200, 200, 200)         # 标题栏下沿
        # 对方气泡：白底 + 两行墨迹（行距很紧，属于同一个气泡）
        img[150:230, 430:900] = (255, 255, 255)
        _text_row(img, 168, 450, 880)
        _text_row(img, 178, 450, 800)
        # 我方气泡：绿底 + 一行墨迹
        img[260:330, 700:1150] = green
        _text_row(img, 278, 720, 1130, ink=(30, 30, 30))
        # 面板上的一行灰字（时间戳 / 系统提示那种）
        _text_row(img, 400, 430, 600, ink=(150, 150, 150), mark=5, h=4)
        return img, pane_bg

    img, pane = _synthetic()
    area = chat_area(img)
    assert area is not None, "锚点定位失败"
    x0, y_top, x1, y_in, bg = area
    assert abs(x0 - 378) <= 12, f"左边界 {x0} 应接近 378"
    assert x1 >= 1160, f"右边界 {x1} 应覆盖到聊天区（含右侧同色留白）"
    assert abs(y_in - 487) <= 12, f"输入框顶 {y_in} 应接近分隔线 487"
    assert 80 <= y_top <= 100, f"消息区顶 {y_top} 应在标题栏之下"
    assert tuple(int(v) for v in bg) == pane, f"底色 {bg} 应为面板底色"

    # 兜底裁剪：尺寸异常时也要能切出一块合理的区域
    fb = fallback_area(img.shape)
    assert fb is not None and fb[2] > fb[0] and fb[3] > fb[1], "兜底裁剪不可用"

    crop = img[y_top:y_in, x0:x1]

    def box(gx0, gy0, gx1, gy1):
        """整帧坐标 → 消息区裁剪图坐标。"""
        return (gx0 - x0, gy0 - y_top, gx1 - x0, gy1 - y_top)

    # 分类：白气泡里的墨迹 = 对方；绿气泡 = 我；面板上的灰字 = gray
    assert who_said(crop, box(446, 164, 884, 176))[0] == "her", "白底墨迹应判成 her"
    assert who_said(crop, box(716, 274, 1134, 286))[0] == "me", "绿底墨迹应判成 me"
    assert who_said(crop, box(426, 396, 604, 408))[0] == "gray", "面板上的灰字应判成 gray"

    # 多行合并：同一气泡内的两行（间距小于半个字高）合成一条
    msgs = parse_messages(crop, [
        ("对方第一行", box(446, 164, 884, 176)),
        ("对方第二行", box(446, 176, 804, 188)),
        ("我这句", box(716, 274, 1134, 286)),
    ], pane)
    assert len(msgs) == 2, f"应合并成 2 条，实际 {len(msgs)}: {msgs}"
    assert msgs[0][0] == "her" and msgs[1][0] == "me", f"说话人判错: {msgs}"
    assert msgs[0][2] == "对方第一行对方第二行", f"同一气泡的两行没合并: {msgs[0][2]}"

    # 上下文文本：对方带名字（群聊），自己标「我」
    ctx = format_context([("her", "张三", "在吗"), ("me", None, "在的")])
    assert ctx == "对方（张三）：在吗\n我：在的", f"上下文格式不对: {ctx!r}"

    print("capture_wgc 自测通过：锚点", (x0, y_top, x1, y_in), "底色", tuple(int(v) for v in bg))
