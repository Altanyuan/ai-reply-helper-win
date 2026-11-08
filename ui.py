"""微信风格的回复建议弹窗（纯 tkinter 实现，不依赖 PIL/ImageTk）。

show_choices(versions, on_choose, master=None, on_close=None)：展示卡片式 AI 回复建议，
保留「复制 / 回填微信」操作；Esc 关闭。
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
import tkinter.font as tkfont
from tkinter import ttk
import math
import pyperclip
from PIL import Image, ImageTk
from paths import asset

# ---- 微信 AI 回复面板设计令牌 ----
PAGE = "#F5F6F7"
SURFACE = "#FFFFFF"
INK_900 = "#16181D"
INK_700 = "#3A4048"
INK_500 = "#57606A"
INK_400 = "#6E7681"
LINE = "#E3E5E8"
LINE_STRONG = "#CFD3D8"
GREEN_600 = "#07883F"
GREEN_700 = "#066B32"
GREEN_50 = "#F0F9F3"
GREEN_LINE = "#BFE0CC"

# 旧错误提示仍使用聊天气泡，保留其既有配色常量。
CHAT_BG = "#ededed"
HEADER_BG = "#f7f7f7"
HEADER_LINE = "#d9d9d9"
GREEN = "#95EC69"
WHITE = SURFACE
DRAFT_BG = "#e8f4ff"
TEXT = INK_900
MINE = GREEN_600
AI = "#10AEFF"
BTN_GREY = "#f5f6f7"
TITLE_FONT = ("Microsoft YaHei", 13, "bold")
TEXT_FONT = ("Microsoft YaHei", 11)
SYS_FONT = ("Microsoft YaHei", 10)

BUBBLE_MAX = 320             # 气泡最大宽度
PAD = 10                     # 气泡内边距
TAIL = 8                     # 气泡小尾巴长度
_AI_AVATAR_IMAGE = None      # 缓存图标，避免每条建议都重新解码


def _round_rect(canvas, x1, y1, x2, y2, r, fill, outline="", width=0, tags=()):
    """以单一多边形绘制平滑圆角，避免分片图形拼接产生白缝。"""
    r = max(0, min(r, (x2-x1)/2, (y2-y1)/2))
    points = []
    for cx, cy, start in ((x2-r, y1+r, -90), (x2-r, y2-r, 0),
                          (x1+r, y2-r, 90), (x1+r, y1+r, 180)):
        for step in range(9):
            angle = math.radians(start + step * 90 / 8)
            points.extend((cx + r*math.cos(angle), cy + r*math.sin(angle)))
    return canvas.create_polygon(*points, smooth=True, splinesteps=24,
                                 fill=fill, outline=outline, width=width,
                                 joinstyle="round", tags=tags)


class _RoundButton(tk.Canvas):
    """轻量圆角按钮，保持 tkinter 原生回调与键盘 invoke 接口。"""
    def __init__(self, parent, text, command, *, bg, fg, font, width=None):
        self._text, self._command = text, command
        self._bg, self._fg, self._font = bg, fg, font
        measured = tkfont.Font(font=font).measure(text)
        self._width = width or measured + 22
        self._height = 32
        super().__init__(parent, width=self._width, height=self._height,
                         bg=parent.cget("bg"), highlightthickness=0, bd=0,
                         cursor="hand2", takefocus=True)
        self._draw()
        self.bind("<Button-1>", lambda _e: self.invoke())
        self.bind("<Return>", lambda _e: self.invoke())
        self.bind("<space>", lambda _e: self.invoke())
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)

    def _draw(self, fill=None):
        self.delete("all")
        _round_rect(self, 1, 1, self._width - 1, self._height - 1, 8,
                    fill or self._bg, outline=LINE if self._bg == SURFACE else (fill or self._bg))
        self.create_text(self._width / 2, self._height / 2, text=self._text,
                         fill=self._fg, font=self._font)

    def _enter(self, _event=None):
        self._draw(PAGE if self._bg == SURFACE else GREEN_700)

    def _leave(self, _event=None):
        self._draw()

    def invoke(self):
        if self._command:
            self._command()


def _make_avatar(parent, letter, side):
    """AI 显示应用图标，本人消息保留微信绿色圆形头像。"""
    global _AI_AVATAR_IMAGE
    c = tk.Canvas(parent, width=36, height=36, bg=CHAT_BG,
                  highlightthickness=0, bd=0)
    if side != "right":
        try:
            if _AI_AVATAR_IMAGE is None:
                with Image.open(asset("icon.ico")) as source:
                    icon = source.convert("RGBA").resize(
                        (36, 36), Image.Resampling.LANCZOS,
                    )
                _AI_AVATAR_IMAGE = ImageTk.PhotoImage(icon, master=parent)
            c.create_image(18, 18, image=_AI_AVATAR_IMAGE)
            c._avatar_image = _AI_AVATAR_IMAGE
            return c
        except Exception:
            # 缺少图标资源时保留原 AI 圆形头像，避免结果窗口无法打开。
            color = AI
    else:
        color = MINE
    c.create_oval(2, 2, 34, 34, fill=color, outline=color)
    c.create_text(18, 19, text=letter, fill="white",
                  font=("Microsoft YaHei", 12, "bold"))
    return c


def _make_bubble(parent, text, side="right", bubble_fill=None):
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
    fill = bubble_fill or (GREEN if side == "right" else WHITE)
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


def _add_message(parent, text, side="right", avatar_text=None, bubble_fill=None):
    """在聊天区加一条消息：头像 + 气泡，按方向左右布局。"""
    row = tk.Frame(parent, bg=CHAT_BG)
    row.pack(fill="x", padx=10, pady=5)
    inner = tk.Frame(row, bg=CHAT_BG)
    inner.pack(anchor="e" if side == "right" else "w")

    avatar_text = avatar_text or ("我" if side == "right" else "AI")
    av = _make_avatar(inner, avatar_text, side)
    bf = _make_bubble(inner, text, side, bubble_fill=bubble_fill)

    if side == "right":
        bf.pack(side="left", padx=(0, 6))
        av.pack(side="left")
    else:
        av.pack(side="left", padx=(0, 6))
        bf.pack(side="left")
    return bf


def _header(root, title="会聊"):
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
    """返回可滚动的内部 Frame（放消息）。"""
    canvas = tk.Canvas(root, bg=CHAT_BG, borderwidth=0,
                       highlightthickness=0)
    scroll = tk.Scrollbar(root, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas, bg=CHAT_BG)
    inner.bind("<Configure>",
               lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    content_id = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=scroll.set)
    canvas.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")

    # 绑定在本窗口的 bindtag 上，使鼠标停在气泡文字或按钮上时也能滚动。
    def _on_mousewheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    root.bind("<MouseWheel>", _on_mousewheel, add="+")
    canvas.bind(
        "<Configure>",
        lambda e: canvas.itemconfigure(content_id, width=e.width),
        add="+",
    )
    return inner, canvas


def _choices_header(root):
    """轻量内容栏；窗口的关闭按钮由系统标题栏提供。"""
    bar = tk.Frame(root, bg=SURFACE, height=38,
                   highlightthickness=0)
    bar.pack(fill="x")
    bar.pack_propagate(False)
    tk.Label(bar, text="AI 回复建议", bg=SURFACE, fg=INK_900,
             font=("Segoe UI", 14, "bold"), anchor="w").pack(
                 side="left", padx=12, fill="y")
    more = tk.Menubutton(
        bar, text="···", bg=SURFACE, fg=INK_400, relief="flat",
        activebackground=PAGE, font=("Segoe UI", 14), cursor="hand2",
        padx=8, takefocus=True, indicatoron=0,
    )
    menu = tk.Menu(more, tearoff=False, bg=SURFACE, fg=INK_700,
                   activebackground=PAGE, activeforeground=INK_900,
                   font=("Segoe UI", 11))
    menu.add_command(label="快捷键：1–4 回填对应建议", state="disabled")
    menu.add_command(label="Esc 关闭窗口", state="disabled")
    menu.add_separator()
    menu.add_command(label="关闭", command=root.destroy)
    more.configure(menu=menu)
    more.pack(side="right", padx=4, pady=3)
    tk.Frame(root, bg=LINE, height=1).pack(fill="x")


def _build_choices(root, versions, on_choose, chat_name=None,
                   prompt_name=None, prompt_id=None, prompt_source="全局默认",
                   prompts=None, on_prompt_change=None):
    """构建卡片式回复建议面板。业务操作仍通过原回调执行。"""
    if not versions:
        return

    root.configure(bg=SURFACE)
    _choices_header(root)

    # 联系人和本次提示词集中在一条固定工具栏中。
    context = tk.Frame(root, bg=SURFACE, padx=12, pady=5)
    context.pack(fill="x")
    context.columnconfigure(2, weight=1)
    name = str(chat_name or "").strip() or "未识别"
    chip = tk.Frame(context, bg=SURFACE, highlightthickness=1,
                    highlightbackground=LINE, highlightcolor=GREEN_600)
    name_font = tkfont.Font(family="Segoe UI", size=12)
    desired_chip_width = max(86, int(name_font.measure(name)) + 36)
    chip.configure(width=desired_chip_width, height=24)
    chip.grid_propagate(False)
    initial = name[:1] if name and name != "未识别" else "·"
    avatar = tk.Label(chip, text=initial, bg="#DCEFE4", fg="#256B47",
                      font=("Segoe UI", 9, "bold"), width=2, height=1)
    avatar.pack(side="left", padx=(3, 4), pady=2)
    name_label = tk.Label(chip, text=name, bg=SURFACE, fg=INK_700,
                          font=("Segoe UI", 12), anchor="w", justify="left")
    name_label.pack(side="left", padx=(0, 7), fill="x", expand=True)

    name_counts = {}
    for prompt in prompts or []:
        label = str(prompt.get("name") or "未命名提示词")
        name_counts[label] = name_counts.get(label, 0) + 1
    label_to_id = {}
    id_to_label = {}
    for prompt in prompts or []:
        pid = prompt.get("id")
        if not pid:
            continue
        label = str(prompt.get("name") or "未命名提示词")
        if name_counts[label] > 1:
            label += f"（{pid}）"
        label_to_id[label] = pid
        id_to_label[pid] = label

    active_label = (id_to_label.get(prompt_id)
                    or str(prompt_name or "全局默认"))
    selected = tk.StringVar(master=root, value=active_label)
    if label_to_id:
        style = ttk.Style(root)
        style.configure("Prompt.TCombobox", font=("Segoe UI", 10), padding=(8, 4))
        prompt_picker = ttk.Combobox(
            context, textvariable=selected, values=tuple(label_to_id),
            state="readonly", width=max(8, min(14, max(map(len, label_to_id)) + 2)),
            style="Prompt.TCombobox", takefocus=True,
        )
    else:
        prompt_picker = tk.Label(context, text=active_label, bg=SURFACE,
                                 fg=INK_700, font=("Segoe UI", 12),
                                 padx=7, pady=2)
        prompt_picker.configure(relief="flat")
    regen = None
    if on_prompt_change and label_to_id:
        def regenerate():
            next_id = label_to_id.get(selected.get())
            if next_id:
                root.destroy()
                on_prompt_change(next_id)

        regen = tk.Button(
            context, text="↻", command=regenerate, bg=SURFACE,
            fg=INK_700, relief="flat", borderwidth=1,
            activebackground=PAGE, activeforeground=GREEN_700,
            font=("Segoe UI", 16), width=2, padx=2, pady=0, cursor="hand2", takefocus=True,
            highlightthickness=1, highlightbackground=LINE,
            highlightcolor=GREEN_600,
        )
    if regen is not None:
        regen.grid(row=0, column=3, padx=(7, 0), sticky="e")
    chip.grid(row=0, column=0, padx=(0, 8), sticky="w")
    prompt_picker.grid(row=0, column=1, sticky="w")

    def fit_context(_event=None):
        available = max(0, context.winfo_width() - 24)
        prompt_min = 120
        regen_width = regen.winfo_reqwidth() + 7 if regen is not None else 0
        needs_second_row = chip.winfo_reqwidth() + prompt_min + regen_width > available
        actual_chip_width = min(desired_chip_width, max(86, available))
        name_label.configure(wraplength=max(80, actual_chip_width - 36))
        chip.configure(width=actual_chip_width,
                       height=max(24, name_label.winfo_reqheight() + 4))
        if needs_second_row:
            chip.grid_configure(row=0, column=0, columnspan=3, sticky="w")
            prompt_picker.grid_configure(row=1, column=0, columnspan=2,
                                         sticky="w", pady=(5, 0))
            if regen is not None:
                regen.grid_configure(row=1, column=3, padx=(7, 0),
                                     pady=(5, 0), sticky="e")
            context.columnconfigure(0, weight=0)
            context.columnconfigure(1, weight=0)
            context.columnconfigure(2, weight=1)
            context.columnconfigure(3, weight=0)
        else:
            chip.grid_configure(row=0, column=0, columnspan=1, sticky="w")
            prompt_picker.grid_configure(row=0, column=1, columnspan=1,
                                         sticky="w", pady=0)
            if regen is not None:
                regen.grid_configure(row=0, column=3, padx=(7, 0),
                                     pady=0, sticky="e")
            context.columnconfigure(0, weight=0)
            context.columnconfigure(1, weight=0)
            context.columnconfigure(2, weight=1)
            context.columnconfigure(3, weight=0)
        name_label.configure(wraplength=max(120, min(500, available - 40)))

    context.bind("<Configure>", fit_context, add="+")
    root.after_idle(fit_context)
    tk.Frame(root, bg=LINE, height=1).pack(fill="x")

    # 安全说明固定在列表外，滚动建议时始终可见。
    hint = tk.Frame(root, bg=PAGE, height=24)
    hint.pack(fill="x")
    hint.pack_propagate(False)
    tk.Label(hint, text="ⓘ", bg=PAGE, fg=GREEN_600,
             font=("Segoe UI", 11, "bold")).pack(side="left", padx=(12, 5))
    tk.Label(hint, text="回填仅写入微信输入框，不会自动发送",
             bg=PAGE, fg=INK_500, font=("Segoe UI", 11),
             anchor="w").pack(side="left", fill="y")
    tk.Frame(root, bg=LINE, height=1).pack(fill="x")

    # 底部状态栏固定显示建议数与当前提示词，不放操作按钮。
    footer = tk.Frame(root, bg=SURFACE, height=34, padx=12,
                      highlightthickness=0)
    footer.pack(side="bottom", fill="x")
    footer.pack_propagate(False)
    source = f" · {prompt_source}" if prompt_source else ""
    status = tk.Label(
        footer,
        text=f"{len(versions)} 条建议 · {prompt_name or '全局默认'}{source}",
        bg=SURFACE, fg=INK_500, font=("Segoe UI", 11), anchor="w",
    )
    status.pack(fill="both", expand=True)
    tk.Frame(root, bg=LINE, height=1).pack(side="bottom", fill="x")

    # 可滚动卡片列表。
    list_wrap = tk.Frame(root, bg=PAGE)
    list_wrap.pack(fill="both", expand=True)
    canvas = tk.Canvas(list_wrap, bg=PAGE, borderwidth=0,
                       highlightthickness=0, takefocus=False)
    scroll = tk.Scrollbar(
        list_wrap, orient="vertical", command=canvas.yview, width=8,
        bg=PAGE, troughcolor=PAGE, activebackground=LINE_STRONG,
        relief="flat", borderwidth=0, highlightthickness=0,
    )
    inner = tk.Frame(canvas, bg=PAGE, padx=12, pady=8)
    content_id = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=scroll.set)
    canvas.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")

    def update_scroll(_event=None):
        canvas.configure(scrollregion=canvas.bbox("all"))

    def fit_width(event):
        canvas.itemconfigure(content_id, width=event.width)

    inner.bind("<Configure>", update_scroll, add="+")
    canvas.bind("<Configure>", fit_width, add="+")

    def scroll_wheel(event):
        canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    root.bind("<MouseWheel>", scroll_wheel, add="+")

    toast = tk.Label(root, text="", bg=INK_900, fg=SURFACE,
                     font=("Segoe UI", 12), padx=12, pady=7,
                     relief="flat", borderwidth=0)
    toast_id = [None]

    def show_toast(message):
        if toast_id[0] is not None:
            root.after_cancel(toast_id[0])
        toast.configure(text=message)
        toast.place(relx=0.5, rely=1.0, anchor="s", y=-42)
        toast.lift()
        toast_id[0] = root.after(1800, toast.place_forget)

    def choose(text):
        root.destroy()
        on_choose(text)

    fill_buttons = {}

    def make_card(index, content):
        shell = tk.Canvas(inner, bg=PAGE, height=110, highlightthickness=0,
                          bd=0, takefocus=False)
        shell.pack(fill="x", pady=(0, 8))
        card = tk.Frame(shell, bg=SURFACE, padx=11, pady=8, bd=0)
        window_id = shell.create_window(9, 8, anchor="nw", window=card)

        def redraw(_event=None):
            width = max(40, shell.winfo_width() - 18)
            shell.itemconfigure(window_id, width=width)
            shell.delete("card-bg")
            _round_rect(shell, 1, 1, max(3, shell.winfo_width()-2),
                        max(3, shell.winfo_height()-2), 14, SURFACE,
                        outline=LINE, width=1, tags=("card-bg",))
            shell.tag_lower("card-bg", window_id)

        shell.bind("<Configure>", redraw, add="+")
        card.bind("<Configure>", lambda _e: shell.after_idle(
            lambda: shell.configure(height=card.winfo_reqheight()+16)), add="+")
        shell.bind("<Enter>", lambda _e: redraw(), add="+")

        def on_card_enter(_event):
            shell.configure(highlightthickness=1, highlightbackground=LINE_STRONG)

        def on_card_leave(_event):
            shell.configure(highlightthickness=0)

        shell.bind("<Enter>", on_card_enter, add="+")
        shell.bind("<Leave>", on_card_leave, add="+")
        top = tk.Frame(card, bg=SURFACE)
        top.pack(fill="x", pady=(0, 5))
        badge = tk.Label(top, text=str(index), bg="#EEF1F4", fg=INK_500,
                         width=2, height=1, font=("Segoe UI", 10, "bold"))
        badge.pack(side="left")
        tk.Label(top, text=f"建议 {index}", bg=SURFACE, fg=INK_500,
                 font=("Segoe UI", 11), anchor="w").pack(side="left", padx=6)
        tk.Label(top, text=f"{len(content)} 字", bg=SURFACE, fg=INK_400,
                 font=("Segoe UI", 11), anchor="e").pack(side="right")

        body = tk.Label(
            card, text=content, anchor="w", justify="left", wraplength=460,
            bg=SURFACE, fg=INK_900, font=("Segoe UI", 10),
        )
        body.pack(fill="x", expand=False)
        card.bind("<Configure>", lambda event: body.configure(
            wraplength=max(120, event.width-24)), add="+")

        actions = tk.Frame(card, bg=SURFACE)
        actions.pack(fill="x", pady=(7, 0))
        acts = tk.Frame(actions, bg=SURFACE)
        acts.pack(side="right")

        copy_btn = _RoundButton(
            acts, text="复制", command=lambda t=content, n=index: copy_text(t, n),
            bg=SURFACE, fg=INK_700, font=("Segoe UI", 10),
        )
        copy_btn.pack(side="left", padx=(0, 6))
        fill_btn = _RoundButton(
            acts, text="回填微信", command=lambda t=content: choose(t),
            bg=GREEN_600, fg=SURFACE, font=("Segoe UI", 10, "bold"),
        )
        fill_btn.pack(side="left")
        fill_buttons[index] = fill_btn

        return card

    def copy_text(text, index):
        try:
            pyperclip.copy(text)
        except Exception:
            return
        show_toast(f"已复制第 {index} 条")

    for i, version in enumerate(versions, 1):
        make_card(i, str(version))

    def on_escape(_event=None):
        if toast.winfo_ismapped():
            toast.place_forget()
            if toast_id[0] is not None:
                root.after_cancel(toast_id[0])
                toast_id[0] = None
        else:
            root.destroy()
        return "break"

    root.bind("<Escape>", on_escape, add="+")
    for shortcut in "1234":
        root.bind(
            shortcut,
            lambda _e, n=int(shortcut):
                fill_buttons[n].invoke() if n in fill_buttons else None,
            add="+",
        )
    return inner


def _build_error(root, msg):
    """构建微信风格错误提示卡。"""
    _header(root, title="会聊")
    inner, _canvas = _scroll_area(root)

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


def _request_rounded_window(root):
    """给 Tk 顶层窗口设置原生窗口区域圆角（Win10/11）。"""
    try:
        import ctypes
        from ctypes import wintypes
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32

        def apply_region(_event=None):
            if not root.winfo_exists() or root.state() == "iconic":
                return
            class RECT(ctypes.Structure):
                _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                            ("right", wintypes.LONG), ("bottom", wintypes.LONG)]
            rect = RECT()
            hwnd = wintypes.HWND(root.winfo_id())
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return
            width, height = rect.right-rect.left, rect.bottom-rect.top
            region = gdi32.CreateRoundRectRgn(0, 0, width+1, height+1, 26, 26)
            if region:
                # Windows owns the HRGN after successful SetWindowRgn.
                if not user32.SetWindowRgn(hwnd, region, True):
                    gdi32.DeleteObject(region)

        root.after(80, apply_region)
        root.bind("<Configure>", lambda _e: root.after_idle(apply_region), add="+")
    except Exception:
        # Non-Windows environments keep their platform-native window shape.
        pass


def show_choices(versions, on_choose, master=None, on_close=None,
                 chat_name=None, prompt_name=None, prompt_id=None,
                 prompt_source="全局默认", prompts=None,
                 on_prompt_change=None):
    """弹出置顶小窗，展示多个润色版本（微信聊天框风格）。

    on_choose(text): 用户点“回填微信”时回调；参数为选定文本，回调后窗口关闭。
    master: 传入则作为 master 的 Toplevel 构建（不另起 mainloop，由主线程驱动）；
            None 则自建 Tk 并 mainloop（无托盘兜底场景）。
    on_close: 窗口销毁时回调（如 tray.show_idle()）。
    """
    if not versions:
        return
    root = tk.Toplevel(master) if master is not None else tk.Tk()
    root.title("AI 回复建议")
    root.attributes("-topmost", True)
    root.geometry("568x760")
    root.minsize(480, 360)
    root.resizable(True, True)
    try:
        root.iconify()       # 防止闪烁，随后立刻恢复
        root.deiconify()
    except Exception:
        pass
    inner = _build_choices(
        root, versions, on_choose, chat_name=chat_name,
        prompt_name=prompt_name, prompt_id=prompt_id,
        prompt_source=prompt_source, prompts=prompts,
        on_prompt_change=on_prompt_change,
    )
    root.update_idletasks()
    _request_rounded_window(root)
    _wire_close(root, on_close)
    if master is None:
        root.mainloop()


def show_error(msg, master=None, on_close=None):
    """弹出一个置顶错误/提示小窗，让失败可见（而不是静默无反应）。

    master / on_close 语义同 show_choices：传入 master 时作为主线程 Toplevel 构建，
    关闭时触发 on_close（如 tray.show_idle()）。
    """
    root = tk.Toplevel(master) if master is not None else tk.Tk()
    root.title("会聊 - 提示")
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
