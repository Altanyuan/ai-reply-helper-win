"""定时任务调度器。

与自动回复并列的第二个后台线程：自动回复是「别人说话我回应」，
这里是「到点我主动发」。两者的共同点是都要操作微信窗口，所以共用
`auto_reply.wechat_lock`，谁先拿到谁执行，另一个跳过本轮。

时间判定只有两种：
  · daily  —— 每天 HH:MM，可限定周几
  · once   —— 指定某个时刻，执行一次后自动停用

不做 cron 表达式，也不做"错过就补发一整天"：默认错过就跳过，
因为「早上好」下午三点发出去很奇怪。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

import config
import paths
from auto_reply import wechat_lock

log = logging.getLogger("polish")

# 失败后的冷却：避免每 20 秒重试把 fail_count 瞬间刷到 3
_FAIL_COOLDOWN_SEC = 300
_FAIL_LIMIT = 3


class Scheduler:
    def __init__(self, notify=None):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._notify = notify or (lambda msg, kind="info": None)

        self._lock = threading.RLock()
        self._running_ids: set[str] = set()
        self._cooldown: dict[str, float] = {}
        self._last_action = ""
        self._last_reason = "未启动"
        self._shot_dir = str(paths.data_dir() / "scheduled_shots")

    # ---- 生命周期 ----
    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
            self._thread.start()
        log.info("定时任务已启动")
        self._last_reason = "刚启动，等待扫描"

    def stop(self):
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2)
        self._thread = None
        log.info("定时任务已停止")

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict:
        tasks = config.get_scheduled().get("tasks") or []
        enabled = [t for t in tasks if t.get("enabled") and t.get("verified")]
        return {
            "running": self.running,
            "enabled_count": len(enabled),
            "total": len(tasks),
            "last_action": self._last_action,
            "last_reason": self._last_reason,
            "shot_dir": self._shot_dir,
        }

    # ---- 主循环 ----
    def _interval(self) -> int:
        try:
            return max(10, int(config.get_scheduled().get("scan_sec") or 20))
        except Exception:
            return 20

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("定时任务循环异常")
            self._stop.wait(self._interval())

    def _tick(self):
        conf = config.get_scheduled()
        if not conf.get("enabled"):
            self._last_reason = "总开关未开"
            return

        now = datetime.now()
        tasks = conf.get("tasks") or []
        if not tasks:
            self._last_reason = "还没有任务"
            return

        for task in tasks:
            if self._stop.is_set():
                return
            ok, reason = self._due(task, now, conf)
            if reason:
                self._last_reason = reason
            if not ok:
                continue
            self._run(task, now)

    # ---- 判定 ----
    def _due(self, task: dict, now: datetime, conf: dict) -> tuple[bool, str]:
        tid = task.get("id") or ""
        if not task.get("enabled"):
            return False, ""
        if not task.get("verified"):
            return False, f"「{task.get('name')}」还没试运行过，不执行"
        if tid in self._running_ids:
            return False, ""
        if self._cooldown.get(tid, 0) > time.time():
            return False, ""

        if task.get("kind") == "daily":
            hhmm = task.get("time") or ""
            try:
                h, m = [int(x) for x in hhmm.split(":")[:2]]
            except Exception:
                return False, f"「{task.get('name')}」时间格式坏了"

            days = task.get("days") or []
            if days and now.isoweekday() not in days:
                return False, ""

            sched = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if now < sched:
                return False, f"未到时间（今日 {hhmm}）"

            if self._ran_today(task, now):
                return False, ""

            late_min = (now - sched).total_seconds() / 60.0
            window = max(0, int(conf.get("catch_up_min") or 0))
            if late_min > window:
                # 超过补偿窗口：标记今天跳过，避免每轮都判定一次
                config.update_task(tid, {
                    "last_run": sched.strftime("%Y-%m-%d %H:%M"),
                    "last_result": f"已跳过（错过 {int(late_min)} 分钟）",
                })
                return False, f"「{task.get('name')}」错过时间，今天跳过"
            return True, ""

        # once
        at = task.get("at") or ""
        try:
            when = datetime.strptime(at, "%Y-%m-%d %H:%M")
        except Exception:
            return False, f"「{task.get('name')}」执行时间格式坏了"
        if now < when:
            return False, ""
        if task.get("last_run"):
            return False, ""
        return True, ""

    def _ran_today(self, task: dict, now: datetime) -> bool:
        last = (task.get("last_run") or "").strip()
        if not last:
            return False
        return last[:10] == now.strftime("%Y-%m-%d")

    # ---- 执行 ----
    def _run(self, task: dict, now: datetime):
        tid = task.get("id") or ""
        name = task.get("name") or task.get("target") or tid

        if not wechat_lock.acquire(blocking=False):
            self._last_reason = "微信正被自动回复占用，30 秒后重试"
            log.info("[定时任务] 「%s」推迟：微信正被自动回复占用", name)
            return
        self._running_ids.add(tid)
        # 延迟到 finally 之外，失败路径也要能重试
        try:
            log.info("[定时任务] 开始执行「%s」：发给「%s」", name, task.get("target"))
            t0 = time.time()

            import wx_send_pro
            sender = wx_send_pro.WeChatSender(shot_dir=self._shot_dir)
            ok, detail, shot = sender.send_to(
                task.get("target", ""), task.get("text", ""),
                dry_run=False, tag=tid or "task")

            cost = time.time() - t0
            stamp = now.strftime("%Y-%m-%d %H:%M")

            if ok:
                patch = {
                    "last_run": stamp,
                    "last_result": "成功" if "确认气泡" in detail else f"已发送（{detail}）",
                    "fail_count": 0,
                }
                if task.get("kind") == "once":
                    patch["enabled"] = False
                    patch["last_result"] = "已完成"
                config.update_task(tid, patch)
                self._last_action = f"已发送「{name}」（{cost:.1f}s）"
                log.info("[定时任务] 完成「%s」，耗时 %.1fs → %s", name, cost, detail)
                self._notify(f"定时任务已发送：{name}", "ok")
            else:
                fc = int(task.get("fail_count") or 0) + 1
                patch = {"fail_count": fc, "last_result": f"失败：{detail}"}
                if fc >= _FAIL_LIMIT:
                    patch["enabled"] = False
                    patch["last_result"] = f"连续失败 {fc} 次，已自动停用"
                config.update_task(tid, patch)
                self._cooldown[tid] = time.time() + _FAIL_COOLDOWN_SEC
                self._last_action = f"执行失败：{name}"
                log.warning("[定时任务] 失败「%s」：%s（累计 %d 次）", name, detail, fc)
                self._notify(f"定时任务失败：{name} —— {detail}", "warn")
        finally:
            self._running_ids.discard(tid)
            wechat_lock.release()


# 运行中的实例（main 启动时赋值，设置页读状态用）
CURRENT: "Scheduler | None" = None
