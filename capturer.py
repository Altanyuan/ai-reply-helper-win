import time
import logging
import pyperclip
import uiautomation as auto

import wechat_window

log = logging.getLogger("polish")


def _get_clipboard():
    try:
        return pyperclip.paste()
    except Exception:
        return ""


def _find_input_edit(win):
    """定位微信“消息输入框”控件（兼容微信 3.x / 4.x）。

    保留此函数名以兼容历史导入；实际逻辑委托给 wechat_window.find_input_edit
    （在微信窗口子树内枚举 Edit，仅当没有 Edit 时才回退 Document，
    取 BoundingRectangle 最靠下者 —— 顶部搜索框在上方，消息输入框在底部）。
    """
    try:
        return wechat_window.find_input_edit(win)
    except Exception as e:
        log.warning(f"定位输入框失败: {e}")
        return None


def capture_draft():
    """捕获微信消息输入框的草稿：聚焦输入框 → Ctrl+A + Ctrl+C。

    使用哨兵值判断输入框是否为空（Ctrl+C 复制失败时剪贴板不变，
    会被误判为“有内容”），确保空输入框返回空字符串而非旧剪贴板内容。

    健壮性修复：若检测不到微信主窗口，则**不执行任何键盘模拟**，
    直接返回空草稿并还原剪贴板，避免把 Ctrl+A/Ctrl+C 误发到当前前台窗口
    （会破坏用户正在编辑的内容，甚至把前台窗口文本误当草稿去润色）。
    """
    original = _get_clipboard()
    sentinel = "__WECHAT_AI_EMPTY_SENTINEL__"
    try:
        pyperclip.copy(sentinel)
    except Exception:
        pass

    win = wechat_window.find_wechat_control()
    if win is None:
        # 微信不在运行：不去触碰任何前台窗口，直接返回空草稿（并还原剪贴板）
        log.info("capture_draft: 未检测到微信窗口，跳过抓取")
        try:
            pyperclip.copy(original)
        except Exception:
            pass
        return ""

    try:
        # 先激活微信窗口：新版微信（4.x，Qt 重写）常常没有标准 Edit 控件，
        # 此时靠「微信为活动窗口 + Ctrl+A/C」依然能取到输入框内容；
        # 同时避免把按键发给当前其它窗口（否则会误伤用户正在编辑的内容）。
        try:
            win.SetActive()
            time.sleep(0.12)
        except Exception:
            pass
        # 确保焦点在消息输入框（不是搜索框），否则 Ctrl+A 会选中错误内容。
        # 4.x（Qt）UIA 拿不到 Edit 控件，focus_input 会自动改用坐标点击兜底。
        wechat_window.focus_input(win)
        time.sleep(0.12)
        auto.SendKeys("{Ctrl}a")
        time.sleep(0.08)
        auto.SendKeys("{Ctrl}c")
        time.sleep(0.15)
    except Exception as e:
        log.warning(f"捕获草稿时失败: {e}")
    text = _get_clipboard()
    if text == sentinel:          # 复制前后没变化 → 输入框为空/无可选文本
        text = ""
    # 还原剪贴板，避免污染用户原有内容
    try:
        pyperclip.copy(original)
    except Exception:
        pass
    return text.strip() if text else ""


def read_wechat_recent(n=5, use_ocr=True, hwnd=None, backend="wgc"):
    """尽力读取微信当前聊天窗口最近 n 条消息作为上下文。

    **两条采集路径按「运行时能力探测」分流，互不干扰**（不解析微信版本号）：

      ▸ 3.x 路径（UIA 直读）—— 微信 3.x（Duilib）的消息列表是 ListControl、
        文字由系统精确给出、没有识别误差。它在第一级命中后直接返回，
        **永远不会进入 4.x 的采集代码**（回归红线：`_read_via_uia` 逻辑不得改动）。

      ▸ 4.x 路径（WGC 采集）—— 微信 4.x（Qt 重写）完全不向 UIA 暴露子控件
        （实测主窗口 GetChildren() 恒为 0），只能靠截图：默认走 `capture_wgc`
        （WGC 窗口捕获 + 像素锚点定位 + 说话人分类，被遮挡也能截、不抢前台），
        失败再回落到原有的「截屏 OCR」。

    任何一级失败都只是降级（最终返回空字符串 = 无上下文润色），不影响主流程。

    参数：
      n: 取最近多少条消息
      use_ocr: 是否允许 4.x 路径用 OCR 读上下文（配置项 ocr_context）
      hwnd: 微信主窗口句柄；缺省时自行定位
      backend: 4.x 的采集后端，'wgc'（默认）或 'ocr'（配置项 capture_4x）
    """
    n = int(n)
    try:
        hwnd = hwnd or wechat_window.find_wechat_hwnd()
        if not hwnd:
            log.info("未找到微信窗口，跳过上下文读取")
            return ""
        # 新版微信（4.x/Qt）默认不暴露 UIA 子控件树，先唤醒无障碍桥再试 UIA。
        # 对 3.x 是无害的空操作。
        wechat_window.wake_accessibility(hwnd, rounds=3, interval=0.25)

        # ============ 3.x 路径：UIA 直读（命中即返回）============
        text = _read_via_uia(n)
        if text:
            return text

        # ============ 4.x 路径：WGC 采集（Qt 自绘，UIA 拿不到控件树）============
        if use_ocr and str(backend or "wgc").lower() != "ocr":
            text = _read_via_wgc(hwnd, n)
            if text:
                return text

        # ============ 兜底：截屏 OCR（4.x 的旧路径 / WGC 不可用时）============
        if use_ocr:
            text = _read_via_screen_ocr(hwnd, n)
            if text:
                return text
            log.info("OCR 未取到上下文（可能聊天区为空、或窗口不在前台被遮挡）")

        log.info("未读取到任何聊天上下文（UIA 不可读且 4.x 采集无结果或不可用）")
        return ""
    except Exception as e:
        log.warning(f"读取微信上下文失败（已降级为空）: {e}")
        return ""


# ==================== 3.x 路径：UIA 直读 ====================

def _read_via_uia(n):
    """微信 3.x 专属路径：UIA 消息列表直读。

    **回归红线：这是 3.x 的既有路径，行为必须保持不变。**
    4.x（Qt）不暴露子控件，本函数在 4.x 上会自然返回空字符串，
    由 read_wechat_recent 转到 4.x 的 WGC 路径。

    两级：
      A) 消息列表直读 —— 3.x 首选，按已知控件名精确匹配（"消息" / "ChatView"）；
      B) 控件树扁平收集 —— 过渡版本兜底（消息节点并列、没有 ListControl）。
    """
    win = wechat_window.find_wechat_control()
    if win is None:
        log.info("未找到微信窗口，跳过 UIA 读取")
        return ""
    try:
        win.SetActive()
    except Exception:
        pass
    time.sleep(0.15)

    # ---- A：消息列表直读（微信 3.x —— 文字精确，优先命中）----
    list_ctrl = wechat_window.find_message_list(win)
    if list_ctrl is not None:
        try:
            items = list_ctrl.GetChildren()
        except Exception as e:
            log.warning(f"读取消息列表子项失败: {e}")
            items = []
        texts = []
        for it in items[-n:]:
            t = wechat_window.node_text(it)
            if t:
                texts.append(t)
        if texts:
            log.info(f"读取到 {len(texts)} 条聊天上下文（UIA 列表模式）")
            return "\n".join(texts)

    # ---- B：控件树扁平收集（过渡版本兜底）----
    texts, visited = wechat_window.collect_texts(win, max_depth=6, max_nodes=600)
    dedup = []
    for t in texts:
        s = (t or "").strip()
        if not s:
            continue
        if dedup and dedup[-1] == s:
            continue
        dedup.append(s)
    # 至少要有 2 段文本才认定「控件树确实暴露出内容」，
    # 避免把零星的界面标签当成聊天上下文。
    if len(dedup) >= 2:
        picked = dedup[-n:]
        log.info(
            f"读取到 {len(picked)} 条聊天上下文（UIA 扁平模式，"
            f"遍历 {visited} 节点、收集 {len(dedup)} 段文本）"
        )
        return "\n".join(picked)
    return ""


# ==================== 4.x 路径：WGC 窗口捕获 ====================

def _read_via_wgc(hwnd, n):
    """微信 4.x 专属路径：WGC 抓窗口 → 像素锚点定位消息区 → OCR → 说话人分类。

    与旧截屏方案相比（都是实测过的差异）：
      · 抓的是**窗口**而不是屏幕 —— 被遮挡、不在前台都能截到，也不抢焦点
        （用户正在打字时不会被打断）；
      · 消息区**动态定位**（面板底色 + 分隔线），不按 1920x1030 基准等比换算，
        窗口任意缩放下裁剪都不偏（旧方案窄窗口会混入联系人名、高窗口会切掉最新几条）；
      · 等到**画面停稳**才取帧，滚动动画与半截气泡不会进来；
      · 按气泡颜色分 me / her，丢掉图片里的字与灰字（时间戳、系统提示）。

    失败（依赖缺失、窗口不可见、OCR 无结果）返回空字符串，由调用方回落下一级。
    """
    try:
        import capture_wgc
    except Exception as e:
        log.warning(f"WGC 采集模块不可用（{type(e).__name__}），将回落到截屏 OCR")
        return ""
    if not capture_wgc.wgc_available():
        return ""
    msgs = capture_wgc.read_recent(hwnd, n=n)
    if not msgs:
        return ""
    return capture_wgc.format_context(msgs)


# ==================== 兜底：截屏 OCR（4.x 旧路径）====================

def _read_via_screen_ocr(hwnd, n):
    """旧版 4.x 采集：把窗口提到前台后截屏 + WinRT OCR。

    保留为最后一级兜底（WGC 依赖缺失或抓帧失败时用）。它要求窗口在前台且不被遮挡，
    对窗口尺寸也更敏感 —— 能用 WGC 就不走这里。
    """
    try:
        import ocr_reader
        if not ocr_reader.ocr_available():
            log.info("OCR 后端不可用，跳过截屏识别")
            return ""
        ctx = ocr_reader.read_chat_context(hwnd, max_messages=n)
        if ctx:
            log.info("读取到聊天上下文（OCR 截屏模式）")
            return ctx
        return ""
    except Exception as e:
        log.warning(f"OCR 读取上下文失败: {e}")
        return ""


def read_wechat_input():
    """尽量直接读微信消息输入框当前文本（比剪贴板更可靠地判断是否为空）。

    只返回“确实有内容”的文本；控件找不到 / 读取失败 / 为空时返回 None，
    交由调用方回落到剪贴板方式（capture_draft）。best-effort。
    """
    try:
        win = wechat_window.find_wechat_control()
        if win is None:
            return None
        edit = _find_input_edit(win)
        if edit is None:
            return None
        try:
            val = edit.GetValuePattern().Value
        except Exception:
            try:
                val = edit.GetLegacyIAccessiblePattern().Value
            except Exception:
                return None
        if not val or not val.strip():
            return None
        return val
    except Exception as e:
        log.warning(f"读取微信输入框失败: {e}")
        return None
