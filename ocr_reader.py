"""基于 Windows 内置 OCR 的聊天上下文读取（微信 4.x 专用兜底）。

背景（重要）：
  微信 4.0 起客户端用 Qt 重写，**完全不向 UI Automation 暴露内部控件** ——
  实测（微信 4.1.13.x）：
    • 主窗口 ``GetChildren()`` 恒为 0 个控件；
    • 发 ``WM_GETOBJECT`` 唤醒多轮后仍为 0；
    • 消息区 ``Ctrl+A/C`` 复制到 0 字符（自绘控件不支持全选）。
  因此输入框 / 消息列表 / 气泡全部无法通过 UIA 读取。

  但**草稿捕获与回填不受影响** —— 它们用 ``Ctrl+A/C/V`` 纯键盘模拟，
  不依赖控件树。仅「读取聊天上下文」这一项需要 OCR 兜底。

实现方式：
  使用 Windows 10/11 内置的 WinRT OCR（``Windows.Media.Ocr``），
  无需安装 Tesseract 等外部依赖，中文识别开箱即用。
  Python 绑定采用新版分包 ``winrt-*``（Python 3.13 有预编译 wheel；
  旧的 ``winsdk`` / 单体 ``winrt`` 在 3.13 上无法构建）。
  任一依赖缺失时优雅降级（返回空字符串，不影响润色主流程）。

流程：
  截取微信窗口「客户区」（不含边框）
  → 等比裁剪出「聊天消息区」（避开左侧会话列表与底部输入框）
  → WinRT OCR 识别 → 按词距重建每行 → 返回最近 n 条。

约束：
  截图仅在本机内存中处理，不落盘、不上传；日志只记录行数与区域尺寸，
  不输出聊天正文。
"""
import asyncio
import logging
import time

log = logging.getLogger("polish")

# 懒加载的 OCR 组件（避免 import 失败拖垮主程序）
_OcrEngine = None
_BitmapDecoder = None
_DataWriter = None
_InMemoryRandomAccessStream = None

_BACKEND_READY = None      # None=未探测 / True=可用 / False=不可用
_IMPORT_ERROR = ""
_ENGINE = None             # 已创建好的引擎（复用，避免每次识别重建）
_ENGINE_LANG = ""          # 实际使用的识别语言标签

# 优先尝试的语言：聊天内容是中文，必须优先用中文引擎。
# 踩坑记录：系统默认引擎是「用户配置语言」，实测为 en-US（英语），
# 用它识别中文会把中文全部丢成乱码或直接不输出，只剩英文片段。
PREFERRED_LANGS = ("zh-Hans-CN", "zh-CN", "zh-Hans", "zh-Hant-TW", "en-US")


def _create_engine():
    """创建 OCR 引擎：优先中文，找不到再退回用户默认语言。

    返回 ``(engine, lang_tag)``；彻底失败返回 ``(None, "")``。
    """
    # 1) 优先显式创建中文引擎
    try:
        from winrt.windows.globalization import Language
        for tag in PREFERRED_LANGS:
            try:
                lang = Language(tag)
                e = _OcrEngine.try_create_from_language(lang)
                if e is not None:
                    return e, tag
            except Exception:
                continue
    except Exception as e:
        log.warning(f"创建指定语言引擎失败，回退到默认语言: {e}")

    # 2) 回退：用户配置默认语言（可能是英文，中文识别会变差）
    try:
        e = _OcrEngine.try_create_from_user_profile_languages()
        if e is not None:
            tag = ""
            try:
                tag = e.recognizer_language.language_tag
            except Exception:
                pass
            return e, tag
    except Exception:
        pass
    return None, ""


def _load_backend():
    """加载 WinRT OCR 组件并创建引擎。返回 True/False，结果缓存。"""
    global _OcrEngine, _BitmapDecoder, _DataWriter, _InMemoryRandomAccessStream
    global _BACKEND_READY, _IMPORT_ERROR, _ENGINE, _ENGINE_LANG

    if _BACKEND_READY is not None:
        return _BACKEND_READY

    try:
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.storage.streams import (
            DataWriter, InMemoryRandomAccessStream,
        )
        _OcrEngine = OcrEngine
        _BitmapDecoder = BitmapDecoder
        _DataWriter = DataWriter
        _InMemoryRandomAccessStream = InMemoryRandomAccessStream
    except Exception as e:
        _IMPORT_ERROR = str(e)
        _BACKEND_READY = False
        log.warning(
            "OCR 后端不可用（%s）。请执行：\n"
            "  pip install winrt-runtime winrt-Windows.Media.Ocr "
            "winrt-Windows.Globalization winrt-Windows.Graphics.Imaging "
            "winrt-Windows.Storage.Streams winrt-Windows.Foundation\n"
            "在此之前将降级为「无上下文润色」。", e,
        )
        return False

    engine, tag = _create_engine()
    if engine is None:
        _IMPORT_ERROR = "OCR 引擎创建失败：系统未安装可用的 OCR 语言包"
        _BACKEND_READY = False
        log.warning(
            "%s。请在「设置 → 时间和语言 → 语言」中为中文添加"
            "「光学字符识别」可选功能。", _IMPORT_ERROR,
        )
        return False

    _ENGINE = engine
    _ENGINE_LANG = tag
    _BACKEND_READY = True
    if tag and not tag.lower().startswith("zh"):
        log.warning(
            "OCR 引擎语言为 %s（非中文），中文聊天内容可能识别不准。"
            "建议在系统设置中为中文添加 OCR 语言包。", tag,
        )
    log.info("OCR 后端就绪：winrt（识别语言=%s）", tag or "系统默认")
    return True


def ocr_available():
    """OCR 是否可用（供启动提示与配置校验）。"""
    return _load_backend()


def ocr_status_text():
    """返回给用户看的状态说明。"""
    if _load_backend():
        return f"OCR 可用（识别语言 {_ENGINE_LANG or '系统默认'}）"
    return f"OCR 不可用：{_IMPORT_ERROR}"


_DPI_AWARE_SET = False


def ensure_dpi_aware():
    """把本进程声明为「每显示器 DPI 感知」。

    必须做，且只需做一次。原因（踩坑记录）：
      - 若进程 **不感知** DPI，Win32 API 返回的是被系统缩放过的「逻辑坐标」，
        需手动乘 dpi/96 才能对齐物理像素；
      - 若进程 **感知** DPI（我们这里），API 直接返回「物理像素」，
        再乘一次就会放大 1.25 倍 —— 实测客户区 1920×1030 被算成 2400×1287，
        超出 1920×1080 屏幕，截出大片黑边且内容错位。
    统一声明感知后，后续所有坐标都是物理像素，**不再做任何手动缩放**，
    逻辑唯一，不会自相矛盾。
    """
    global _DPI_AWARE_SET
    if _DPI_AWARE_SET:
        return
    try:
        import ctypes
        # 1 = PROCESS_SYSTEM_DPI_AWARE；2 = PROCESS_PER_MONITOR_DPI_AWARE
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    _DPI_AWARE_SET = True


def get_client_box(hwnd):
    """取窗口「客户区」在屏幕上的**物理像素**矩形 (left, top, right, bottom)。

    为什么不用 GetWindowRect：
      - GetWindowRect 含边框/阴影，窗口最大化时给的是 (-9,-9,1929,1039)
        这类带负坐标的矩形，截出的图会带上边框与黑边。
    改用 GetClientRect（客户区，不含边框）+ ClientToScreen（转屏幕坐标）。

    前提：调用前已经 ensure_dpi_aware()，因此返回值就是物理像素，无需缩放。
    """
    import ctypes
    from ctypes import wintypes

    ensure_dpi_aware()

    user32 = ctypes.windll.user32
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.ClientToScreen.restype = wintypes.BOOL

    rc = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rc)):
        return None
    pt = wintypes.POINT(rc.left, rc.top)
    if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return None

    width = rc.right - rc.left
    height = rc.bottom - rc.top
    if width <= 0 or height <= 0:
        return None

    left, top = int(pt.x), int(pt.y)
    return (left, top, left + int(width), top + int(height))


def _grab_window_image(hwnd):
    """截取指定窗口「客户区」位图（PIL.Image）。失败返回 None。"""
    try:
        from PIL import ImageGrab
    except Exception as e:
        log.warning(f"PIL 不可用，无法截图: {e}")
        return None

    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL

    # 先置前，避免截到被遮挡内容
    try:
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.25)

    box = get_client_box(hwnd)
    if box is None:
        log.warning("获取窗口客户区失败")
        return None

    try:
        return ImageGrab.grab(bbox=box, all_screens=True)
    except Exception as e:
        log.warning(f"截图失败: {e}")
        return None


# ---- 聊天区裁剪：基于「参考客户区尺寸」的等比换算 ----
#
# 微信 4.x 的客户区布局基准（1920×1030 客户区实测，逐像素标注）：
#
#   x: 0 ── 378 ──────────────── 1168 ── 1920
#      [ 会话列表 ] [      聊天消息区      ] [右侧留白]
#   y: 0 ── 86 ──────────── 488 ──── 1030
#      [标题栏] [   消息区   ] [输入框区]
#
# 说明：
#   - 左侧会话列表宽约 378px（含头像条），再往左会把对方气泡文字切掉；
#   - 右侧滚动条在 x≈1168，超过它只是空白，故右边界取 1168 省算力；
#   - 底部输入框上边界 y≈488，超过会混入输入框边框与按钮。
# 换算时按当前客户区与基准的比例缩放，窗口大小不同也能自适应。
# 这些是「客户区像素」，不含窗口边框（由 get_client_box 保证）。
REF_W = 1920
REF_H = 1030

REF_LEFT = 378        # 会话列表右边界 → 聊天消息区左边界
REF_TOP = 86          # 标题栏（含会话名行）之下，消息区上边界
REF_RIGHT = 1168      # 滚动条右侧，节省识别算力（超出部分为空白）
REF_BOTTOM = 488      # 输入框上边界（不含输入框）

# 兜底比例（当客户区尺寸异常、无法等比换算时使用）
CROP_LEFT = 0.197
CROP_TOP = 0.083
CROP_RIGHT = 0.608
CROP_BOTTOM = 0.474


def _crop_chat_area(img):
    """从客户区截图裁出「聊天消息区」（按基准客户区等比换算）。

    返回裁剪后的 PIL.Image；若换算结果异常则退回原图，保证不裁出空图。
    """
    if img is None:
        return None
    w, h = img.size
    sx = w / float(REF_W)
    sy = h / float(REF_H)

    left = int(REF_LEFT * sx)
    top = int(REF_TOP * sy)
    right = int(REF_RIGHT * sx)
    bottom = int(REF_BOTTOM * sy)

    # 越界兜底
    left = max(0, min(left, w - 1))
    top = max(0, min(top, h - 1))
    right = max(left + 1, min(right, w))
    bottom = max(top + 1, min(bottom, h))

    if right - left < 60 or bottom - top < 60:
        log.warning(
            "聊天区裁剪结果过小（%dx%d，客户区 %dx%d），已退回整幅客户区截图",
            right - left, bottom - top, w, h,
        )
        return img

    log.info(
        "聊天区裁剪: 客户区 %dx%d → 区域 (%d,%d)-(%d,%d) 尺寸 %dx%d",
        w, h, left, top, right, bottom, right - left, bottom - top,
    )
    return img.crop((left, top, right, bottom))


def _png_bytes(img):
    """PIL.Image → PNG 字节。"""
    import io as _io
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _recognize_async(img, engine):
    """在**同一个**事件循环里完成「写流 → 解码 → 识别」。

    踩坑记录：不能在 async 函数里再调 asyncio.run()（会报
    "asyncio.run() cannot be called from a running event loop"）。
    WinRT 的多个 await 必须串在同一个循环里，因此这里把所有步骤
    都写成 await，由外层统一 asyncio.run 驱动。
    """
    data = _png_bytes(img)

    stream = _InMemoryRandomAccessStream()
    writer = _DataWriter(stream)
    writer.write_bytes(data)
    await writer.store_async()
    await writer.flush_async()
    stream.seek(0)

    decoder = await _BitmapDecoder.create_async(stream)
    # OCR 需要 SoftwareBitmap；部分 winrt 版本无此方法，退回解码器本身
    try:
        bitmap = await decoder.get_software_bitmap_async()
    except AttributeError:
        bitmap = decoder
    return await engine.recognize_async(bitmap)


def _run_coro(coro):
    """在**独立线程**里跑协程（不直接用调用方线程）。

    为什么必须另起线程（实测踩坑，重要）：
      WinRT 的异步机制要求执行线程是「未被 STA 初始化过 COM」的干净线程。
      本程序里调用 OCR 的线程恰恰是 uiautomation 的工作线程 ——
      `UIAutomationInitializerInThread` 已经在该线程做过 CoInitialize（STA）。
      在那样的线程里直接 `asyncio.run` 驱动 WinRT 会**死锁**：
      实测「先 UIA 后 OCR」与「先 OCR 后 UIA」在同一线程内都会卡住 45s+ 无返回；
      而把 OCR 放到全新线程执行则稳定通过。
    另起线程同时也顺带解决了「当前线程已有事件循环」导致的嵌套 RuntimeError。
    """
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _union_rect(words):
    """一组 word 的外接矩形 (x0, y0, x1, y1)；一个都拿不到坐标时返回 (0, 0, 0, 0)。

    注意（踩坑）：WinRT 的 Rect 属性是 x / y / width / height，
    没有 left / right / top / bottom。
    """
    x0 = y0 = x1 = y1 = None
    for w in words or []:
        try:
            r = w.bounding_rect
            a, b = float(r.x), float(r.y)
            c, d = a + float(r.width), b + float(r.height)
        except Exception:
            continue
        x0 = a if x0 is None else min(x0, a)
        y0 = b if y0 is None else min(y0, b)
        x1 = c if x1 is None else max(x1, c)
        y1 = d if y1 is None else max(y1, d)
    if x0 is None:
        return (0, 0, 0, 0)
    return (int(x0), int(y0), int(x1), int(y1))


def recognize_boxes(img):
    """对 PIL.Image 做 OCR，返回 [(文本, (x0, y0, x1, y1))]，按纵向排序。失败返回 []。

    与下面 _recognize 的唯一区别：连每个文字行的**位置**一起返回 —— 上层要拿这个框
    回原图做颜色采样，判断这行字是对方气泡、我方绿泡还是灰字
    （见 capture_wgc.who_said）。WinRT 只有 OcrWord 带 bounding_rect（OcrLine 没有），
    所以行的框取该行所有 word 的并集；行文本仍走 _join_words（修中文逐字空格的老问题）。
    """
    if not _load_backend():
        return []
    try:
        # 复用 _load_backend 里已按「优先中文」策略创建好的引擎，避免重复创建
        engine = _ENGINE
        if engine is None:
            engine, _ = _create_engine()
            if engine is None:
                log.warning("OCR 引擎创建失败（缺少语言包）")
                return []

        result = _run_coro(_recognize_async(img, engine))

        out = []
        for line in result.lines:
            try:
                words = list(line.words)
            except Exception:
                words = []
            text = _join_words(words, fallback=line.text or "")
            if text:
                out.append((text, _union_rect(words)))
        out.sort(key=lambda item: item[1][1])
        return out
    except Exception as e:
        log.warning(f"OCR 识别失败: {type(e).__name__}: {e}")
        return []


def _recognize(img):
    """对 PIL.Image 做 OCR，返回按纵向排序的文本行列表（不带坐标）。失败返回 []。"""
    return [t for t, _ in recognize_boxes(img)]


def _join_words(words, fallback=""):
    """按词重建一行文本，修掉中文引擎「逐字空格」的问题。

    踩坑记录：WinRT 中文引擎（zh-Hans-CN）会把每个汉字识别成独立的 word，
    直接取 ``line.text`` 会得到 "微 信 单 条 消 息" 这种带空格的畸形文本，
    喂给大模型会严重影响理解。

    修正策略：依据相邻 word 的 bounding_rect 水平间距判断是否该补空格 ——
      - 两个 word 都是「CJK / 全角标点」→ 不补空格（中文连续书写）
      - 否则按间距是否超过半个字符宽决定补不补（英文单词间需要空格）

    注意：WinRT 的 Rect 属性是 x / y / width / height，
          没有 left / right / top / bottom（踩坑）。
    """
    if not words:
        return (fallback or "").strip()

    parts = []
    prev_right = None
    prev_char_h = 0.0
    for w in words:
        try:
            t = w.text or ""
        except Exception:
            t = ""
        if not t:
            continue
        try:
            rect = w.bounding_rect
            right = float(rect.x) + float(rect.width)
            char_h = float(rect.height)
        except Exception:
            right = None
            char_h = 0.0

        if prev_right is not None and right is not None and parts:
            gap = float(rect.x) - prev_right
            prev_t = parts[-1]
            # 汉字/全角标点之间不插空格
            if _is_cjk(prev_t[-1:]) and _is_cjk(t[:1]):
                need_space = False
            else:
                # 用字符高度估算「半个字符宽」，超过则视为单词间隔
                ref_h = char_h or prev_char_h or 12.0
                need_space = gap > ref_h * 0.3
            if need_space and not parts[-1].endswith(" "):
                parts.append(" ")

        parts.append(t)
        prev_right = right
        prev_char_h = char_h

    text = "".join(parts).strip()
    # 合并可能产生的多余空格
    while "  " in text:
        text = text.replace("  ", " ")
    return text


def _is_cjk(ch):
    """判断字符是否为中日韩文字或全角标点（这类字符间不应插入空格）。"""
    if not ch:
        return False
    o = ord(ch)
    return (
        0x4E00 <= o <= 0x9FFF or      # CJK 统一表意文字
        0x3400 <= o <= 0x4DBF or      # CJK 扩展 A
        0x3000 <= o <= 0x303F or      # CJK 标点（、。「」等）
        0xFF00 <= o <= 0xFFEF or      # 全角字符
        0xF900 <= o <= 0xFAFF         # CJK 兼容表意文字
    )


def read_chat_context(hwnd, max_messages=6):
    """OCR 读取微信聊天上下文。

    参数：
      hwnd: 微信主窗口句柄（用于截图定位）
      max_messages: 最多返回多少条消息

    返回：字符串（换行分隔的最近消息）；失败或不可用返回 ""。
    调用方应把空字符串视为「无上下文」，正常降级。
    """
    if not hwnd:
        return ""
    if not _load_backend():
        return ""

    img = _grab_window_image(hwnd)
    if img is None:
        return ""
    chat = _crop_chat_area(img)
    lines = _recognize(chat)
    if not lines:
        log.info("OCR 未识别到聊天文本")
        return ""

    # 过滤噪声行并去掉相邻重复
    cleaned = []
    for t in lines:
        s = t.strip()
        if not s:
            continue
        # 至少含一个字母/数字/汉字，否则判为图标或分隔线识别噪声
        if not any(ch.isalnum() or '\u4e00' <= ch <= '\u9fff' for ch in s):
            continue
        if cleaned and cleaned[-1] == s:
            continue
        cleaned.append(s)

    if not cleaned:
        return ""
    picked = cleaned[-int(max_messages):]
    log.info(f"OCR 读取到 {len(picked)} 条聊天上下文（共识别 {len(cleaned)} 行）")
    return "\n".join(picked)
