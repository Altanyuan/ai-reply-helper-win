"""右下角浮动动图组件（Windows）。

- 程序启动：显示 idle 动图（默认 1.webp），位置默认右下角
- 用户按热键：切换为 active 动图（默认 2.webp）
- 关闭“优化后语句”弹窗：切回 idle 动图
- **鼠标左键可拖动图标**，位置限制在屏幕可视区域内，松手后记入配置

实现要点：
1. 用 tkinter 做一个无边框、置顶、透明背景的窗口，钉在屏幕右下角。
2. webp 带 RGBA 透明通道，用“键色合成法”做 Windows 透明窗口
   （把每帧合成到一个几乎不出现的品红色背景，再把窗口透明色设为该色）。
3. 组件在调用方线程（main.py 的主线程）跑 tkinter mainloop；
   业务线程（pynput 回调）只通过线程安全的 queue 下发 idle/active 指令，
   避免跨线程调用 tkinter，也规避 Tcl 跨线程收尾告警。
4. 动图帧很多（约 120 帧），若一次性解码会阻塞启动数秒。这里采用“分块后台解码
   + 抽帧（SAMPLING）”策略：mainloop 立即启动保持响应，帧按小块解码，
   首帧就绪即显示，解码完再开始循环动画。
5. **拖动与点击的区分**：按下记录起点，位移超过 DRAG_THRESHOLD 才算拖动；
   抬起时若从未超过阈值 → 判定为「点击」，照旧触发润色流程；
   若发生过位移 → 只移动位置、**不触发**润色。二者互不干扰。
6. **位置持久化**：拖动结束时把最终坐标通过 on_move 回调交给调用方（main.py
   写入 config.json 的 tray_pos）；下次启动沿用。若保存的位置缺失、非法或
   完全落在屏幕外，则复位到默认右下角，保证图标一定可见。
"""
import os
import queue
import logging
import tkinter as tk
from PIL import Image, ImageSequence, ImageTk

log = logging.getLogger("tray")

KEY_COLOR = (255, 0, 255)          # 透明键色（合成背景用，贴纸几乎不会用到）
KEY_HEX = "#FF00FF"
POLL_MS = 60                       # 指令轮询间隔
LOAD_ITERS = 4                     # 每小块解码的 webp 帧数（控制单次阻塞时长）
LOAD_TICK_MS = 8                   # 两块解码之间的间隔（让 mainloop 喘息）
SAMPLING = 3                       # 抽帧：每 N 帧保留 1 帧（120→~40，省时省内存）
FRAME_FALLBACK_MS = 40             # webp 未给出时长时的默认帧延迟（≈25fps）
CLOSE_HIDE_DELAY_MS = 400          # 鼠标移出托盘后，延迟多久隐藏关闭按钮
DRAG_THRESHOLD = 4                 # 位移超过该像素数才判定为「拖动」，否则算「点击」
EDGE_MARGIN = 24                   # 默认停靠时距屏幕边缘的留白


def _work_area():
    """主屏工作区（已排除任务栏）(left, top, right, bottom)；取不到返回 None。"""
    try:
        import ctypes
        from ctypes import wintypes
        SPI_GETWORKAREA = 0x0030
        rect = wintypes.RECT()
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETWORKAREA, 0, ctypes.byref(rect), 0
        )
        if ok:
            return (int(rect.left), int(rect.top),
                    int(rect.right), int(rect.bottom))
    except Exception:
        pass
    return None


def _visible_bounds(root):
    """返回图标允许摆放的边界 (left, top, right, bottom)。

    优先用系统工作区（这样不会压住任务栏）；但**仅当工作区与 tkinter 报告的
    整屏尺寸自洽时**才采用 —— 若进程 DPI 感知状态与 tkinter 不一致，两套坐标
    会差一个缩放比，混用会把图标夹到错误位置，此时宁可用整屏尺寸兜底。
    """
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    wa = _work_area()
    if wa:
        wl, wt, wr, wb = wa
        if 0 <= wl < wr <= sw + 2 and 0 <= wt < wb <= sh + 2:
            return wl, wt, wr, wb
    return 0, 0, sw, sh


def _clamp(x, y, w, h, bounds):
    """把窗口左上角 (x, y) 夹进可见边界，保证窗口**完整**落在区域内。

    窗口比可用区域还大时退化为对齐左上角，避免出现负坐标把自己挤出屏幕。
    """
    left, top, right, bottom = bounds
    max_x, max_y = right - w, bottom - h
    if max_x < left:
        x = left
    else:
        x = max(left, min(int(x), max_x))
    if max_y < top:
        y = top
    else:
        y = max(top, min(int(y), max_y))
    return int(x), int(y)


def _make_close_button(root, size, on_click):
    """在托盘右上角创建「悬停才显示」的关闭按钮（红底白叉）。

    返回 (按钮, show, schedule_hide, hide_now)：
      show()          立即显示，并取消尚未执行的隐藏
      schedule_hide() 延时隐藏；期间若再次 show() 则取消隐藏
      hide_now()      立即隐藏（拖动图标时用它，避免 X 挡在拖动路径上）

    为什么要延时隐藏：鼠标从窗口移到按钮上的瞬间，会先触发窗口/标签的 Leave，
    若立即 place_forget，按钮会在被点到之前就消失，导致“点不到 X”。
    留出缓冲时间，且鼠标进入按钮时会再次 show() 取消隐藏。

    按钮 Canvas 的背景用键色（会被 -transparentcolor 抠掉），
    所以视觉上只剩下圆形按钮本身，不会有一块方块底。
    """
    btn = tk.Canvas(root, width=26, height=26, bg=KEY_HEX,
                    highlightthickness=0, bd=0, cursor="hand2")
    btn.create_oval(3, 3, 23, 23, fill="#e74c3c", outline="#ffffff", width=1)
    btn.create_line(9, 9, 17, 17, fill="white", width=2)
    btn.create_line(17, 9, 9, 17, fill="white", width=2)

    job = {"id": None}

    def _cancel():
        if job["id"] is not None:
            try:
                root.after_cancel(job["id"])
            except Exception:
                pass
            job["id"] = None

    def hide_now():
        job["id"] = None
        btn.place_forget()

    def show():
        _cancel()
        btn.place(x=size - 32, y=4)
        # 注意：Canvas 自带 lift()（提升画布内图元），会覆盖控件层级的 Misc.lift()，
        # 直接 btn.lift() 会报 "wrong # args" TclError。必须显式调用 Misc.lift，
        # 才能把按钮叠在动图 label 之上，保证点击命中的是 X 而不是触发润色。
        tk.Misc.lift(btn)

    def schedule_hide():
        _cancel()
        job["id"] = root.after(CLOSE_HIDE_DELAY_MS, hide_now)

    btn.bind("<Button-1>", lambda e: on_click())
    btn.bind("<Enter>", lambda e: show())
    btn.bind("<Leave>", lambda e: schedule_hide())
    return btn, show, schedule_hide, hide_now


class TrayWidget:
    def __init__(self, idle_path, active_path, size=220, pos=None, on_move=None):
        self.idle_path = idle_path
        self.active_path = active_path
        self.size = int(size)
        self._cmd = queue.Queue()
        self._cache = {}           # name -> (frames, durs)，解码完成后缓存
        self._pending = {}         # name -> 进行中的 loader 状态
        self._root = None
        self._command = None        # 点击动图时的回调（由 main.py 注入，与热键等价）
        self._pos = pos             # 上次保存的位置 (x, y)；None/非法则用默认右下角
        self._on_move = on_move     # 拖动结束时回调 (x, y)，由 main.py 写入配置

    def run(self):
        """在主线程启动 mainloop（进程常驻）；调用 stop() 或 Ctrl+C 退出。"""
        self._run()

    # ---------- 内部：跑 tkinter mainloop ----------
    def _run(self):
        try:
            root = tk.Tk()
        except Exception as e:  # 无显示环境等
            print(f"[tray] 无法创建窗口，跳过右下角动图: {e}")
            return

        self._root = root
        root.overrideredirect(True)              # 去标题栏 + 不在任务栏出现
        root.attributes("-topmost", True)        # 始终置顶
        root.attributes("-transparentcolor", KEY_HEX)
        root.configure(
            bg=KEY_HEX,
            highlightthickness=0,
            bd=0,
            relief="flat",
            highlightbackground=KEY_HEX,
        )

        w = h = self.size
        root.geometry(f"{w}x{h}")
        root.update_idletasks()
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()

        # 初始位置：优先沿用上次拖动保存的位置；缺失/非法则复位到右下角默认位。
        # 最后统一夹进可见区域，保证图标一定完整落在屏幕上。
        bounds = _visible_bounds(root)
        init_x, init_y = sw - w - EDGE_MARGIN, sh - h - EDGE_MARGIN
        if self._pos:
            try:
                init_x, init_y = int(self._pos[0]), int(self._pos[1])
                log.info(f"托盘动图：沿用上次保存的位置 ({init_x},{init_y})")
            except Exception:
                log.info("托盘动图：保存的位置非法，复位到右下角")
        init_x, init_y = _clamp(init_x, init_y, w, h, bounds)
        root.geometry(f"{w}x{h}+{init_x}+{init_y}")

        label = tk.Label(root, bd=0, bg=KEY_HEX, highlightthickness=0)
        label.pack()
        label.config(cursor="hand2")          # 提示可点击

        def _on_click(event=None):
            # 点击右下角动图：触发回调（main.py 注入的 trigger()，会另起线程跑
            # 与热键完全一致的 _handle 流程）。此处只做转发，不阻塞 mainloop。
            if self._command:
                self._command()

        # ---- 拖动：与「点击触发润色」严格区分 ----
        # 判定规则：按下记起点 → 位移超过 DRAG_THRESHOLD 才升级为拖动 →
        # 抬起时「从未超过阈值」才算点击（触发润色），否则只移动、不触发。
        # 这样手抖不会误触发，拖动也不会误触发润色。
        drag = {"active": False, "moved": False, "px": 0, "py": 0,
                "wx": 0, "wy": 0, "bounds": bounds}
        # 自己记录「已经设定过的窗口位置」。不用 root.winfo_x() 是因为它读的是 Tk
        # 缓存的几何信息，在连续的移动事件里可能还没更新，会拿到旧坐标
        # （导致松手时保存到拖动前的位置）。这里以我们下发的值为唯一真相。
        cur = [init_x, init_y]

        def _on_press(event):
            drag.update(
                active=True, moved=False,
                px=event.x_root, py=event.y_root,
                wx=cur[0], wy=cur[1],
                bounds=_visible_bounds(root),
            )

        def _on_motion(event):
            if not drag["active"]:
                return
            dx = event.x_root - drag["px"]
            dy = event.y_root - drag["py"]
            if not drag["moved"]:
                if max(abs(dx), abs(dy)) <= DRAG_THRESHOLD:
                    return                      # 阈值内抖动：仍按点击处理
                drag["moved"] = True
                _hide_close()                   # 拖动时收起 X，别挡在拖动路径上
                try:
                    label.config(cursor="fleur")  # 视觉反馈：当前是「可移动」状态
                except Exception:
                    pass
                log.info("开始拖动托盘动图")
            # 以按下时的位置为基准做增量位移，并夹进可见区域。
            # 被夹住时窗口停在边界、鼠标继续移动，回移时会自然跟随，不会漂移。
            x, y = _clamp(drag["wx"] + dx, drag["wy"] + dy, w, h, drag["bounds"])
            cur[0], cur[1] = x, y
            root.geometry(f"+{x}+{y}")

        def _on_release(event):
            if not drag["active"]:
                return
            drag["active"] = False
            moved = drag["moved"]
            drag["moved"] = False
            try:
                label.config(cursor="hand2")
            except Exception:
                pass
            if not moved:
                _on_click()                     # 未发生位移 → 点击，原行为不变
                return
            # 拖动结束：按最新边界再夹一次（防拖动期间分辨率/任务栏变化），再保存
            x, y = _clamp(cur[0], cur[1], w, h, _visible_bounds(root))
            cur[0], cur[1] = x, y
            root.geometry(f"+{x}+{y}")
            log.info(f"托盘动图已移动到 ({x},{y})")
            self._save_pos(x, y)
            _show_close()                       # 鼠标仍悬停在图标上，按悬停语义显示 X

        for _wid in (root, label):
            _wid.bind("<ButtonPress-1>", _on_press)
            _wid.bind("<B1-Motion>", _on_motion)
            _wid.bind("<ButtonRelease-1>", _on_release)

        # 悬停托盘时，右上角显示关闭按钮 X；点击 X 即退出程序。
        # 绑定到 root 与 label 两处：鼠标在动图任意位置移动都能可靠触发显隐。
        _close_btn, _show_close, _schedule_hide, _hide_close = _make_close_button(
            root, w, on_click=self._request_exit
        )

        def _maybe_show(_e):
            if not drag["active"]:
                _show_close()

        def _maybe_hide(_e):
            if not drag["active"]:
                _schedule_hide()

        root.bind("<Enter>", _maybe_show)
        root.bind("<Leave>", _maybe_hide)
        label.bind("<Enter>", _maybe_show)
        label.bind("<Leave>", _maybe_hide)

        state = {"path": None, "frames": [], "durs": [], "idx": 0, "after_id": None}

        def animate():
            frames = state["frames"]
            if not frames:
                return
            state["idx"] = (state["idx"] + 1) % len(frames)
            label.configure(image=frames[state["idx"]])
            if state["after_id"]:
                root.after_cancel(state["after_id"])
            state["after_id"] = root.after(state["durs"][state["idx"]], animate)

        def _activate(name):
            frames, durs = self._cache[name]
            state["frames"], state["durs"] = frames, durs
            state["idx"] = 0
            if frames:
                label.configure(image=frames[0])
                if state["after_id"]:
                    root.after_cancel(state["after_id"])
                state["after_id"] = root.after(durs[0], animate)

        def _begin_load(name):
            if name in self._cache:
                _activate(name)
                return
            ld = self._pending.get(name)
            if ld and not ld["done"]:            # 正在解码，避免重复启动
                if ld["frames"]:
                    label.configure(image=ld["frames"][0])
                return
            path = self.idle_path if name == "idle" else self.active_path
            im = Image.open(path)
            self._pending[name] = {
                "it": iter(ImageSequence.Iterator(im)),
                "frames": [], "durs": [], "g": 0, "done": False, "shown": False,
            }
            _load_tick(name)

        def _load_tick(name):
            ld = self._pending.get(name)
            if not ld or ld["done"]:
                return
            try:
                for _ in range(LOAD_ITERS):
                    fr = next(ld["it"])
                    g = ld["g"]
                    ld["g"] += 1
                    if g % SAMPLING != 0:        # 抽帧降负载
                        continue
                    rgba = fr.convert("RGBA").resize((w, h), Image.LANCZOS)
                    alpha = rgba.split()[3]
                    # 二值遮罩：彻底消除半透明边缘的品红描边（红线）。
                    # 边缘像素要么完全透明（=键色），要么完全不透明，不再产生
                    # 介于 gif 颜色与品红之间的混合色，从而被 transparentcolor
                    # 正确剔除。阈值略低以保留更多贴纸主体。
                    mask = alpha.point(lambda a: 255 if a > 100 else 0)
                    bg = Image.new("RGB", (w, h), KEY_COLOR)
                    bg.paste(rgba.convert("RGB"), (0, 0), mask)
                    ld["frames"].append(ImageTk.PhotoImage(bg))
                    d = int(fr.info.get("duration", 0) or 0)
                    ld["durs"].append((d * SAMPLING) if d > 0 else FRAME_FALLBACK_MS * SAMPLING)
                if ld["frames"] and not ld["shown"]:
                    ld["shown"] = True
                    if state["path"] == name:    # 当前正显示该动图，先亮首帧
                        label.configure(image=ld["frames"][0])
            except StopIteration:
                ld["done"] = True
                self._cache[name] = (ld["frames"], ld["durs"])
                if state["path"] == name:        # 解码完成，开始循环动画
                    _activate(name)
                return
            root.after(LOAD_TICK_MS, lambda: _load_tick(name))

        def set_state(name):
            if state["path"] == name:
                return
            state["path"] = name
            _begin_load(name)

        def poll():
            try:
                while True:
                    item = self._cmd.get_nowait()
                    cmd = item[0]
                    if cmd == "quit":
                        try:
                            # 在 widget 线程内先释放帧引用，避免跨线程 Tcl 收尾告警
                            state["frames"] = []
                            state["durs"] = []
                            state["idx"] = 0
                            self._cache.clear()
                            self._pending.clear()
                            root.quit()          # 退出 mainloop()
                            root.destroy()       # 销毁窗口
                        except Exception:
                            pass
                        return
                    elif cmd == "idle":
                        set_state("idle")
                    elif cmd == "active":
                        set_state("active")
                    elif cmd == "ui":
                        # 在主线程（本 Tcl 解释器）执行弹框构建回调，
                        # 避免业务线程新建 Tk 导致 “Calling Tcl from different
                        # apartment” 并卡死 worker 线程。
                        try:
                            item[1]()
                        except Exception as e:
                            log.warning(f"主线程执行 UI 回调失败: {e}")
            except queue.Empty:
                pass
            root.after(POLL_MS, poll)

        set_state("idle")          # 初始：开始加载并显示 idle 动图
        root.after(POLL_MS, poll)
        root.mainloop()

    # ---------- 对外接口：可从任意线程调用，仅入队（线程安全）----------
    def show_idle(self):
        self._cmd.put(("idle", None))

    def show_active(self):
        self._cmd.put(("active", None))

    def set_command(self, fn):
        """设置点击动图时的回调（由 main.py 注入，与热键等价触发 AI 流程）。"""
        self._command = fn

    def _save_pos(self, x, y):
        """拖动结束时记录图标位置：内存兜底 + 交给调用方持久化。

        即使持久化失败（例如配置目录只读），本进程内仍记住新位置、不影响使用；
        下次启动会回落到默认右下角，不会因保存失败而丢失图标。
        """
        self._pos = (int(x), int(y))
        if not self._on_move:
            return
        try:
            self._on_move(int(x), int(y))
        except Exception as e:
            log.warning(f"保存托盘动图位置失败（下次启动将复位到右下角）: {e}")

    def _request_exit(self):
        """点击右上角 X：请求退出程序。

        只入队 quit 指令，由 mainloop 里的 poll() 处理（销毁窗口并退出 mainloop），
        随后 main() 的 finally 会停止热键 / 鼠标监听，进程正常退出（含清理单实例锁）。
        这里不直接 sys.exit，避免在 tkinter 回调中粗暴中断导致收尾不完整。
        """
        log.info("点击托盘右上角关闭按钮，退出程序")
        self.stop()

    def run_ui(self, fn):
        """在主线程（tray 的 tkinter 解释器）安全地执行 UI 构建函数 fn。

        业务线程调用本方法只负责把 (fn) 入队，立即返回、不会阻塞，
        也不跨线程触碰 Tcl；fn 会在主线程的 mainloop 里被 poll() 取出执行，
        弹框作为主线程 Tk 根的 Toplevel 构建，杜绝第二套 Tk/mainloop。
        """
        self._cmd.put(("ui", fn))

    def get_root(self):
        """返回主线程的 Tk 根，供弹框作为父窗口（Toplevel）使用。"""
        return self._root

    def stop(self):
        self._cmd.put(("quit", None))


def make_tray(idle_path, active_path, size=220, pos=None, on_move=None):
    """工厂：文件缺失或初始化失败时返回 None（不影响主流程）。

    pos:     上次保存的图标位置 (x, y)；缺省或非法时用默认右下角
    on_move: 拖动结束时的回调 (x, y)，由调用方持久化（写入 config.json）
    """
    if not (idle_path and os.path.exists(idle_path) and os.path.exists(active_path)):
        print(f"[tray] 动图缺失（{idle_path} / {active_path}），跳过右下角动图。")
        return None
    return TrayWidget(idle_path, active_path, size=size, pos=pos, on_move=on_move)
