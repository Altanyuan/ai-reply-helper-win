"""自动回复调度器。

一句话：盯着微信当前聊天，按「每个人自己的开关 + 风格」自动生成回复并发送。

为什么是一个独立线程而不是挂在热键上：
  热键是「用户主动要一次结果」，自动回复是「后台持续盯梢」，两者的生命周期、
  失败策略完全不同。混在一起会让热键变慢、也会让自动回复的限流/静默逻辑污染主流程。

安全策略（按用户确认的口径）：
  · 不抢焦点：微信不在前台就整轮跳过，绝不 SetForegroundWindow；
  · 不覆盖草稿：输入框里有字就不发；
  · 不重复：同一条消息只回一次，指纹持久化，重启后仍然有效；
  · 不失控：静默时段、每小时上限、开启后自动关闭、连续失败自动关闭。
"""
from __future__ import annotations

import json
import logging
import random
import unicodedata
import threading
import time
from collections import deque
from pathlib import Path

import ai_client
import capturer
import config
import paths
import sender
import wechat_window
from contact_rules import resolve_prompt_id

log = logging.getLogger("polish")

_STATE_FILE = "auto_reply_state.json"
_REPLIED_KEEP = 20


# ---------------- 纯函数（好测） ----------------

def parse_context(text: str) -> list[tuple[str, str, str]]:
    """把 format_context 的输出解析成 [(who, name, text)]，who ∈ me / them。

    格式来自 capture_wgc.format_context：
        我：xxx
        对方（昵称）：xxx     ← 群聊
        对方：xxx
    """
    out: list[tuple[str, str, str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("我："):
            out.append(("me", "", line[2:].strip()))
        elif line.startswith("对方（"):
            rest = line[len("对方（"):]
            end = rest.find("）：")
            if end > 0:
                out.append(("them", rest[:end].strip(), rest[end + 2:].strip()))
            else:
                out.append(("them", "", rest.strip()))
        elif line.startswith("对方："):
            out.append(("them", "", line[3:].strip()))
    return out


def fingerprint(title: str, who: str, text: str, count: int) -> str:
    """一条消息的指纹：会话 + 说话人 + 内容 + 条数。用于去重。（朴素实现，不引 hashlib 依赖）"""
    raw = f"{title}|{who}|{text.strip()}|{count}"
    h = 0
    for ch in raw:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFFFFFF
    return f"{h:012x}"


def in_quiet_hours(start: str, end: str, now: time.struct_time | None = None) -> bool:
    """静默时段判断，支持跨零点（22:00~08:00）。格式非法时视为不静默。"""
    def to_min(s: str) -> int | None:
        try:
            hh, mm = str(s).strip().split(":")
            return int(hh) * 60 + int(mm)
        except Exception:
            return None

    a, b = to_min(start), to_min(end)
    if a is None or b is None or a == b:
        return False
    t = now or time.localtime()
    cur = t.tm_hour * 60 + t.tm_min
    if a < b:
        return a <= cur < b
    return cur >= a or cur < b


def normalize_name(text: str) -> str:
    """名字归一化后再比对。

    为什么要做（实测会遇到）：微信昵称里常见全角字母/数字，OCR 也可能把
    半角读成全角（「ＡＢＣ」vs「ABC」），大小写也可能不一致。逐字符相等
    会导致「明明看着一样，就是匹配不上」，表现为这个人的自动回复永远不生效。
    NFKC 把全角压成半角，casefold 抹掉大小写差异。
    """
    return unicodedata.normalize("NFKC", str(text or "")).strip().casefold()


def match_rule(ar: dict, title: str) -> dict | None:
    """在自动回复名单里匹配会话名（归一化后比较，重名不猜）。"""
    name = normalize_name(title)
    if not name:
        return None
    hit = None
    for rule in ar.get("contacts") or []:
        if not isinstance(rule, dict):
            continue
        if normalize_name(rule.get("name")) == name:
            if hit is not None:
                return None  # 重名 → 不猜，直接放过（宁可不回，也不要回错人）
            hit = rule
    return hit


# ---------------- 调度器 ----------------

class AutoReplier:
    def __init__(self, notify=None):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._notify = notify or (lambda msg, kind="info": None)

        self._lock = threading.RLock()
        self._replied: deque[str] = deque(maxlen=_REPLIED_KEEP)
        self._enabled_at = 0.0
        self._paused_until = 0.0
        self._active_contact = ""
        self._last_action = ""
        self._last_reason = "未启动"
        self._fail_streak = 0
        self._day = time.strftime("%Y-%m-%d")
        self._day_count: dict[str, int] = {}
        self._hour_hits: dict[str, deque] = {}

        self._load_state()

    # ---- 状态持久化（只存指纹，不存聊天内容）----
    def _state_path(self) -> Path:
        return Path(paths.data_dir()) / _STATE_FILE

    def _load_state(self):
        try:
            data = json.loads(self._state_path().read_text(encoding="utf-8"))
            self._replied = deque(list(data.get("replied") or [])[-_REPLIED_KEEP:], maxlen=_REPLIED_KEEP)
            day = data.get("day") or ""
            if day == self._day:
                counts = data.get("day_count") or {}
                if isinstance(counts, dict):
                    self._day_count = {str(k): int(v) for k, v in counts.items()}
        except Exception:
            pass

    def _save_state(self):
        try:
            payload = {"replied": list(self._replied), "day": self._day, "day_count": self._day_count}
            self._state_path().write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except Exception as e:
            log.warning("保存自动回复状态失败：%s", e)

    # ---- 生命周期 ----
    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._enabled_at = time.time()
            self._paused_until = 0.0
            self._thread = threading.Thread(target=self._loop, name="auto-reply", daemon=True)
            self._thread.start()
        log.info("自动回复已启动")
        self._last_reason = "刚启动，等待第一轮"

    def stop(self):
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2)
        self._thread = None
        log.info("自动回复已停止")
        self._last_reason = "已关闭"

    def resume(self):
        """取消暂停（用户在设置页点「恢复自动回复」）。"""
        self._paused_until = 0.0
        self._last_reason = "已恢复，等下一轮"
        log.info("自动回复已恢复")

    def pause(self, seconds: int = 600):
        self._paused_until = time.time() + max(30, int(seconds))
        log.info("自动回复暂停 %s 秒", seconds)
        self._last_reason = f"已暂停 {max(30, int(seconds)) // 60} 分钟"

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict:
        now = time.time()
        return {
            "running": self.running,
            "paused_for": max(0, int(self._paused_until - now)) if self._paused_until > now else 0,
            "contact": self._active_contact,
            "today_count": sum(self._day_count.values()),
            "by_contact": dict(self._day_count),
            "last_action": self._last_action,
            "last_reason": self._last_reason,
        }

    # ---- 主循环 ----
    def _interval(self) -> int:
        try:
            ar = (config.load_config().get("auto_reply") or {})
            return max(15, int(ar.get("interval_sec") or 60))
        except Exception:
            return 60

    def _loop(self):
        """调度线程。

        必须在本线程里初始化 COM：capturer 会先走 UIA 路径（uiautomation），
        而 UIA 依赖 COM；子线程没 CoInitialize 时会抛
        `[WinError -2147221008] 尚未调用 CoInitialize`，然后被吞掉、
        只剩「未找到微信窗口」，表现为 UIA 路径永远失效
        （微信 3.x 只能走 UIA，那它就读不到任何消息）。
        """
        ui = None
        try:
            import uiautomation as _uia
            ui = _uia.UIAutomationInitializerInThread()
            ui.__enter__()
        except Exception as e:
            log.warning("自动回复线程初始化 UIA 失败（仅影响 UIA 路径）：%s", e)
            ui = None
        try:
            while not self._stop.is_set():
                try:
                    self._tick()
                except Exception:
                    log.exception("自动回复循环异常")
                # 用 wait 而不是 sleep：停止时能立刻醒来
                self._stop.wait(self._interval())
        finally:
            if ui is not None:
                try:
                    ui.__exit__(None, None, None)
                except Exception:
                    pass

    def tick_once(self, delay_override: float | None = 3.0):
        """手动触发一轮（设置页的「立即试一次」）。默认把随机延迟压到 3 秒，便于验证。"""
        try:
            self._tick(delay_override=delay_override)
        except Exception:
            log.exception("手动触发一轮失败")

    def _say(self, reason: str):
        """记录本轮判断。只在结论变化时写日志，避免每 60 秒刷一行。"""
        if reason != self._last_reason:
            log.info("[自动回复] %s", reason)
        self._last_reason = reason

    # ---- 一轮 ----
    def _tick(self, delay_override: float | None = None):
        cfg = config.load_config()
        ar = cfg.get("auto_reply") or {}

        if not ar.get("enabled"):
            self._say("总开关未开")
            return
        now = time.time()
        if now < self._paused_until:
            left = max(1, int((self._paused_until - now) // 60) + 1)
            self._say(f"暂停中，还有 {left} 分钟（可点「恢复自动回复」立即继续）")
            return

        auto_off = int(ar.get("auto_off_after_min") or 0)
        if auto_off and self._enabled_at and now - self._enabled_at > auto_off * 60:
            config.update_config({"auto_reply": dict(ar, enabled=False)})
            self._say("已到设定时长，自动关闭")
            self._notify("自动回复已自动关闭（到了设定的运行时长）", "warn")
            return

        qh = ar.get("quiet_hours") or []
        if len(qh) == 2 and in_quiet_hours(qh[0], qh[1]):
            self._say("静默时段，不回复")
            return

        state, hwnd, why = sender.wechat_state()
        if state != "foreground":
            # activate_window 打开时，微信在后台也允许继续（发送前会把它切到前台）
            allow_bg = bool(ar.get("activate_window")) and state == "background"
            if not allow_bg:
                self._say(why or "微信不在前台")
                return

        # 用户刚动过键鼠 → 这轮别插手（也避免 capture_draft 的 Ctrl+A 打扰他）
        if sender.idle_seconds() < 2.0:
            self._say("你正在操作，稍后再说")
            return

        n = int(ar.get("context_messages") or 10)
        try:
            text, title = capturer.read_wechat_recent_with_title(n=n, hwnd=hwnd)
        except Exception as e:
            self._say(f"读取聊天失败：{e}")
            return
        if not text or not title:
            self._say("没读到聊天内容或会话名")
            return

        rule = match_rule(ar, title)
        if rule is None:
            self._active_contact = title
            self._say(f"「{title}」不在自动回复名单里")
            return
        if not rule.get("enabled"):
            self._active_contact = title
            self._say(f"「{title}」的开关没打开")
            return

        msgs = parse_context(text)
        if not msgs:
            self._say("没解析出消息")
            return
        who, name, last = msgs[-1]
        if who != "them":
            self._say("最后一条是我发的，不用回")
            return
        if len(last) < 1:
            self._say("对方最后一条没有可读文字")
            return

        fp = fingerprint(title, who, last, len(msgs))
        if fp in self._replied:
            self._say("这条已经回过了")
            return
        if not self._rate_ok(title, ar):
            self._say("超过每小时上限，本轮跳过")
            return

        # ---- 生成 ----
        self._active_contact = title
        run_cfg = self._cfg_for(cfg, rule, title)
        try:
            versions = ai_client.polish("", text, run_cfg)
        except Exception as e:
            self._fail_streak += 1
            self._say(f"生成失败：{e}")
            self._maybe_disable("连续生成失败")
            return
        reply = (versions[0] if versions else "").strip()
        ok, why2 = sender.self_check(reply, last)
        if not ok:
            self._say(f"内容没通过自检（{why2}），不发")
            return

        # ---- 随机延迟 ----
        delay_cfg = ar.get("reply_delay_sec") or [20, 90]
        try:
            lo, hi = float(delay_cfg[0]), float(delay_cfg[1])
        except Exception:
            lo, hi = 20.0, 90.0
        if hi < lo:
            lo, hi = hi, lo
        delay = float(delay_override) if delay_override is not None else random.uniform(lo, hi)
        self._say(f"准备回复，{delay:.0f} 秒后发送")
        if self._stop.wait(delay):
            return
        if time.time() < self._paused_until:
            self._say("期间被暂停，放弃本轮")
            return

        # ---- 闸 1：会话没变 ----
        try:
            _, title2 = capturer.read_wechat_recent_with_title(n=3, hwnd=wechat_window.find_wechat_hwnd())
        except Exception:
            title2 = None
        if title2 and title2 != title:
            self._say("会话已切换，放弃发送")
            return

        # ---- 闸 3：仍在前台 ----
        state2, hwnd2, why3 = sender.wechat_state()
        if state2 != "foreground":
            allow_bg = bool(ar.get("activate_window")) and state2 == "background"
            if not allow_bg:
                self._say(f"微信不在前台了（{why3}），放弃发送")
                return

        # ---- 闸 2：输入框是空的 ----
        draft = sender.read_input_text()
        if draft:
            self._say("输入框里有你正在写的内容，不覆盖")
            return

        # ---- 发送 ----
        if sender.send_text(reply, hwnd2, ctrl_enter=bool(ar.get("ctrl_enter")),
                            restore_focus=bool(ar.get("activate_window"))):
            self._mark_sent(fp, title, ar)
            self._fail_streak = 0
            self._last_action = f"已回复 {title}（{time.strftime('%H:%M')}）"
            log.info("自动回复已发送：%s", title)
            self._notify(f"已回复 {title}", "ok")
        else:
            self._fail_streak += 1
            self._say("发送失败")
            self._maybe_disable("连续发送失败")

    # ---- 辅助 ----
    def _cfg_for(self, cfg: dict, rule: dict, title: str) -> dict:
        c = dict(cfg)
        pid = str(rule.get("prompt_id") or "").strip()
        if not pid:
            pid, _ = resolve_prompt_id(
                title, cfg.get("contact_rules"), cfg.get("prompts"), cfg.get("active_prompt_id")
            )
        if pid:
            c["active_prompt_id"] = pid
        c["versions"] = 1  # 自动回复只要一条，不要三个版本
        return c

    def _rate_ok(self, title: str, ar: dict) -> bool:
        limit = int(ar.get("max_per_hour") or 0)
        if limit <= 0:
            return True  # 0 = 不限
        q = self._hour_hits.setdefault(title, deque())
        cutoff = time.time() - 3600
        while q and q[0] < cutoff:
            q.popleft()
        return len(q) < limit

    def _mark_sent(self, fp: str, title: str, ar: dict):
        with self._lock:
            today = time.strftime("%Y-%m-%d")
            if today != self._day:
                self._day = today
                self._day_count = {}
            self._replied.append(fp)
            self._day_count[title] = self._day_count.get(title, 0) + 1
            self._hour_hits.setdefault(title, deque()).append(time.time())
            self._save_state()

    def _maybe_disable(self, reason: str):
        if self._fail_streak < 3:
            return
        try:
            ar = config.load_config().get("auto_reply") or {}
            config.update_config({"auto_reply": dict(ar, enabled=False)})
        except Exception:
            pass
        self._say(f"{reason} 3 次，已自动关闭")
        self._notify(f"自动回复因{reason} 3 次已自动关闭", "warn")


# 运行中的实例（main 启动时赋值）。设置页通过它读实时状态，避免 config_editor 反向 import main。
CURRENT: "AutoReplier | None" = None
