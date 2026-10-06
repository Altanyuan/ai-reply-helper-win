"""微信主窗口定位 —— 兼容微信 3.x 与 4.x（Qt 重写）。

背景（实测结论）：
  - 微信 3.x：进程 `WeChat.exe`，窗口类名固定为 `WeChatMainWndForPC`
  - 微信 4.x：改用 **Qt 重写**，进程改名 `Weixin.exe`，窗口类名变成
    `Qt51514QWindowIcon` 一类 —— 其中 `Qt51514` 是 Qt 5.15.14 的版本编号，
    **会随微信内置的 Qt 版本变化**（如 Qt 6 会是另一套前缀）。

因此不能硬编码任何具体类名，否则微信一升级就再次失效。这里采用与 Qt 版本
无关的定位策略：按「进程名 + 窗口可见 + 窗口标题」来定位主窗口，类名仅作参考。
"""
import ctypes
import logging
import os
import time
from ctypes import wintypes

log = logging.getLogger("polish")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# ── 无障碍(UI Automation)唤醒相关 ────────────────────────────────
# 微信 4.x 是 Qt 应用，默认不向 UIA 暴露子控件树（探测只能拿到根窗口 1 个节点）。
# 与读屏软件一样，向窗口发送 WM_GETOBJECT 可触发 Qt 惰性构建 QAccessible 树。
WM_GETOBJECT = 0x003D
OBJID_CLIENT = -4        # 客户区无障碍对象
OBJID_WINDOW = 0         # 窗口本身
SMTO_ABORTIFHUNG = 0x0002

LRESULT = ctypes.c_ssize_t
try:
    user32.SendMessageTimeoutW.restype = LRESULT
    user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
    ]
except Exception:
    pass


def wake_accessibility(hwnd, rounds=3, interval=0.25):
    """唤醒新版微信（Qt）的无障碍树，使其对 UI Automation 可见。

    通过向窗口发送 WM_GETOBJECT（模拟辅助技术连接），促使 Qt 惰性初始化
    QAccessible 桥，之后 UIA 才能遍历到消息列表 / 输入框等子控件。

    对微信 3.x 是无害的空操作；调用前无需 COM 初始化。
    """
    if not hwnd:
        return
    for _ in range(max(1, rounds)):
        for objid in (OBJID_CLIENT, OBJID_WINDOW):
            try:
                res = ctypes.c_size_t(0)
                user32.SendMessageTimeoutW(
                    hwnd, WM_GETOBJECT, 0, objid,
                    SMTO_ABORTIFHUNG, 300, ctypes.byref(res),
                )
            except Exception:
                pass
        time.sleep(interval)

# 微信相关进程名（4.x 是 Weixin.exe，3.x 是 WeChat.exe，含常见变体）
WECHAT_PROCESS_NAMES = {
    "weixin.exe",      # 微信 4.x 主程序
    "wechat.exe",      # 微信 3.x 主程序
    "wechatapp.exe",
    "wechatstore.exe",
}

# 仅作参考/回退用的类名特征
LEGACY_CLASS = "WeChatMainWndForPC"   # 3.x 固定类名
QT_CLASS_PREFIX = "Qt"                # 4.x：Qt51514QWindowIcon 等

# 主窗口标题（4.x 与 3.x 主窗口标题都是“微信”）
MAIN_TITLE = "微信"

EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _enum_top_windows():
    """枚举所有顶层窗口，返回 [dict(hwnd,title,class,pid,visible)]。"""
    wins = []

    def _cb(hwnd, _lparam):
        try:
            if not user32.IsWindow(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            wins.append({
                "hwnd": int(hwnd),
                "title": buf.value,
                "class": cls.value,
                "pid": pid.value,
                "visible": bool(user32.IsWindowVisible(hwnd)),
            })
        except Exception:
            pass
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    return wins


def _proc_name(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(h)
    return None


def _is_wechat_process(pid, cache=None):
    if cache is not None and pid in cache:
        return cache[pid]
    name = _proc_name(pid)
    ok = bool(name and name in WECHAT_PROCESS_NAMES)
    if cache is not None:
        cache[pid] = ok
    return ok


def find_wechat_hwnd():
    """定位微信主窗口句柄，找不到返回 None。

    判定优先级（越靠前越可信）：
      1. 属于微信进程 + 可见 + 标题正好是“微信”
      2. 属于微信进程 + 可见 + 类名是已知的微信类名（3.x 固定类名或 Qt 窗口）
      3. 属于微信进程的任意可见顶层窗口
    """
    try:
        wins = _enum_top_windows()
    except Exception:
        return None

    cache = {}
    wx = [w for w in wins if w["visible"] and _is_wechat_process(w["pid"], cache)]
    if not wx:
        return None

    # 1) 标题精确匹配主窗口
    for w in wx:
        if w["title"].strip() == MAIN_TITLE:
            return w["hwnd"]

    # 2) 已知类名特征：3.x 固定类名 或 4.x 的 Qt 窗口
    for w in wx:
        if w["class"] == LEGACY_CLASS or w["class"].startswith(QT_CLASS_PREFIX):
            return w["hwnd"]

    # 3) 兜底：第一个属于微信进程的可见窗口
    return wx[0]["hwnd"]


def find_wechat_control():
    """返回微信主窗口对应的 uiautomation 控件，找不到返回 None。

    先走「进程名 + 标题」定位（兼容 4.x/3.x 且与 Qt 版本无关），
    失败再回退到按 3.x 固定类名搜索。

    调用方需确保当前线程已初始化 COM（本项目均用
    UIAutomationInitializerInThread 包裹）。
    """
    hwnd = find_wechat_hwnd()
    if hwnd:
        try:
            import uiautomation as auto
            ctrl = auto.ControlFromHandle(hwnd)
            if ctrl is not None:
                return ctrl
        except Exception:
            pass
    # 回退：老版本（3.x）固定类名
    try:
        import uiautomation as auto
        w = auto.WindowControl(searchDepth=1, ClassName=LEGACY_CLASS)
        if w.Exists(0, 0):
            return w
    except Exception:
        pass
    return None


def wechat_exists():
    """是否存在可定位的微信主窗口（供 main.py 轮询使用）。"""
    return find_wechat_hwnd() is not None


def is_wechat_running():
    """微信进程是否存在（用于区分「没开微信」和「开了但定位不到窗口」）。

    注意：这里枚举的是**所有顶层窗口（含不可见）**，因此微信缩在托盘里
    也能被判定为「进程在运行」，从而给用户「恢复窗口」而不是「先打开微信」
    的提示。
    """
    try:
        cache = {}
        for w in _enum_top_windows():
            if _is_wechat_process(w["pid"], cache):
                return True
    except Exception:
        pass
    return False


# ── 控件枚举 / 文本收集（3.x 直读与 4.x 兜底共用）────────────────
def iter_by_type(control_type, max_depth=16, max_items=30, root=None):
    """按 ControlType 逐实例枚举控件（uiautomation 没有 FindAll，只能靠 foundIndex）。

    root 为 None 时从**桌面根节点**全局搜索；传入微信窗口时**限定在微信窗口子树内**
    搜索。后者对微信 3.x 至关重要：3.x 里输入框名字为空，全靠「取最靠下的 Edit」
    来区分（顶部搜索框 vs 底部消息输入框）。若全局搜索，会把浏览器地址栏、
    编辑器等其它进程的 Edit 混进来，导致 SetFocus/Ctrl+V 打到错误窗口。

    返回控件列表；出错的实例静默跳过。
    """
    out = []
    try:
        import uiautomation as auto
        base = root if root is not None else auto.GetRootControl()
    except Exception:
        return out
    for idx in range(1, max_items + 1):
        try:
            c = base.Control(searchDepth=max_depth, ControlType=control_type,
                             foundIndex=idx)
            if not c.Exists(0, 0):
                break
            out.append(c)
        except Exception:
            break
    return out


def _rect_bottom(ctrl):
    try:
        return ctrl.BoundingRectangle.bottom
    except Exception:
        return 0


def _bottom_most(controls):
    """取「纵向最靠下」的控件；空列表返回 None。"""
    cands = [c for c in controls if _rect_bottom(c) > 0]
    if not cands:
        return None
    cands.sort(key=_rect_bottom, reverse=True)
    return cands[0]


def _node_self_text(node):
    """取节点**自身**文本：Name → ValuePattern → LegacyIAccessible。"""
    try:
        t = (getattr(node, "Name", "") or "").strip()
        if t:
            return t
    except Exception:
        pass
    try:
        v = node.GetValuePattern().Value
        if v and v.strip():
            return v.strip()
    except Exception:
        pass
    try:
        v = node.GetLegacyIAccessiblePattern().Value
        if v and v.strip():
            return v.strip()
    except Exception:
        pass
    return ""


def node_text(node):
    """尽最大努力取一个消息节点的文本（不同版本控件层级不同）。

    先取节点自身文本；若为空（气泡文本挂在内层子控件上），再拼接子节点文本。
    """
    t = _node_self_text(node)
    if t:
        return t
    parts = []
    try:
        for ch in node.GetChildren():
            s = _node_self_text(ch)
            if s:
                parts.append(s)
    except Exception:
        pass
    return "\n".join(parts).strip()


def collect_texts(root, max_depth=6, max_nodes=600):
    """广度优先遍历子树，收集所有有文本的节点（控件树「扁平」时的兜底）。

    返回 (texts, visited_count)。部分版本的控件树可能是扁平结构（所有消息
    作为兄弟节点并列），因此不能只取列表子项，需要遍历收集。
    """
    texts = []
    visited = 0
    stack = [(root, 0)]
    while stack and visited < max_nodes:
        node, d = stack.pop(0)
        visited += 1
        t = _node_self_text(node)
        if t:
            texts.append(t)
        if d >= max_depth:
            continue
        try:
            for ch in node.GetChildren():
                stack.append((ch, d + 1))
        except Exception:
            pass
    return texts, visited


# 3.x 消息列表控件的已知名称（**精确匹配优先**，避免与「会话列表」混淆）
MESSAGE_LIST_NAMES = ("消息", "ChatView")


def find_message_list(win):
    """定位微信「聊天消息列表」控件；找不到返回 None。

    关键兼容点（微信 3.x）：
      3.x 的**会话列表与消息列表都是 ListControl**。若只按「子项最多」这类
      启发式挑选，在「消息少、会话多」时会错选成会话列表，导致上下文读成
      一串联系人名。因此这里**先按已知名称精确匹配**（"消息" / "ChatView"），
      名称匹配不到时才回落到「子项最多」启发式。

    微信 4.x（Qt）控件树不可遍历，本函数返回 None，
    由调用方回落「扁平收集」或「截屏识别」。
    """
    if win is None:
        return None
    try:
        import uiautomation as auto
        list_type = auto.ControlType.ListControl
    except Exception:
        return None

    # 1) 名称精确匹配（3.x 首选，最稳）
    for c in iter_by_type(list_type, root=win, max_items=12):
        try:
            if (c.Name or "").strip() in MESSAGE_LIST_NAMES:
                log.info(f"消息列表已定位（按名称）: {c.Name!r}")
                return c
        except Exception:
            continue

    # 2) 启发式：List → Tree → Pane，取「子项最多」者
    for ct in (list_type,
               auto.ControlType.TreeControl,
               auto.ControlType.PaneControl):
        best, best_cnt = None, 0
        for c in iter_by_type(ct, root=win, max_items=12):
            try:
                cnt = len(c.GetChildren())
            except Exception:
                cnt = 0
            if cnt > best_cnt:
                best, best_cnt = c, cnt
        if best is not None and best_cnt > 0:
            log.info(f"消息列表已定位（启发式）: 子项数={best_cnt}")
            return best
    return None


def find_input_edit(win):
    """定位微信「消息输入框」控件（取纵向最靠下的那个，避开顶部搜索框）。

    微信窗口里有多个可编辑控件（顶部搜索框、底部消息输入框等），本工具要
    捕获的是**底部消息输入框**，因此取 BoundingRectangle 最靠下者 —— 3.x 里
    输入框名称同样为空，只能靠位置区分，这个语义不能改。

    实现要点：
      - 搜索**限定在微信窗口子树内**（`iter_by_type(root=win)`）：全局搜索会把
        浏览器地址栏、编辑器等其它进程的控件混进来，导致 SetFocus/Ctrl+V
        打到错误窗口。
      - **优先只枚举 Edit**，保持与改造前完全一致的行为；仅当 Edit 一个都没有
        时才回退枚举 Document（部分版本的输入框是 Document 而非 Edit）。
      - 微信 4.x（Qt）UIA 不暴露任何子控件，返回 None —— 由 focus_input 走坐标兜底。

    返回控件；找不到返回 None。
    """
    if win is None:
        return None
    try:
        import uiautomation as auto
        # 1) 优先 Edit（3.x 消息输入框就是 Edit，行为与改造前一致）
        ed = _bottom_most(iter_by_type(auto.ControlType.EditControl,
                                       root=win, max_items=12))
        if ed is not None:
            return ed
        # 2) 回退 Document（部分版本输入框是 Document）
        return _bottom_most(iter_by_type(auto.ControlType.DocumentControl,
                                         root=win, max_items=12))
    except Exception:
        return None


def focus_input(win):
    """把焦点放到微信消息输入框，保证后续 Ctrl+A/C/V 作用于输入框。

    微信 3.x：UIA 能找到 Edit 控件，直接 SetFocus。
    微信 4.x（Qt）：UIA 拿不到子控件，改用「点击聊天区底部输入框位置」兜底
                    —— 键盘操作不依赖 UIA，Qt 应用依然响应。

    返回是否成功。
    """
    # 1) 优先用 UIA 定位（3.x）
    ed = find_input_edit(win)
    if ed is not None:
        try:
            ed.SetFocus()
            return True
        except Exception:
            pass

    # 2) 兜底：按坐标点击聊天区底部（输入框大致位置）
    try:
        r = win.BoundingRectangle
        left, top, right, bottom = r.left, r.top, r.right, r.bottom
        w, h = right - left, bottom - top
        if w <= 0 or h <= 0:
            return False
        # 左侧会话列表宽度基本固定（约 300~350px，不随窗口等比缩放），
        # 因此用「绝对宽度 + 聊天区取中」比按百分比更准。
        sidebar = min(350, int(w * 0.3))
        x = left + sidebar + (w - sidebar) // 2
        # 输入框高度也相对固定（约 100~150px），用绝对偏移而非比例，
        # 避免窗口化/最大化时高度差导致点偏。
        y = bottom - min(80, int(h * 0.12))
        from pynput.mouse import Button, Controller as MouseController
        m = MouseController()
        m.position = (x, y)
        m.click(Button.left)
        return True
    except Exception:
        return False
