# -*- coding: utf-8 -*-
"""微信 4.x 消息采集：WGC 窗口捕获 + 像素锚点定位 + 说话人分类。

为什么单独一条路径（背景，实测结论）：
  微信 4.x 用 Qt 重写后**完全不向 UIA 暴露子控件**（主窗口 GetChildren() 恒为 0，
  发 WM_GETOBJECT 唤醒多轮仍是 0），3.x 的「控件树直读」在 4.x 上必然拿不到东西。
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

OCR 后端沿用项目现有的 Windows 内置 OCR（`ocr_reader`），不引入 RapidOCR：
  WinRT 的 OcrWord 也带 bounding_rect，够做颜色分类；又是系统组件、已随包分发过，
  中文引擎优先，能省下 onnxruntime + 模型那 60MB。
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
_COL_RATIO = 0.3       # 一列里底色占比超过它 → 这一列在面板内
_ROW_RATIO = 0.9       # 一行里底色占比超过它 → 这一行是纯面板
_SEP_STD = 4           # 整行标准差 < 它 → 单色行（横向分隔线候选）
_SEP_ROW = 0.1         # 分隔线所在行的底色占比要低于它
_MIN_W, _MIN_H = 100, 40
_HEADER_RATIO = 0.085  # 标题栏（含会话名）占客户区高度的比例（1920x1030 实测 86px）
_HEADER_MIN, _HEADER_MAX = 40, 140

# 锚点定位失败时的兜底裁剪比例（沿用旧版本的实测标定，聊胜于无）
_FALLBACK = (0.197, 0.083, 0.608, 0.474)   # left, top, right, bottom


def wgc_available() -> bool:
    """WGC 采集是否可用。依赖缺失（没装 windows-capture）时返回 False，
    调用方会回落到原有的截屏 OCR 路径，不影响主流程。"""
    try:
        import windows_capture  # noqa: F401
        return True
    except Exception as e:
        log.info(f"WGC 不可用（{type(e).__name__}），4.x 采集将回落到截屏 OCR")
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


def _area_with_bg(full, bg):
    """给定面板底色，用像素锚点算出消息区；算不出返回 None。"""
    H, W = full.shape[:2]
    # 底色掩码 → 列/行占比
    isbg = np.abs(full.astype(np.int16) - bg).sum(-1) <= _BG_TOL
    col = isbg[H // 4: H * 3 // 4].mean(0)
    x0 = int(np.argmax(col > _COL_RATIO))
    x1 = W - int(np.argmax(col[::-1] > _COL_RATIO))
    if x1 - x0 < _MIN_W:
        return None

    row = isbg[:, x0:x1].mean(1)
    y0 = int(np.argmax(row > _ROW_RATIO))
    y1 = H - int(np.argmax(row[::-1] > _ROW_RATIO))
    if y1 - y0 < _MIN_H:
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

    if y_in - y_top < _MIN_H:
        return None
    return x0, y_top, x1, y_in, bg


def chat_area(full):
    """从整帧里定位「消息区」，返回 (x0, y_top, x1, y_in, 面板底色) 或 None。

    全像素锚点，不写死坐标、不做基准等比换算，所以窗口任意缩放、深浅主题都成立：
      · 面板底色 = 候选色里**算出来的消息区最靠上**的那个（见 _bg_candidates）
      · 左右边界 = 第一 / 最后一根「底色占比 > 30%」的列
        （左侧会话列表是另一种底色，占比为 0，天然被排除）
      · 消息区上下界 = 「底色占比 > 90%」的纯面板行
      · 横向分隔线 = 整行单色（标准差 < 4）且几乎不含底色；
        输入框顶 = 面板 45% 高度以下的第一根，再往上一根是公告条下沿，
        没有就用标题栏高度兜底
    认不出（窗口太小 / 布局还没铺好）返回 None，由调用方走兜底裁剪。
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
        lines.append(f"我：{text}" if who == "me" else f"{name or '对方'}：{text}")
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


def read_recent(hwnd, n=5, timeout=GRAB_TIMEOUT):
    """4.x 采集入口：WGC 抓帧 → 定位消息区 → OCR → 分类 → 取最近 n 条。

    返回 [(who, name, text)]；任何环节失败都返回 []（调用方据此回落下一级），
    不影响主流程。帧与中间图像全程在内存里，不落盘。
    """
    if not hwnd:
        return []
    import ocr_reader
    if not ocr_reader.ocr_available():
        return []

    full, area = grab_frame(hwnd, timeout=timeout)
    if full is None or area is None:
        return []

    x0, y_top, x1, y_in, pane_bg = area
    crop = full[y_top:y_in, x0:x1]
    if crop.size == 0:
        return []
    log.info(f"WGC 采集：整帧 {full.shape[1]}x{full.shape[0]} → 消息区 "
             f"({x0},{y_top})-({x1},{y_in}) 尺寸 {x1 - x0}x{y_in - y_top}")

    try:
        from PIL import Image
        boxes = ocr_reader.recognize_boxes(Image.fromarray(crop))
    except Exception as e:
        log.warning(f"消息区 OCR 失败: {type(e).__name__}: {e}")
        return []
    if not boxes:
        log.info("WGC 采集：消息区没识别到文字")
        return []

    msgs = parse_messages(crop, boxes, pane_bg)
    if not msgs:
        log.info(f"WGC 采集：识别到 {len(boxes)} 个文本框，但没有可用的消息")
        return []
    picked = msgs[-int(n):]
    log.info(f"WGC 采集：识别 {len(boxes)} 框 → {len(msgs)} 条消息，取最近 {len(picked)} 条")
    return picked


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
    assert ctx == "张三：在吗\n我：在的", f"上下文格式不对: {ctx!r}"

    print("capture_wgc 自测通过：锚点", (x0, y_top, x1, y_in), "底色", tuple(int(v) for v in bg))
