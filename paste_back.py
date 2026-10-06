import time
import logging
import ctypes
from ctypes import wintypes
import pyperclip
import uiautomation as auto
import wechat_window
from pynput.keyboard import Controller as KeyboardController, Key

log = logging.getLogger("polish")
_kb = KeyboardController()

# ---- 可靠的“把窗口提到前台”所需的 Windows API ----
# 64 位下 HWND 是 64 位指针，ctypes 默认按 c_int 处理会截断，必须显式声明。
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

GWL_STYLE = -16
WS_MINIMIZE = 0x20000000
SW_RESTORE = 9
ASFW_ANY = 0xFFFFFFFF
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040

user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wintypes.LONG
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.SetWindowPos.restype = wintypes.BOOL
user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
user32.AllowSetForegroundWindow.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

WECHAT_CLASS = "WeChatMainWndForPC"


def _force_foreground(hwnd):
    """可靠地把目标窗口提到前台并抢回焦点（绕过 Windows 对后台线程 SetForegroundWindow 的限制）。

    关键点：在后台/非前台线程里直接调用 SetForegroundWindow 会静默失败（返回 0），
    导致后续 Ctrl+V 落到别的窗口 → 看起来“没有回填”。这里用标准做法：
      1) AllowSetForegroundWindow(ASFW_ANY) 放开前台限制；
      2) 若窗口被最小化则先 Restore；
      3) AttachThreadInput 把本线程输入挂到“当前前台窗口线程”上，
         这样 SetForegroundWindow 不会被拦截，焦点也会随之转移。
    """
    if not hwnd:
        return
    try:
        user32.AllowSetForegroundWindow(ASFW_ANY)

        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        if style & WS_MINIMIZE:
            user32.ShowWindow(hwnd, SW_RESTORE)

        fg = user32.GetForegroundWindow()
        cur = kernel32.GetCurrentThreadId()
        if fg:
            pid = wintypes.DWORD()
            fg_thread = user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
            if fg_thread and fg_thread != cur:
                user32.AttachThreadInput(cur, fg_thread, True)
                try:
                    user32.SetForegroundWindow(hwnd)
                    user32.SetWindowPos(
                        hwnd, 0, 0, 0, 0, 0,
                        SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW,
                    )
                finally:
                    user32.AttachThreadInput(cur, fg_thread, False)
                return

        # fg 为空或本线程就是前台线程：直接置前
        user32.SetForegroundWindow(hwnd)
    except Exception as e:
        log.warning(f"强制前台失败，回退到 uiautomation: {e}")
        try:
            auto.SetForegroundWindow(hwnd)
        except Exception:
            pass


def _find_wechat_window(preferred_hwnd=None):
    """定位微信主窗口控件。

    优先用类名定位（最可靠），因为触发来源可能是：
      - 按热键：触发热键时前台是微信，target_hwnd 正确；
      - 点右下角动图：触发瞬间前台是动图窗口/桌面，target_hwnd 指向错误窗口，
        若直接信任它，_force_foreground 会把错误的窗口提到前台，Ctrl+V 无处可去。
    因此一律用类名重新定位微信，仅当类名找不到时再回退到传入的 hwnd。
    """
    # 统一走 wechat_window（按进程名+标题定位，兼容微信 3.x / 4.x），
    # 不再按固定类名搜索 —— 4.x 用 Qt 重写后类名会随 Qt 版本变化。
    try:
        win = wechat_window.find_wechat_control()
        if win is not None:
            return win
    except Exception as e:
        log.warning(f"定位微信窗口失败: {e}")

    if preferred_hwnd:
        try:
            w = auto.ControlFromHandle(preferred_hwnd)
            if w is not None:
                return w
        except Exception:
            pass
    return None


def _focus_wechat_input(win, retries=2):
    """把键盘焦点显式设到微信“消息输入框”这一 Edit 上。

    仅把微信窗口提到前台还不够：焦点可能落在聊天列表/联系人列表上，
    此时 Ctrl+A/Ctrl+V 会选中/粘贴到错误位置甚至无效。必须显式聚焦输入框。

    性能注意：微信控件经 UI Automation 枚举本身较慢，因此只**定位一次**
    （不每次重试都重新枚举），随后仅对同一个 Edit 做 SetFocus 重试，
    避免累计出数百毫秒甚至更久的等待，保证点击发送后近乎即时回填。
    """
    for i in range(retries):
        try:
            if wechat_window.focus_input(win):
                time.sleep(0.03)
                return True
        except Exception as e:
            log.warning(f"聚焦输入框失败: {e}")
        time.sleep(0.03)
    return False


def _hwnd_of(win):
    try:
        return int(win.NativeWindowHandle)
    except Exception:
        return None


def paste_back(text, target_hwnd=None, delay=0.05):
    """把 text 回填并粘贴到微信消息输入框：定位微信 → 聚焦输入框 → Ctrl+A+Ctrl+V。

    关键修复（解决“有时回填、有时不回填”）：
      1) 不信任触发时记录的 target_hwnd，改为按类名重新定位微信主窗口；
      2) 显式把键盘焦点设到“消息输入框”Edit 控件，再做 Ctrl+A+Ctrl+V，

    延迟优化：剪贴板写入是同步的、UIA 只枚举一次、各步仅留极小 settle，
    点击发送后文本几乎即时出现在微信输入框。
    """
    try:
        original = pyperclip.paste()
    except Exception:
        original = ""

    win = _find_wechat_window(target_hwnd)
    hwnd = _hwnd_of(win) if win is not None else None
    if not hwnd and target_hwnd:
        hwnd = target_hwnd

    # 1) 把微信窗口提到前台（用类名定位到的真实微信句柄）
    _force_foreground(hwnd)
    time.sleep(0.02)  # 仅给前台切换一个极短 settle

    # 2) 显式聚焦消息输入框（核心修复点）
    if win is not None:
        _focus_wechat_input(win)
    else:
        log.warning("未定位到微信窗口，尝试直接粘贴到当前前台窗口")

    # 3) 写入剪贴板后粘贴；剪贴板写入是同步的，只需极短等待
    try:
        pyperclip.copy(text)
    except Exception as e:
        log.warning(f"写入剪贴板失败: {e}")
    time.sleep(delay)

    try:
        # 先全选输入框内容，再粘贴，确保覆盖原草稿（空草稿时即插入）
        with _kb.pressed(Key.ctrl):
            _kb.press("a")
            _kb.release("a")
        time.sleep(0.03)
    except Exception as e:
        log.warning(f"全选失败: {e}")

    try:
        with _kb.pressed(Key.ctrl):
            _kb.press("v")
            _kb.release("v")
    except Exception as e:
        log.warning(f"发送 Ctrl+V 失败: {e}")

    # 给微信留出读取剪贴板的时间，再还原用户原剪贴板（过短会导致粘贴成旧内容）
    time.sleep(0.12)
    # 还原用户原来的剪贴板
    try:
        pyperclip.copy(original)
    except Exception:
        pass
    log.info("已回填并粘贴选定版本")
