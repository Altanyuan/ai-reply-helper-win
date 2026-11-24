"""定时发送的底层通道：搜索联系人 → 切换会话 → 输入 → 发送 → 校验。

思路移植自 `wxsend.py`（另一会话里用命令行验证过的脚本），改成会聊的模块形态：
抓帧/定位/OCR 复用 `capture_wgc`，输入复用 `paste_back`，不再单独引一套依赖。

它和「自动回复」的区别：
  · 自动回复只处理当前打开的会话；这里要**主动搜索并切换**到目标会话。
  · 自动回复发完不管；这里每一步都要回读校验，最后还要确认气泡出现。

安全红线（按顺序）：
  1. 搜索结果必须落在「联系人」分区（同名的群聊/公众号不算）
  2. 多个精确同名 → 放弃，不猜
  3. 进入会话后标题栏必须等于目标 → 否则放弃
  4. 输入完必须回读输入框 → 对不上就不按回车
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
import time
from ctypes import wintypes

import numpy as np
from pynput.keyboard import Controller as KeyboardController, Key
from pynput.mouse import Button, Controller as MouseController

import capture_wgc
import ocr_reader
import paste_back
import pyperclip
import wechat_window

log = logging.getLogger("polish")

_kb = KeyboardController()
_mouse = MouseController()
user32 = ctypes.windll.user32

# 搜索结果里的分区标题。第一项是网页联想，必须排除，否则会点到「搜一搜」建议上。
SECTION_PATTERNS = [
    ("网络搜索", ("网络搜索", "搜索网络", "搜一搜", "搜索结果")),
    ("联系人", ("联系人",)),
    ("群聊", ("群聊",)),
    ("聊天记录", ("聊天记录",)),
    ("收藏", ("收藏",)),
    ("公众号", ("公众号",)),
    ("功能", ("功能",)),
    ("最常使用", ("最常使用",)),
]

_PUNCT = r"[\s\u3000·.,，。:：;；()（）\[\]【】<>《》@_\-—\*\|]+"


class SendError(RuntimeError):
    """某一步校验没过。消息按 stage 定位是哪一步。"""

    def __init__(self, stage: str, detail: str):
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail


def norm(s: str) -> str:
    return re.sub(_PUNCT, "", str(s or "")).lower()


def section_of(text: str) -> str | None:
    nt = norm(text)
    if not nt or len(nt) > 9:
        return None
    for name, keys in SECTION_PATTERNS:
        if any(k in nt for k in keys):
            return name
    return None


# ---------------- 窗口与点击 ----------------

class _RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def client_origin(hwnd: int) -> tuple[int, int]:
    """窗口客户区左上角的屏幕坐标。

    抓帧拿到的是客户区图像（WGC 不含系统标题栏），点击要把帧内坐标加上这个原点。
    这里用 ClientToScreen 而不是 GetWindowRect：后者含边框，微信 4.x 又是无边框窗口，
    两者在我们这里恰好相同，但换到经典边框窗口就会偏一条标题栏。
    """
    pt = _POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def click_at(hwnd: int, x: int, y: int, settle: float = 0.25):
    """在窗口客户区坐标 (x, y) 处点击。

    时序比想象中重要（实测）：先把光标移过去、停 0.18 秒让目标窗口更新 hover 状态，
    再分 press/release。直接 `_mouse.click()`（移完立刻按）在列表项上会"点了个空"——
    画面有反应（搜索面板关掉）但没选中会话。
    """
    ox, oy = client_origin(hwnd)
    _mouse.position = (ox + int(x), oy + int(y))
    time.sleep(0.18)
    _mouse.press(Button.left)
    time.sleep(0.06)
    _mouse.release(Button.left)
    time.sleep(settle)


def press_key(key):
    _kb.press(key)
    _kb.release(key)


def hotkey_ctrl(letter: str):
    with _kb.pressed(Key.ctrl):
        _kb.press(letter)
        _kb.release(letter)


# ---------------- 主类 ----------------

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010


class WeChatSender:
    def __init__(self, shot_dir: str | None = None):
        self.shot_dir = shot_dir
        self.hwnd = 0
        self._saved_rect = None

    # ---- 窗口尺寸 ----
    def _ensure_window_size(self):
        """窗口太小时把它撑到能正常布局的尺寸，返回原矩形供还原。

        为什么要这一步（2026-11-24 实测）：微信缩到 294x387 时，会话列表（固定约 300px）
        占满整个窗口，聊天区被挤没，搜索框/结果面板的比例全失效，搜索必定失败。
        """
        r = _RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if w >= 820 and h >= 620:
            return None
        target_w, target_h = 1000, 720
        try:
            sw = user32.GetSystemMetrics(0)
            sh = user32.GetSystemMetrics(1)
            target_w = min(target_w, max(820, sw - 120))
            target_h = min(target_h, max(620, sh - 160))
        except Exception:
            pass
        self._saved_rect = (r.left, r.top, w, h)
        log.info("[定时任务] 微信窗口 %dx%d 太小，临时撑到 %dx%d", w, h, target_w, target_h)
        user32.SetWindowPos(self.hwnd, 0, r.left, r.top, target_w, target_h,
                            SWP_NOZORDER | SWP_NOACTIVATE)
        time.sleep(0.8)
        return self._saved_rect

    def _restore_window_size(self):
        if not self._saved_rect:
            return
        try:
            x, y, w, h = self._saved_rect
            user32.SetWindowPos(self.hwnd, 0, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE)
            time.sleep(0.3)
        except Exception:
            pass
        self._saved_rect = None

    # ---- 基础：抓帧 + OCR ----
    def _grab(self):
        self.hwnd = wechat_window.find_wechat_hwnd() or 0
        if not self.hwnd:
            raise SendError("窗口", "找不到微信窗口（可能缩在托盘里或未登录）")
        full, area = capture_wgc.grab_frame(self.hwnd)
        if full is None or area is None:
            raise SendError("抓帧", "拿不到微信画面（窗口不可见或被最小化）")
        return full, area

    def _ocr(self, full, region=None):
        from PIL import Image
        img = full
        if region is not None:
            x0, y0, x1, y1 = [int(v) for v in region]
            img = full[max(0, y0):y1, max(0, x0):x1]
        boxes, _info = ocr_reader.recognize_boxes_scaled(Image.fromarray(img))
        out = []
        for text, rect in boxes or []:
            x0, y0, x1, y1 = rect
            if region is not None:
                x0 += region[0]
                x1 += region[0]
                y0 += region[1]
                y1 += region[1]
            cx, cy = (int(x0) + int(x1)) // 2, (int(y0) + int(y1)) // 2
            out.append((str(text).strip(), (cx, cy), (int(x0), int(y0), int(x1), int(y1))))
        return out

    def _save_shot(self, full, tag: str) -> str:
        if not self.shot_dir:
            return ""
        try:
            from PIL import Image
            os.makedirs(self.shot_dir, exist_ok=True)
            path = os.path.join(self.shot_dir, f"{tag}_{time.strftime('%Y%m%d_%H%M%S')}.png")
            Image.fromarray(full).save(path)
            return path
        except Exception:
            return ""

    # ---- 会话标题 ----
    def _title(self, full, area) -> str:
        boxes, _info = capture_wgc.recognize_chat_header(full, area)
        hx0, hx1 = capture_wgc.header_crop_bounds(full.shape[1], area[0])
        return (capture_wgc.select_chat_title(boxes, hx1 - hx0) or "").strip()

    # ---- 搜索 ----
    def _locate_search_box(self, full, area):
        """用 OCR 找搜索框里那两个字，拿它的中心点。

        为什么不用比例（实测）：窗口 984x712 时按 0.062 算出的 y=44，
        而搜索框实际在 y≈71 —— 点偏 27px 就落在搜索框上方的空白里，
        点击无效、Ctrl+V 粘不进去，表现为「搜索框始终没收到输入」。
        """
        h = full.shape[0]
        try:
            items = self._ocr(full, (0, 0, max(80, int(area[0])), int(h * 0.15)))
        except Exception:
            items = []
        for text, (cx, cy), _r in items:
            t = str(text or "")
            if "搜索" in t or t.strip().upper().endswith("SEARCH"):
                return int(cx), int(cy)
        return None

    def _search(self, target: str):
        """点搜索框 → 输入名字 → 回读确认真的输进去了。返回 (full, area, items)。"""
        full, area = self._grab()
        list_right = int(area[0])
        height = full.shape[0]
        for attempt in (1, 2):
            full, area = self._grab()
            list_right = int(area[0])
            height = full.shape[0]
            pt = self._locate_search_box(full, area)
            if pt is None:
                pt = (int(list_right * 0.5), int(height * 0.10))
            click_at(self.hwnd, pt[0], pt[1], settle=0.45)
            hotkey_ctrl("a")            # 覆盖上一次的搜索词
            time.sleep(0.12)
            # 这里不能用 paste_back：它内部会把焦点设到「消息输入框」，
            # 中文也会被粘到聊天框而不是搜索框。搜索框只需要剪贴板 + Ctrl+V。
            try:
                pyperclip.copy(target)
            except Exception:
                raise SendError("搜索", "无法写入剪贴板")
            hotkey_ctrl("v")
            time.sleep(1.6)

            full, area = self._grab()
            # 只 OCR 左栏（比整帧快 3 倍），坐标仍按整帧算
            items = self._ocr(full, (0, 0, max(80, int(area[0])), full.shape[0]))
            if norm(target) in norm(self._search_box_text(items, area, height)):
                # 输入进去了，还要确认结果面板真的出来了（有分区标题）
                if not any(section_of(t) for t, _, _ in items):
                    log.info("[定时任务] 搜索面板还没出现，按回车触发一次")
                    press_key(Key.enter)
                    time.sleep(1.4)
                    full, area = self._grab()
                    items = self._ocr(full, (0, 0, max(80, int(area[0])), full.shape[0]))
                return full, area, items
            if attempt == 1:
                log.info("[定时任务] 搜索框没收到输入，重试一次")
                wechat_window.wake_accessibility(self.hwnd, rounds=1, interval=0.1)
                time.sleep(0.4)
        raise SendError("搜索", f"搜索框始终没收到「{target}」（微信可能卡住）")

    def _search_box_text(self, items, area, height) -> str:
        """取搜索框那一行的文字。

        阈值用 0.16 而不是 0.10：实测搜索框文字中心在 y≈71，而 0.10*712=71.2，
        恰好卡在边界上，抖动一点就判成「没输入」，导致搜索失败重试两次后放弃。
        """
        edge = int(area[0])
        sel = [i for i in items if i[1][1] < 0.16 * height and i[1][0] < edge]
        sel.sort(key=lambda i: i[1][0])
        return " ".join(i[0] for i in sel).strip()

    # ---- 结果面板 ----
    def _parse_results(self, query: str, items, area, height):
        """返回 [(section, name, rank, (x, y))]。只解析会话列表区域。"""
        edge = int(area[0])
        # 下界 0.13：搜索框在 y≈0.10~0.11 的位置，它里面的关键词会被 OCR 当成候选
        # （实测 "Halo I" 就是搜索框文字，rank=2，会污染候选集）
        pool = [i for i in items if i[1][0] < edge - 4 and 0.13 * height < i[1][1] < 0.92 * height]
        if not pool:
            return []
        pool.sort(key=lambda i: i[1][1])
        headers = [(section_of(text), cy) for text, (cx, cy), _r in pool if section_of(text)]
        nq = norm(query)
        found = []
        for text, (cx, cy), _rect in pool:
            if section_of(text):
                continue
            nt = norm(text)
            if not nt:
                continue
            if nt == nq:
                rank = 3
            elif nt.startswith(nq):
                rank = 2
            elif nq in nt:
                rank = 1
            else:
                continue
            section = ""
            for sname, sy in headers:
                if sy < cy:
                    section = sname
            found.append((section, text, rank, (cx, cy)))
        return found

    def _pick_friend(self, found, target: str):
        """只在「联系人」分区里挑；精确 > 前缀；多个精确同名直接放弃。"""
        cands = [f for f in found if f[0] == "联系人"]
        if not cands:
            # OCR 有时读不出「联系人」这几个字（实测：小字号 + 灰色），
            # 这时所有条目都归属不到分区。退化为「精确/前缀匹配且唯一」才接受：
            #   · rank=1（包含）一律不要 —— "Halo: 小程序..." 这种聊天记录就是 rank=1
            #   · 唯一性必须成立，多个前缀候选说明拿不准
            # 即便选错，后面还有「标题栏必须等于目标」这道闸兜底。
            loose = [f for f in found if f[2] >= 2]
            if len(loose) == 1:
                log.info("[定时任务] 没识别到「联系人」分区，按唯一前缀候选：%s", loose[0][1])
                return loose[0]
            seen = "、".join(sorted({f[1] for f in found})) or "（空）"
            raise SendError("搜索", f"「联系人」分区里没有「{target}」，结果里只有：{seen}")
        cands.sort(key=lambda f: (-f[2], f[3][1]))
        best = cands[0]
        if best[2] == 3:
            same = [c for c in cands if c[2] == 3]
            if len(same) > 1:
                names = "、".join(c[1] for c in same)
                raise SendError("搜索", f"「联系人」里有多个「{target}」同名结果（{names}），不猜")
        elif best[2] == 1:
            raise SendError("搜索", f"只有包含「{target}」的结果（{best[1]}），不是精确匹配，放弃")
        return best

    # ---- 输入框 ----
    def _focus_input(self):
        win = wechat_window.find_wechat_control()
        if win is not None:
            wechat_window.focus_input(win)
        time.sleep(0.3)

    def _clear_input(self):
        self._focus_input()
        hotkey_ctrl("a")
        time.sleep(0.08)
        press_key(Key.delete)
        time.sleep(0.2)

    def _read_input(self, full, area) -> str:
        """读输入框内容。

        区域用窗口底部比例，不用 area[3]：实测消息区的下边界会压到输入框里
        （area[3]=706、窗口高 712），从那儿往下只剩几像素，只能 OCR 到底部工具栏，
        读到「发送 / 表情按钮」这种字样，核对必然失败。
        """
        h = full.shape[0]
        y0 = int(h * 0.72)
        y1 = int(h * 0.94)
        region = (int(area[0]), y0, int(area[2]), y1)
        items = self._ocr(full, region)
        items.sort(key=lambda i: (i[1][1], i[1][0]))
        return " ".join(i[0] for i in items).strip()

    # ---- 气泡确认 ----
    def _bubble_has(self, full, area, needle: str) -> bool:
        probe = norm(needle)[:max(4, min(8, len(norm(needle))))]
        if not probe:
            return False
        region = (int(area[0]), int(area[1]), int(area[2]), int(area[3]))
        items = self._ocr(full, region)
        blob = norm(" ".join(i[0] for i in items))
        return probe in blob

    # ================= 主流程 =================

    def send_to(self, target: str, text: str, dry_run: bool = False,
                tag: str = "task") -> tuple[bool, str, str]:
        """给 target 发 text。

        返回 (是否成功, 说明, 截图路径)。dry_run=True 时走到输入框就停，不按回车。
        """
        target = (target or "").strip()
        text = (text or "").strip()
        if not target or not text:
            return False, "目标和内容不能为空", ""

        try:
            # 1) 找窗口 + 激活 + 保证窗口够大
            full, area = self._grab()
            capture_wgc.unminimize(self.hwnd)
            user32.SetForegroundWindow(self.hwnd)
            time.sleep(0.5)
            self._ensure_window_size()

            # 2) 已经是目标会话就不用搜
            title = self._title(full, area)
            if norm(title) == norm(target):
                log.info("[定时任务] 当前会话就是「%s」，跳过搜索", target)
                shot = self._save_shot(full, tag + "_current")
            else:
                # 3) 搜索
                full, area, items = self._search(target)
                found = self._parse_results(target, items, area, full.shape[0])
                section, name, _rank, (cx, cy) = self._pick_friend(found, target)
                log.info("[定时任务] 搜索「%s」→ 命中「%s」分区：%s", target, section, name)

                # 4) 点进会话。
                #    注意：搜索框有焦点时，**第一次点击只是让搜索框失焦**，不会选中会话
                #    （实测 2026-11-24：点完停在普通会话列表、右侧空白，标题读成「未识别」）。
                #    所以点一次后要校验，没进去就补一次。
                #    处理办法：先点聊天区空白让搜索框失焦，再点目标行。
                blank_x = int((area[0] + area[2]) / 2)
                blank_y = int((area[1] + area[3]) / 2)
                click_at(self.hwnd, blank_x, blank_y, settle=0.5)
                time.sleep(0.35)
                click_at(self.hwnd, cx, cy, settle=1.2)
                full, area = self._grab()
                if norm(self._title(full, area)) != norm(target):
                    log.info("[定时任务] 点击后没进入会话，再点一次目标行")
                    click_at(self.hwnd, cx, cy, settle=1.3)
                    full, area = self._grab()
                if norm(self._title(full, area)) != norm(target):
                    log.info("[定时任务] 仍没进入，尝试回车确认")
                    press_key(Key.enter)
                    time.sleep(1.2)
                    full, area = self._grab()
                shot = self._save_shot(full, tag + "_opened")

            # 5) 校验会话
            title = self._title(full, area)
            if norm(title) != norm(target):
                raise SendError("会话校验", f"期望「{target}」，实际「{title or '未识别'}」，已放弃")

            # 6) 清空输入框
            self._clear_input()

            # 7) 写内容
            paste_back.paste_back(text, target_hwnd=self.hwnd)
            time.sleep(0.6)

            # 8) 回读输入框
            full, area = self._grab()
            shot = self._save_shot(full, tag + "_typed")
            got = self._read_input(full, area)
            head = norm(text)[:20]
            if head and head not in norm(got):
                raise SendError("输入核对", f"输入框内容不符（期望前 20 字「{text[:20]}」，实际「{got[:40]}」）")

            if dry_run:
                self._clear_input()
                return True, f"试运行通过：会话「{title}」，内容 {len(text)} 字，输入框已复原（未发送）", shot

            # 9) 发送
            press_key(Key.enter)
            time.sleep(1.2)
            full, area = self._grab()
            left = self._read_input(full, area)
            if left:
                # 可能设成了 Ctrl+Enter 发送
                with _kb.pressed(Key.ctrl):
                    press_key(Key.enter)
                time.sleep(1.0)
                full, area = self._grab()

            # 10) 气泡确认
            shot = self._save_shot(full, tag + "_sent")
            if self._bubble_has(full, area, text):
                return True, f"已发送并确认气泡（会话「{title}」）", shot
            time.sleep(1.6)
            full, area = self._grab()
            shot = self._save_shot(full, tag + "_sent2")
            if self._bubble_has(full, area, text):
                return True, f"已发送并确认气泡（会话「{title}」）", shot
            return True, "已按回车发送，但没在聊天区读到气泡（可能被折叠或 OCR 漏读）", shot

        except SendError as e:
            return False, f"{e.stage}失败：{e.detail}", ""
        except Exception as e:
            log.exception("定时发送异常")
            return False, f"异常：{type(e).__name__}: {e}", ""
        finally:
            self._restore_window_size()
