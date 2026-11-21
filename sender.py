"""自动回复的「发送通道」：前台校验 → 空输入框校验 → 回填 → 回车。

为什么不直接复用 paste_back：
  paste_back 内部会 `_force_foreground` 把微信拉到前台（热键场景需要这样），
  但自动回复按用户设定「不抢焦点」——微信不在前台时这一轮直接放弃。
  所以先由本模块确认微信已在前台，再调用 paste_back（此时它的提前台是无害空操作）。

设计红线：
  · 绝不主动激活/切换窗口；
  · 绝不在输入框有内容时粘贴（会覆盖用户正在写的字）；
  · 发送前把「会话名 / 空输入框 / 前台 / 内容自检」四道闸都过一遍。
"""
from __future__ import annotations

import ctypes
import logging
import os
import time
from ctypes import wintypes

from pynput.keyboard import Controller as KeyboardController, Key

import capturer
import paste_back
import wechat_window

log = logging.getLogger("polish")
_kb = KeyboardController()

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
kernel32.GetTickCount.restype = wintypes.DWORD
user32.GetLastInputInfo.argtypes = [ctypes.c_void_p]
user32.GetLastInputInfo.restype = wintypes.BOOL


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds() -> float:
    """距离上一次键盘/鼠标操作过了多少秒。拿不到时返回一个很大的值。"""
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return 9999.0
        now = kernel32.GetTickCount() & 0xFFFFFFFF
        return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return 9999.0


def _pid_of(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def wechat_state() -> tuple[str, int | None, str]:
    """返回 (状态, hwnd, 人话原因)。

    状态：foreground / background / minimized / missing
    比对进程 ID 而不是窗口句柄——微信可能有多个顶层窗口（主窗、独立聊天窗）。
    """
    hwnd = wechat_window.find_wechat_hwnd()
    if not hwnd:
        return "missing", None, "找不到微信窗口（可能缩在托盘里）"
    if user32.IsIconic(hwnd):
        return "minimized", hwnd, "微信窗口最小化了"
    fg = user32.GetForegroundWindow()
    if not fg:
        return "background", hwnd, "拿不到前台窗口"
    fg_pid = _pid_of(fg)
    if fg_pid == _pid_of(hwnd):
        return "foreground", hwnd, ""
    # 分两种说清楚：挡在前面的是我们自己，还是别的程序
    if fg_pid == os.getpid():
        return "background", hwnd, "会聊自己的窗口在前面（关掉设置页，或点一下微信再等）"
    name = ""
    try:
        name = wechat_window._proc_name(fg_pid) or ""
    except Exception:
        name = ""
    return "background", hwnd, f"微信不在前台（当前前台：{name or '其他程序'}）"


def read_input_text() -> str:
    """读输入框内容（用于判断是否为空）。

    capture_draft 会 Ctrl+A 全选来复制，读完必须把光标收回到末尾，
    否则用户回来一打字就会把选中的内容整段替换掉。
    """
    text = ""
    try:
        text = capturer.capture_draft() or ""
    except Exception as e:
        log.warning("读取输入框草稿失败：%s", e)
    try:
        _kb.press(Key.end)
        _kb.release(Key.end)
    except Exception:
        pass
    return text.strip()


def send_text(text: str, hwnd: int | None = None, ctrl_enter: bool = False) -> bool:
    """回填并回车发送。返回是否发出（不代表对方收到）。"""
    text = (text or "").strip()
    if not text:
        return False
    try:
        paste_back.paste_back(text, target_hwnd=hwnd)
    except Exception as e:
        log.warning("回填失败：%s", e)
        return False

    time.sleep(0.35)
    try:
        if ctrl_enter:
            with _kb.pressed(Key.ctrl):
                _kb.press(Key.enter)
                _kb.release(Key.enter)
        else:
            _kb.press(Key.enter)
            _kb.release(Key.enter)
        return True
    except Exception as e:
        log.warning("回车发送失败：%s", e)
        return False


# 模型偶尔会漏出这些痕迹，发出去很尴尬
_LEAK_MARKS = ("[[", "]]", '{"', '"tasks"', "提示词", "系统设定", "作为 AI", "作为AI", "我无法")


def self_check(reply: str, incoming: str) -> tuple[bool, str]:
    """闸 4：发送前的内容自检。"""
    t = (reply or "").strip()
    if len(t) < 2:
        return False, "回复太短"
    if incoming and t == (incoming or "").strip():
        return False, "复读了对方原话"
    for mark in _LEAK_MARKS:
        if mark in t:
            return False, f"含疑似提示词痕迹：{mark}"
    return True, ""
