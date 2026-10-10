"""微信聊天框风格的润色结果弹窗（纯 tkinter 实现，不依赖 PIL/ImageTk）。

只改样式，不改变功能：
- show_choices(versions, on_choose, master=None, on_close=None)：展示多个润色版本，
  每个版本是微信「我发出的」绿色气泡，下方带「复制 / 发送」按钮；另有一条 AI 的
  白色气泡作引导；Esc 关闭。
- show_error(msg, master=None, on_close=None)：微信风格的错误提示卡。

线程模型（关键）：
弹框必须运行在“持有 tkinter 解释器”的线程里。本程序托盘在主线程持有 Tk 解释器，
而触发（热键 / 点击动图）发生在子线程。因此 main.py 会把这些弹框构建函数通过
tray.run_ui() 路由到【主线程】执行，并以 Toplevel(master=托盘根) 形式构建——
此时传 master 参数且【不】再跑自己的 mainloop。master 为 None 时（无托盘兜底场景）
才自建 Tk 并 mainloop。这样杜绝在子线程新建 Tk 导致的
“Calling Tcl from different apartment” 并卡死 worker 线程的问题。
"""
import tkinter as tk
import pyperclip

# ---- 微信配色 ----
CHAT_BG = "#ededed"          # 聊天区背景灰
HEADER_BG = "#f7f7f7"        # 顶栏
HEADER_LINE = "#d9d9d9"      # 顶栏分隔线
GREEN = "#95EC69"            # 微信发出气泡绿
WHITE = "#ffffff"            # 收到气泡白
TEXT = "#1f1f1f"             # 正文
MINE = "#07C160"             # 头像/发送按钮绿（微信绿）
AI = "#10AEFF"               # AI 头像蓝
BTN_GREY = "#e6e6e6"         # 复制按钮灰底
TITLE_FONT = ("Microsoft YaHei", 13, "bold")
TEXT_FONT = ("Microsoft YaHei", 11)
SYS_FONT = ("Microsoft YaHei", 10)

BUBBLE_MAX = 282             # 气泡最大宽度
PAD = 10                     # 气泡内边距
TAIL = 8                     # 气泡小尾巴长度


def _round_rect(canvas, x1, y1, x2, y2, r, fill, outline="", width=0):
    """在 Canvas 上画圆角矩形（纯 tk，不依赖图片）。"""
    canvas.create_arc(x1, y1, x1 + 2 * r, y1 + 2 * r, start=90, extent=90,
                      style="pieslice", outline=outline, width=width, fill=fill)
    canvas.create_arc(x2 - 2 * r, y1, x2, y1 + 2 * r, start=0, extent=90,
                      style="pieslice", outline=outline, width=width, fill=fill)
    canvas.create_arc(x2 - 2 * r, y2 - 2 * r, x2, y2, start=270, extent=90,
                      style="pieslice", outline=outline, width=width, fill=fill)
    canvas.create_arc(x1, y2 - 2 * r, x1 + 2 * r, y2, start=180, extent=90,
                      style="pieslice", outline=outline, width=width, fill=fill)
    canvas.create_rectangle(x1 + r, y1, x2 - r, y2,
                            outline=outline, width=width, fill=fill)
    canvas.create_rectangle(x1, y1 + r, x2, y2 - r,
                            outline=outline, width=width, fill=fill)


def _make_avatar(parent, letter, side):
    """画一个微信风格圆形头像（蓝=AI，绿=我）。"""
    color = MINE if side == "right" else AI
    c = tk.Canvas(parent, width=36, height=36, bg=CHAT_BG,
                  highlightthickness=0, bd=0)
    c.create_oval(2, 2, 34, 34, fill=color, outline=color)
    c.create_text(18, 19, text=letter, fill="white",
                  font=("Microsoft YaHei", 12, "bold"))
    return c


def _make_bubble(parent, text, side="right"):
    """画一个微信气泡（圆角矩形 + 小尾巴），返回承载它的 Frame。"""
    # 先量文字尺寸
    tmp = tk.Label(parent, text=text, font=TEXT_FONT,
                   wraplength=BUBBLE_MAX - 2 * PAD)
    tmp.update_idletasks()
    tw = tmp.winfo_reqwidth()
    th = tmp.winfo_reqheight()
    tmp.destroy()

    bw = min(BUBBLE_MAX, tw) + 2 * PAD
    bh = th + 2 * PAD
    fill = GREEN if side == "right" else WHITE
    mid = bh / 2

    bf = tk.Frame(parent, bg=CHAT_BG)

    if side == "right":
        cw, ch = bw + TAIL, bh
        canvas = tk.Canvas(bf, width=cw, height=ch, bg=CHAT_BG,
                           highlightthickness=0, bd=0)
        canvas.pack()
        _round_rect(canvas, 0, 0, bw, bh, 10, fill=fill)
        canvas.create_polygon(bw, mid - 6, bw + TAIL, mid, bw, mid + 6,
                              fill=fill, outline=fill)
        lbl = tk.Label(canvas, text=text, font=TEXT_FONT, fg=TEXT,
                       wraplength=BUBBLE_MAX - 2 * PAD, justify="left",
                       bg=fill)
        canvas.create_window(PAD, PAD, window=lbl, anchor="nw")
    else:
        cw, ch = bw + TAIL, bh
        canvas = tk.Canvas(bf, width=cw, height=ch, bg=CHAT_BG,
                           highlightthickness=0, bd=0)
        canvas.pack()
        _round_rect(canvas, TAIL, 0, TAIL + bw, bh, 10, fill=fill)
        canvas.create_polygon(TAIL, mid - 6, 0, mid, TAIL, mid + 6,
                              fill=fill, outline=fill)
        lbl = tk.Label(canvas, text=text, font=TEXT_FONT, fg=TEXT,
                       wraplength=BUBBLE_MAX - 2 * PAD, justify="left",
                       bg=fill)
        canvas.create_window(TAIL + PAD, PAD, window=lbl, anchor="nw")

    return bf


def _add_message(parent, text, side="right"):
    """在聊天区加一条消息：头像 + 气泡，按方向左右布局。"""
    row = tk.Frame(parent, bg=CHAT_BG)
    row.pack(fill="x", padx=10, pady=5)
    inner = tk.Frame(row, bg=CHAT_BG)
    inner.pack(anchor="e" if side == "right" else "w")

    av = _make_avatar(inner, "我" if side == "right" else "AI", side)
    bf = _make_bubble(inner, text, side)

    if side == "right":
        bf.pack(side="left", padx=(0, 6))
        av.pack(side="left")
    else:
        av.pack(side="left", padx=(0, 6))
        bf.pack(side="left")
    return bf


def _header(root, title="ai-reply-helper"):
    """微信聊天顶栏：左返回、中标题、右关闭。"""
    bar = tk.Frame(root, bg=HEADER_BG, height=46)
    bar.pack(fill="x")
    bar.pack_propagate(False)

    back = tk.Label(bar, text="‹", bg=HEADER_BG, fg="#181818",
                    font=("Microsoft YaHei", 20), cursor="hand2")
    back.pack(side="left", padx=10)
    back.bind("<Button-1>", lambda e: root.destroy())

    t = tk.Label(bar, text=title, bg=HEADER_BG, fg="#181818",
                 font=TITLE_FONT)
    t.pack(side="left", expand=True, fill="both")

    close = tk.Label(bar, text="✕", bg=HEADER_BG, fg="#888888",
                     font=("Microsoft YaHei", 12), cursor="hand2")
    close.pack(side="right", padx=12)
    close.bind("<Button-1>", lambda e: root.destroy())

    sep = tk.Frame(root, bg=HEADER_LINE, height=1)
    sep.pack(fill="x")


def _scroll_area(root):
    """返回可滚动的内部 Frame（放消息）。

    关键：inner 必须撑满 canvas 宽度（canvas <Configure> 时同步 inner 宽度），
    否则消息行按内容宽度左对齐，右边会留一条空白带；同时 scrollregion
    也要跟着更新，内容超高时滚动条才生效。
    """
    canvas = tk.Canvas(root, bg=CHAT_BG, borderwidth=0,
                       highlightthickness=0)
    scroll = tk.Scrollbar(root, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas, bg=CHAT_BG)
    win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

    def _on_inner_configure(e):
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _on_canvas_configure(e):
        canvas.itemconfig(win_id, width=e.width)
        canvas.configure(scrollregion=canvas.bbox("all"))

    inner.bind("<Configure>", _on_inner_configure)
    canvas.bind("<Configure>", _on_canvas_configure)
    canvas.configure(yscrollcommand=scroll.set)
    canvas.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")

    # 鼠标滚轮滚动（仅绑定到本 canvas，避免 bind_all 跨解释器泄漏）
    def _on_mousewheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    canvas.bind("<MouseWheel>", _on_mousewheel)
    return inner


def _build_choices(root, versions, on_choose):
    """构建「多版本选择」弹窗内容。"""
    if not versions:
        return

    _header(root)
    inner = _scroll_area(root)

    # 顶部系统提示（微信灰色居中提示）
    sys_tip = tk.Label(
        inner, text="AI 已为你生成以下润色版本",
        bg=CHAT_BG, fg="#9a9a9a", font=SYS_FONT,
    )
    sys_tip.pack(pady=(8, 2))

    def choose(text):
        root.destroy()
        on_choose(text)

    # AI 引导气泡（收到，白）
    _add_message(
        inner,
        f"已生成 {len(versions)} 个版本，点「发送」即回填微信输入框：",
        side="left",
    )

    for i, v in enumerate(versions, 1):
        bf = _add_message(inner, v, side="right")

        def _copy(t=v):
            try:
                pyperclip.copy(t)
            except Exception:
                pass

        def _send(t=v):
            choose(t)

        btns = tk.Frame(bf, bg=CHAT_BG)
        btns.pack(anchor="e", pady=(4, 0))
        tk.Button(btns, text=f"复制", command=_copy, width=7,
                  bg=BTN_GREY, fg="#222222", relief="flat",
                  font=("Microsoft YaHei", 10)).pack(side="right", padx=2)
        tk.Button(btns, text="发送", command=_send, width=7,
                  bg=MINE, fg="white", relief="flat",
                  activebackground=MINE, font=("Microsoft YaHei", 10, "bold")
                  ).pack(side="right", padx=2)

    root.bind("<Escape>", lambda e: root.destroy())


def _build_error(root, msg):
    """构建微信风格错误提示卡。"""
    _header(root, title="ai-reply-helper")
    inner = _scroll_area(root)

    sys_tip = tk.Label(
        inner, text="提示", bg=CHAT_BG, fg="#9a9a9a", font=SYS_FONT,
    )
    sys_tip.pack(pady=(10, 4))

    # 错误内容用「收到」白气泡呈现
    _add_message(inner, msg, side="left")

    ok = tk.Button(root, text="知道了", command=root.destroy, width=14,
                   bg=MINE, fg="white", relief="flat",
                   activebackground=MINE, font=("Microsoft YaHei", 11, "bold"))
    ok.pack(pady=(6, 14))

    root.bind("<Escape>", lambda e: root.destroy())


def _wire_close(root, on_close):
    """弹框关闭（销毁）时触发 on_close 回调（如：右下角动图切回待机）。"""
    if on_close is None:
        return

    def _on_destroy(event):
        # 仅当被销毁的是本窗口自身（而非子控件）才触发
        if str(event.widget) == root._w:
            try:
                on_close()
            except Exception:
                pass

    root.bind("<Destroy>", _on_destroy)


def show_choices(versions, on_choose, master=None, on_close=None):
    """弹出置顶小窗，展示多个润色版本（微信聊天框风格）。

    on_choose(text): 用户点“发送”时回调，参数为选定文本；回调后窗口关闭。
    master: 传入则作为 master 的 Toplevel 构建（不另起 mainloop，由主线程驱动）；
            None 则自建 Tk 并 mainloop（无托盘兜底场景）。
    on_close: 窗口销毁时回调（如 tray.show_idle()）。
    """
    if not versions:
        return
    root = tk.Toplevel(master) if master is not None else tk.Tk()
    root.title("ai-reply-helper")
    root.attributes("-topmost", True)
    root.geometry("380x540")
    try:
        root.iconify()       # 防止闪烁，随后立刻恢复
        root.deiconify()
    except Exception:
        pass
    _build_choices(root, versions, on_choose)
    _wire_close(root, on_close)
    if master is None:
        root.mainloop()


def show_error(msg, master=None, on_close=None):
    """弹出一个置顶错误/提示小窗，让失败可见（而不是静默无反应）。

    master / on_close 语义同 show_choices：传入 master 时作为主线程 Toplevel 构建，
    关闭时触发 on_close（如 tray.show_idle()）。
    """
    root = tk.Toplevel(master) if master is not None else tk.Tk()
    root.title("ai-reply-helper - 提示")
    root.attributes("-topmost", True)
    root.geometry("430x260")
    try:
        root.iconify()
        root.deiconify()
    except Exception:
        pass
    _build_error(root, msg)
    _wire_close(root, on_close)
    if master is None:
        root.mainloop()
