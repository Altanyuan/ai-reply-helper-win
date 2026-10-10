"""设置页：提示词管理 + 模型与 API Key 管理。

打开方式：**鼠标中键（滚轮按下）**任意位置（由 main.py 的全局鼠标监听触发）。

页面结构
    ┌ 顶栏：标题 + 「提示词 / 模型与 Key / 软件作者」三个页签 ──────┐
    │ 左栏：列表或说明（提示词 / 模型 / 作者引导）                  │
    │ 右栏：对应内容（前两页为编辑表单，作者页为两张二维码卡片）    │
    └ 底栏：操作反馈（成功 / 失败 / 警告）+ 关闭 ────────────────┘

设计要点
  1. 「选中即编辑」：点左栏某一项 → 右栏载入该项内容；改完点保存。
  2. Key 默认**打码显示**，点「显示」才展开；列表与所有提示语一律脱敏。
  3. 校验走「字段标红 + 底栏文字提示」，不用弹窗打断操作
     （只有删除这样不可逆的动作才二次确认）。
  4. 布局用 grid 权重做自适应：窗口变大两栏一起变宽，缩到 720×560 仍可用；
     列表与长文本区域各自可滚动，内容多也不会撑破布局。
  5. 所有 tk 变量都显式绑定本页的 root，避免跨 Tcl 解释器（托盘主线程）访问。

存储说明：API Key 按当前约定**明文**保存在本机 config.json；界面负责脱敏展示，
分发 / 备份时不要带上该文件（打包脚本已排除）。
"""
import threading
import tkinter as tk
import tkinter.font as tkfont

import config
import key_utils
import paths

# ---- 浅色清爽风配色（2026-10-10 重设计：提示词工作室）----
BG = "#f6f8f9"
PANEL = "#ffffff"
CARD_BG = "#ffffff"
ACTIVE_BG = "#e6f7f2"
BORDER = "#e2e6ea"
ACTIVE_BORDER = "#0d9d80"
TEXT = "#1f2329"
MUTED = "#6b7280"
ACCENT = "#0d9d80"          # 主强调：深青（主按钮底 / 描边 / 激活态）
ACCENT_HOVER = "#0b8a70"
SECONDARY = "#eef1f4"       # 中性按钮底
SECONDARY_HOVER = "#dfe6ec"
OK = "#0d9d80"
WARN = "#9a6b00"
BAD = "#e5484d"
DANGER = "#e5484d"          # 危险描边按钮用
DANGER_HOVER = "#fdecec"    # 危险描边按钮悬停底
BADGE_BUILTIN_BG = "#eef2f5"
BADGE_CUSTOM_BG = "#e8f6f1"
ERR_BORDER = "#e5484d"

# ---- 按钮禁用态与行内间距 ----
# 禁用态：浅灰底 + 低对比文字，让「不可点」一目了然。
BTN_DISABLED_BG = "#f1f3f5"
BTN_DISABLED_FG = "#a6aeb7"
BTN_GAP = 8          # 同一行相邻按钮之间的水平间距（像素）

DOT_SIZE = 18
DOT_OFF_RING = "#c3cbd3"    # 浅色下用浅灰环，保证未选中态可见但不抢眼
DOT_ON_RING = "#0d9d80"
DOT_ON_FILL = "#0d9d80"

LABEL_FONT = ("Microsoft YaHei", 10)
TITLE_FONT = ("Microsoft YaHei", 13, "bold")
NAME_FONT = ("Microsoft YaHei", 11, "bold")
SMALL_FONT = ("Microsoft YaHei", 9)
MONO_FONT = ("Consolas", 10)

_EDITOR_FONT_PREF = ["Microsoft YaHei", "Microsoft YaHei UI", "Consolas",
                     "Cascadia Code", "Cascadia Mono"]


def _pick_editor_font(root, size=10):
    """挑一个本机已安装、支持中文的字体（必须传入本页 root 再查询）。"""
    try:
        families = set(tkfont.families(root))
    except Exception:
        families = set()
    for f in _EDITOR_FONT_PREF:
        if f in families:
            return (f, size)
    return ("Microsoft YaHei", size)


def _preview(text, n=72):
    t = (text or "").replace("\n", " ").strip()
    return (t[:n] + "…") if len(t) > n else t


# ---------------- 通用控件工厂 ----------------

def _mk_entry(parent, font=None):
    # width=1：让输入框只请求最小宽度，实际宽度交给 grid 的 sticky+weight 分配。
    # 否则 Entry 会按默认字符数要一大块地方，把右栏挤出窗口（实测过）。
    return tk.Entry(parent, width=1, bg=PANEL, fg=TEXT,
                    insertbackground="#1f2329", relief="solid",
                    borderwidth=1, highlightthickness=1,
                    highlightbackground=BORDER, highlightcolor=ACCENT,
                    font=font or LABEL_FONT)


def _mk_label(parent, text, fg=MUTED, font=None, **kw):
    return tk.Label(parent, text=text, bg=BG, fg=fg, font=font or LABEL_FONT,
                    anchor="w", **kw)


def _bind_wraplength(label, container, pad=0, min_w=90):
    """让 label 的换行宽度跟随容器实际宽度。

    为什么需要（踩坑）：固定 wraplength 不只是「换行宽度」，它同时是**请求宽度**
    的下限之一 —— 窄窗口下会把所在列撑宽，把两栏布局挤出窗口边界（实测）。
    跟随容器宽度后，宽窗口文字铺满、窄窗口自动收窄，两端都不会溢出。

    仅在数值真的变化时才下发 configure，避免与布局计算来回触发。
    """
    def _fit(_e=None):
        try:
            w = container.winfo_width() - pad
            if w >= min_w and int(label.cget("wraplength")) != w:
                label.configure(wraplength=w)
        except Exception:
            pass

    container.bind("<Configure>", _fit, add="+")
    return _fit


def _mk_btn(parent, text, cmd, kind="secondary", width=None, pad=10):
    """创建按钮，并挂上统一的「悬停 / 禁用」表现。

    - 悬停：底色切到该 kind 的 hover 色；同时保留 activebackground，
      鼠标按下也有反馈，两种状态颜色一致，不会互相打架。
    - 禁用：由 _set_btn_enabled() 统一切换（暗底 + 低对比文字）。
    调色板记在控件属性上，供状态切换时复原。
    """
    palette = {
        "primary": (ACCENT, "#ffffff", ACCENT_HOVER, None),
        "secondary": (SECONDARY, TEXT, SECONDARY_HOVER, None),
        "danger": (DANGER, "#ffffff", "#c7363c", None),
        # 描边按钮：白底 + 彩色细边框 + 彩色文字（浅色主题下的次级/危险动作）
        "outline": (PANEL, ACCENT, ACTIVE_BG, ACCENT),
        "danger_outline": (PANEL, DANGER, DANGER_HOVER, DANGER),
    }
    bg, fg, hover, outline = palette.get(kind, palette["secondary"])
    btn = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                    activebackground=hover, activeforeground=fg,
                    disabledforeground=BTN_DISABLED_FG,
                    relief="flat", bd=0, cursor="hand2", font=LABEL_FONT,
                    padx=pad, pady=5)
    if outline:
        # highlight 描边环：常驻显示（focus 与否都画），实现 1px 彩色边框
        btn.configure(highlightthickness=1, highlightbackground=outline,
                      highlightcolor=outline)
    if width:
        btn.configure(width=width)

    # 记录调色板，供 _set_btn_enabled 复原
    btn._kind_bg, btn._kind_fg, btn._kind_hover = bg, fg, hover

    def _on_enter(_e):
        if str(btn["state"]) != "disabled":
            btn.configure(bg=hover)

    def _on_leave(_e):
        if str(btn["state"]) != "disabled":
            btn.configure(bg=bg)

    btn.bind("<Enter>", _on_enter, add="+")
    btn.bind("<Leave>", _on_leave, add="+")
    return btn


def _set_btn_enabled(btn, enabled):
    """统一切换按钮可用态：禁用时用暗底 + 低对比文字，明确表达「不可点」。"""
    try:
        if enabled:
            btn.configure(state="normal", bg=btn._kind_bg, fg=btn._kind_fg,
                          cursor="hand2")
        else:
            btn.configure(state="disabled", bg=BTN_DISABLED_BG,
                          fg=BTN_DISABLED_FG, cursor="arrow")
    except Exception:
        pass


def _mk_btn_row(parent, specs, row, bg=None, pady=(0, 0)):
    """把若干按钮排成**一行**：等宽、等间距，并随右栏宽度自适应收缩。

    为什么用 grid + uniform 权重，而不是 pack(side="left")：
      pack 按各按钮的请求宽度依次排布，「删除 / 恢复默认 / 设为生效 / 保存」
      四个加起来约 430px，会把右栏顶出窗口（旧版正因此拆成了两行）。
      改成 grid 均匀列后每个按钮平分可用宽度，窗口再窄也不会溢出；
      同时所有按钮等宽、间距一致，视觉上更整齐统一。

    specs: [(text, cmd, kind), ...]
    返回按顺序排列的按钮列表，便于调用方保存引用以切换禁用态。
    """
    bar = tk.Frame(parent, bg=bg or PANEL)
    bar.grid(row=row, column=0, sticky="ew", pady=pady)
    btns = []
    last = len(specs) - 1
    for i, (text, cmd, kind) in enumerate(specs):
        col = i * 2
        bar.columnconfigure(col, weight=1, uniform="btnrow")
        # pad 收窄：四等宽按钮共享有限宽度，内部留白无需那么大，
        # 否则「恢复默认」这类四字按钮在窗口最窄时会被挤到裁字。
        b = _mk_btn(bar, text, cmd, kind, pad=6)
        b.grid(row=0, column=col, sticky="ew")
        btns.append(b)
        if i < last:
            # 用固定宽度的空白列充当间距，而不是给按钮加 padx ——
            # 加 padx 会把被减掉宽度的按钮压窄，造成同排按钮宽度不一
            # （实测首按钮 111px、其余 103px）；独立空列宽度不参与权重
            # 分配，可保证按钮完全等宽，整排两端与上方输入框严格对齐。
            sp = tk.Frame(bar, bg=bg or PANEL, width=BTN_GAP, height=1)
            sp.grid(row=0, column=col + 1, sticky="ns")
    return btns


class _ScrollList:
    """可滚动列表容器（Canvas + 内嵌 Frame），宽度自动跟随外层。"""

    def __init__(self, parent):
        self.outer = tk.Frame(parent, bg=BORDER, padx=1, pady=1)
        self.canvas = tk.Canvas(self.outer, bg=BG, highlightthickness=0, bd=0)
        bar = tk.Scrollbar(self.outer, orient="vertical",
                           command=self.canvas.yview, bg=PANEL, troughcolor=BG)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self._win = self.canvas.create_window((0, 0), window=self.inner,
                                              anchor="nw")
        self.canvas.configure(yscrollcommand=bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_canvas)
        # 滚轮绑在画布上：鼠标在哪个列表上就滚哪个，不用 bind_all
        self.canvas.bind("<MouseWheel>", self._on_wheel)

    def _on_canvas(self, event):
        w = event.width - 2
        self.canvas.itemconfig(self._win, width=w if w > 0 else event.width)

    def _on_wheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def clear(self):
        for child in self.inner.winfo_children():
            child.destroy()


class _Editor:
    """设置页控制器：提示词 + 模型与 Key 两个页签。"""

    def __init__(self, root, tab="author"):
        self.root = root
        self.editor_font = _pick_editor_font(root, 10)
        self.prompt_var = tk.StringVar(master=root)      # 选中的提示词 id
        self.model_var = tk.StringVar(master=root)       # 选中的模型 id
        self._prompt_id = None                           # None = 正在新增
        self._model_id = None
        self._prompt_rows = []
        self._model_rows = []
        self._testing = False
        self._flash_job = None
        self._qr_previews = {}                           # 底栏入口 -> 悬停预览浮层
        self._qr_states = {}                             # 资源名 -> 二维码卡片状态
        self._author_job = None                          # 作者页延迟渲染的 after 任务
        self._closing = False                            # 关窗中：不再调度任何 after 任务
        self._build()
        self._reload_prompts()
        self._reload_models()
        self._switch_tab(tab)

    # ================= 骨架 =================

    def _build(self):
        # 顶栏
        header = tk.Frame(self.root, bg=PANEL)
        header.pack(fill="x")
        tk.Frame(header, bg=ACCENT, width=4).pack(side="left", fill="y")
        tk.Label(header, text="✦ 提示词工作室", bg=PANEL, fg=TEXT,
                 font=TITLE_FONT, padx=12, pady=10).pack(side="left")

        # 页签（自绘，保持一致深色风格）
        tabs = tk.Frame(header, bg=PANEL)
        tabs.pack(side="left", padx=(18, 0))
        self.tab_btns = {}
        for key, label in (("prompts", "提示词"), ("models", "模型与 Key"),
                           ("author", "软件作者")):
            btn = tk.Label(tabs, text=label, bg=PANEL, fg=MUTED, font=NAME_FONT,
                           padx=14, pady=6, cursor="hand2")
            btn.pack(side="left", padx=(0, 4))
            btn.bind("<Button-1>", lambda _e, k=key: self._switch_tab(k))
            self.tab_btns[key] = btn

        tk.Label(header, text="改完点保存即时生效 · 无需重启",
                 bg=PANEL, fg=MUTED, font=SMALL_FONT,
                 padx=12).pack(side="right")

        # 底栏：必须在「内容区」之前 pack。
        # 踩坑：内容区 body 用了 expand=True，若先 pack，它会吃掉全部剩余空间；
        # 窗口高度不够时，后 pack 的底栏分不到位置，`winfo_ismapped()` 为 0，
        # 表现为**整条底栏消失**（没有状态栏、没有关闭按钮、也没有公众号入口）。
        # 先 pack 底栏可锁定其所需高度，内容区再占用剩下的空间。
        bottom = tk.Frame(self.root, bg=PANEL)
        bottom.pack(fill="x", side="bottom")
        self.status = tk.Label(bottom, text="", bg=PANEL, fg=MUTED,
                               font=SMALL_FONT, anchor="w", padx=14, pady=8,
                               wraplength=520, justify="left")
        self.status.pack(side="left", fill="x", expand=True)
        _mk_btn(bottom, "关闭", self._on_close, "secondary", width=8).pack(
            side="right", padx=10, pady=5)
        self._build_qr_entry(bottom)        # 「交流群 / 公众号」入口（在关闭左侧）

        # 内容区（占满底栏之外的剩余空间）
        self.body = tk.Frame(self.root, bg=BG)
        self.body.pack(fill="both", expand=True)
        self.tab_prompts = tk.Frame(self.body, bg=BG)
        self.tab_models = tk.Frame(self.body, bg=BG)
        self.tab_author = tk.Frame(self.body, bg=BG)
        self._build_prompts_tab()
        self._build_models_tab()
        self._build_author_tab()
        # 页签名 → 内容容器，供 _switch_tab 统一切换（今后新增页签只需在此登记）
        self.tabs = {
            "prompts": self.tab_prompts,
            "models": self.tab_models,
            "author": self.tab_author,
        }

        self.root.bind("<Escape>", lambda e: self._on_close())
        self.root.bind("<Control-s>", lambda e: self._save_current_tab())

    def _switch_tab(self, key):
        if key not in getattr(self, "tabs", {}):
            key = "author"
        for k, btn in self.tab_btns.items():
            active = (k == key)
            btn.configure(bg=CARD_BG if active else PANEL,
                          fg=TEXT if active else MUTED)
        for frame in self.tabs.values():
            frame.pack_forget()
        self.tabs[key].pack(fill="both", expand=True)
        self.current_tab = key
        if key == "author":
            # 页签首次显示时容器尺寸才确定，延迟一帧再按实际尺寸渲染二维码。
            # 任务 id 登记下来：重复切换不会堆叠多个回调，关窗时也能一并撤销。
            self._cancel_author_job()
            try:
                self._author_job = self.root.after(30, self._render_all_qr)
            except Exception:
                self._author_job = None
        self._show_default_status()

    def _save_current_tab(self):
        tab = getattr(self, "current_tab", "prompts")
        if tab == "prompts":
            self._on_prompt_save()
        elif tab == "models":
            self._on_model_save()
        else:
            self._flash("「软件作者」页无需保存，扫码加入交流群 / 关注公众号即可",
                        "info")

    def _on_close(self):
        # 必须先竖标志再撤销：destroy 过程中控件会连发 <Configure>，
        # 那些回调会「补建」新的防抖任务，撤了也白撤（实测仍会报错）。
        self._closing = True
        for prev in list(getattr(self, "_qr_previews", {}).values()):
            prev.hide()                      # 先收掉悬停浮层，避免留下残影
        self._cancel_jobs()                  # 再撤掉未触发的重绘 / 状态刷新回调
        try:
            self.root.quit()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def _cancel_author_job(self):
        if self._author_job is not None:
            try:
                self.root.after_cancel(self._author_job)
            except Exception:
                pass
            self._author_job = None

    def _cancel_jobs(self):
        """撤销所有已登记的 after 任务（关窗前调用）。

        为什么必须做（踩坑）：窗口 destroy 之后，pending 的回调仍会被 Tk 触发，
        打出 `invalid command name "..." while executing "after" script` ——
        源码运行（run.bat 控制台）时刷一屏红字，看起来像程序崩了。
        涉及到的地方：作者页延迟渲染、两张二维码的防抖重绘、底栏状态条复位。
        """
        self._cancel_author_job()
        for entry in getattr(self, "_qr_states", {}).values():
            st = entry.get("state") or {}
            job = st.get("job")
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
                st["job"] = None
        if self._flash_job is not None:
            try:
                self.root.after_cancel(self._flash_job)
            except Exception:
                pass
            self._flash_job = None

    # ---------- 底栏二维码入口（作者公众号 / 使用交流群）----------

    def _build_qr_entry(self, parent):
        """底栏二维码入口：点击弹大图，悬停浮出小预览。

        pack(side="right") 的顺序是「先入队的贴最右」，因此下面的书写顺序
        （交流群 → 作者公众号）在界面上呈现为「作者微信公众号 ｜ 使用交流群 ｜ 关闭」。

        资源缺失的入口直接不创建 —— 与其留一个点了没反应的链接，
        不如干净地不显示（打包漏带图片时就是这种情况）。
        """
        for asset, text in ((GROUP_QR_ASSET, "使用交流群"),
                            (AUTHOR_QR_ASSET, "作者微信公众号")):
            if not qr_available(asset):
                continue
            link = tk.Label(parent, text=text, bg=PANEL, fg=MUTED,
                            font=SMALL_FONT, cursor="hand2", padx=10, pady=8)
            link.pack(side="right")
            link.bind("<Button-1>", lambda _e, a=asset: self._open_qr(a))
            link.bind("<Enter>",
                      lambda _e, w=link, a=asset: self._on_qr_enter(w, a),
                      add="+")
            link.bind("<Leave>", lambda _e, w=link: self._on_qr_leave(w),
                      add="+")

    def _on_qr_enter(self, widget, asset):
        try:
            widget.configure(fg=TEXT)        # 悬停提亮，暗示可点击
        except Exception:
            pass
        prev = self._qr_previews.get(widget)
        if prev is None:
            pil = _load_qr_pil(asset)
            if pil is None:
                return
            prev = _QrHoverPreview(widget, pil)
            self._qr_previews[widget] = prev
        prev.schedule()

    def _on_qr_leave(self, widget):
        try:
            widget.configure(fg=MUTED)
        except Exception:
            pass
        prev = self._qr_previews.get(widget)
        if prev is not None:
            prev.hide()

    def _open_qr(self, asset):
        """点击入口 / 卡片 / 按钮：弹出可缩放的二维码大图，便于扫码。"""
        if _show_qr_dialog(self.root, asset=asset) is None:
            self._flash(f"未找到二维码图片 {asset}", "err")

    # ================= 提示词页签 =================

    def _build_prompts_tab(self):
        wrap = tk.Frame(self.tab_prompts, bg=BG)
        wrap.pack(fill="both", expand=True)
        wrap.columnconfigure(0, weight=2, minsize=250)
        wrap.columnconfigure(1, weight=3, minsize=330)
        wrap.rowconfigure(1, weight=1)

        # 状态条
        self.prompt_status = tk.Label(
            wrap, text="", bg=BG, fg=OK, font=LABEL_FONT, anchor="w",
            justify="left", wraplength=560)
        self.prompt_status.grid(row=0, column=0, columnspan=2, sticky="ew",
                                padx=14, pady=(10, 6))

        # 左栏：列表
        left = tk.Frame(wrap, bg=BG)
        left.grid(row=1, column=0, sticky="nsew", padx=(14, 7), pady=(0, 12))
        bar = tk.Frame(left, bg=BG)
        bar.pack(fill="x", pady=(0, 6))
        _mk_btn(bar, "+ 新增提示词", self._on_prompt_new, "primary").pack(side="left")
        # 搜索框：按名称 / 说明 / 内容过滤下方卡片
        self.p_search_var = tk.StringVar(master=self.root)
        self.p_search = _mk_entry(left, font=SMALL_FONT)
        self.p_search.pack(fill="x", pady=(0, 6), ipady=3)
        self.p_search_var.trace_add(
            "write", lambda *_: self._render_prompt_list(
                getattr(self, "_prompts_cache", []),
                getattr(self, "_active_id_cache", None),
                self.p_search_var.get()))
        self.plist = _ScrollList(left)
        self.plist.outer.pack(fill="both", expand=True)

        # 右栏：表单
        right = tk.Frame(wrap, bg=PANEL, padx=14, pady=12)
        right.grid(row=1, column=1, sticky="nsew", padx=(7, 14), pady=(0, 12))
        right.columnconfigure(0, weight=1)
        self.p_form_title = tk.Label(right, text="编辑提示词", bg=PANEL, fg=TEXT,
                                     font=NAME_FONT, anchor="w")
        self.p_form_title.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        _mk_label(right, "提示词名称", MUTED, SMALL_FONT).grid(
            row=1, column=0, sticky="ew")
        self.p_name = _mk_entry(right)
        self.p_name.grid(row=2, column=0, sticky="ew", pady=(2, 8), ipady=4)

        _mk_label(right, "一句话说明（可选）", MUTED, SMALL_FONT).grid(
            row=3, column=0, sticky="ew")
        self.p_desc = _mk_entry(right)
        self.p_desc.grid(row=4, column=0, sticky="ew", pady=(2, 8), ipady=4)

        _mk_label(right, "提示词内容（角色与风格设定）", MUTED, SMALL_FONT).grid(
            row=5, column=0, sticky="ew")
        self.p_content = tk.Text(right, height=10, width=1, bg=BG, fg=TEXT,
                                 insertbackground="#1f2329", relief="solid",
                                 borderwidth=1, highlightthickness=1,
                                 highlightbackground=BORDER,
                                 font=self.editor_font, wrap="word",
                                 padx=8, pady=6,
                                 selectbackground=ACCENT,
                                 selectforeground="#ffffff")
        self.p_content.grid(row=6, column=0, sticky="nsew", pady=(2, 4))
        right.rowconfigure(6, weight=1)
        self.p_content.bind("<KeyRelease>", lambda _e: self._update_char_count(),
                            add="+")

        # 变量 chips + 字数统计：chips 点击插入到光标处；
        # {{草稿}}/{{上下文}} 是排版锚点（提示词内的指代写法），草稿与上下文
        # 由程序自动附在提示词之后发送，见 p_hint 说明。
        chips = tk.Frame(right, bg=PANEL)
        chips.grid(row=7, column=0, sticky="ew", pady=(0, 4))
        for var in ("{{草稿}}", "{{上下文}}"):
            _mk_btn(chips, var,
                    lambda v=var: self.p_content.insert("insert", v),
                    "outline", pad=8).pack(side="left", padx=(0, 8))
        self.p_count = tk.Label(chips, text="0 / 2000 字符", bg=PANEL,
                                fg=MUTED, font=SMALL_FONT)
        self.p_count.pack(side="right")

        self.p_hint = tk.Label(right,
                               text="该内容会作为 system 提示词发给大模型；"
                                    "草稿与上下文由程序自动附在提示词之后，"
                                    "{{草稿}}/{{上下文}} 只是在提示词里指代它们。"
                                    "只有被设为「生效」的那一条才会真正使用。",
                               bg=PANEL, fg=MUTED, font=SMALL_FONT, anchor="w",
                               justify="left", wraplength=300)
        self.p_hint.grid(row=8, column=0, sticky="ew", pady=(0, 8))

        # 操作按钮：一行五个，等宽等距、随右栏宽度自适应收缩。
        # 顺序自左到右为「破坏性 → 辅助 → 主要动作」，保存靠右更顺手。
        (self.btn_p_delete, self.btn_p_reset, self.btn_p_trial,
         self.btn_p_activate, self.btn_p_save) = _mk_btn_row(
            right,
            [("删除", self._on_prompt_delete, "danger_outline"),
             ("恢复默认", self._on_prompt_reset, "secondary"),
             ("试运行", self._on_prompt_trial, "outline"),
             ("设为生效", self._on_prompt_activate, "secondary"),
             ("保存", self._on_prompt_save, "primary")],
            row=9)

    def _reload_prompts(self, keep_id=None):
        prompts = config.get_prompts()
        active_id = config.get_active_prompt_id()
        self._prompts_cache = prompts          # 搜索过滤用
        self._active_id_cache = active_id
        self.prompt_status.configure(
            text=f"当前生效：{self._name_of_prompt(active_id)}"
                 f"　｜　共 {len(prompts)} 条"
                 f"（内置 {sum(1 for p in prompts if p.get('builtin'))}"
                 f" / 自定义 {sum(1 for p in prompts if not p.get('builtin'))}）",
            fg=OK)
        self._render_prompt_list(prompts, active_id,
                                 self.p_search_var.get()
                                 if hasattr(self, "p_search_var") else "")

        target = keep_id or self._prompt_id or active_id
        ids = [p.get("id") for p in prompts]
        self._select_prompt(target if target in ids else (ids[0] if ids else None))

    def _name_of_prompt(self, pid):
        for p in config.get_prompts():
            if p.get("id") == pid:
                return p.get("name", "未命名")
        return "—"

    def _render_prompt_list(self, prompts, active_id, keyword=""):
        self.plist.clear()
        self._prompt_rows = []
        kw = (keyword or "").strip().lower()
        shown = [p for p in prompts
                 if not kw or kw in (p.get("name", "") + " "
                                     + p.get("desc", "") + " "
                                     + p.get("content", "")).lower()]
        if not shown:
            _mk_label(self.plist.inner,
                      "（没有匹配的提示词，换个关键词试试）" if kw
                      else "（还没有任何提示词）",
                      MUTED, SMALL_FONT).pack(padx=10, pady=10)
            return
        for p in shown:
            self._make_prompt_row(p, active_id)

    def _update_char_count(self):
        """编辑区字数统计（仅展示，不限流）。"""
        try:
            n = len(self.p_content.get("1.0", "end-1c"))
            self.p_count.configure(text=f"{n} / 2000 字符")
        except Exception:
            pass

    def _make_prompt_row(self, p, active_id):
        pid = p.get("id", "")
        is_builtin = bool(p.get("builtin", False))
        is_active = (pid == active_id)

        frame = tk.Frame(self.plist.inner, bg=BORDER, padx=1)
        frame.pack(fill="x", pady=3, padx=2)
        body = tk.Frame(frame, bg=CARD_BG)
        body.pack(fill="x")
        bar = tk.Frame(body, bg=ACTIVE_BORDER if is_active else BORDER, width=4)
        bar.pack(side="left", fill="y")
        dot = tk.Canvas(body, width=DOT_SIZE, height=DOT_SIZE, bg=CARD_BG,
                        highlightthickness=0, bd=0, cursor="hand2")
        dot.pack(side="left", padx=(10, 6), pady=10)
        mid = tk.Frame(body, bg=CARD_BG)
        mid.pack(side="left", fill="x", expand=True, pady=7)

        top = tk.Frame(mid, bg=CARD_BG)
        top.pack(fill="x")
        name_lbl = tk.Label(top, text=p.get("name", "未命名"), bg=CARD_BG,
                            fg=TEXT, font=LABEL_FONT, anchor="w")
        name_lbl.pack(side="left")
        tk.Label(top, text="内置" if is_builtin else "自定义",
                 bg=BADGE_BUILTIN_BG if is_builtin else BADGE_CUSTOM_BG,
                 fg=TEXT, font=SMALL_FONT, padx=5, pady=1).pack(side="left",
                                                                padx=(6, 0))
        active_tag = tk.Label(top, text="✓ 生效", bg=CARD_BG, fg=OK,
                              font=SMALL_FONT)
        if is_active:
            active_tag.pack(side="left", padx=(6, 0))

        # wraplength 固定：列表文字的请求宽度不能超过左栏，否则会把右栏挤走
        desc = p.get("desc", "")
        if desc:
            tk.Label(mid, text=_preview(desc, 40), bg=CARD_BG, fg=MUTED,
                     font=SMALL_FONT, anchor="w", justify="left",
                     wraplength=200).pack(fill="x")
        tk.Label(mid, text=_preview(p.get("content", ""), 40), bg=CARD_BG,
                 fg=MUTED, font=SMALL_FONT, anchor="w", justify="left",
                 wraplength=200).pack(fill="x")

        def _pick(_e, pid=pid):
            self._select_prompt(pid)
        for w in (frame, body, bar, dot, mid, name_lbl):
            w.bind("<Button-1>", _pick)

        self._draw_dot(dot, is_active, CARD_BG)
        self._prompt_rows.append({"id": pid, "frame": frame, "bar": bar,
                                  "body": body, "dot": dot})

    def _draw_dot(self, canvas, active, bg):
        try:
            canvas.configure(bg=bg)
            canvas.delete("all")
            s = DOT_SIZE
            if active:
                canvas.create_oval(2, 2, s - 2, s - 2, outline=DOT_ON_RING,
                                   width=2, fill=bg)
                canvas.create_oval(6, 6, s - 6, s - 6, outline=DOT_ON_FILL,
                                   width=0, fill=DOT_ON_FILL)
            else:
                canvas.create_oval(2, 2, s - 2, s - 2, outline=DOT_OFF_RING,
                                   width=2, fill=bg)
        except Exception:
            pass

    def _select_prompt(self, pid):
        self._prompt_id = pid
        self.prompt_var.set(pid or "")
        p = self._find_prompt(pid)
        if p is None:
            self.p_form_title.configure(text="新增提示词（填写后点保存）")
            self.p_name.delete(0, "end")
            self.p_desc.delete(0, "end")
            self.p_content.delete("1.0", "end")
            self._clear_field_errors()
            self._sync_prompt_buttons()
            self._update_char_count()
            return
        builtin = "（内置）" if p.get("builtin") else ""
        self.p_form_title.configure(text=f"编辑提示词{builtin}")
        self.p_name.delete(0, "end")
        self.p_name.insert(0, p.get("name", ""))
        self.p_desc.delete(0, "end")
        self.p_desc.insert(0, p.get("desc", ""))
        self.p_content.delete("1.0", "end")
        self.p_content.insert("1.0", p.get("content", ""))
        self._clear_field_errors()
        self._sync_prompt_buttons()
        self._update_char_count()

    def _sync_prompt_buttons(self):
        """按当前选中项刷新提示词页五个按钮的可用态。

        - 删除：内置项不可删；新增模式（未选中）也没有可删对象。
        - 恢复默认：只对内置项才成立。
        - 试运行：始终可用（用编辑器当前文本试跑）。
        - 设为生效：本就是生效项、或尚未保存时都无需再点。
        - 保存：始终可用（新增与修改都靠它落盘）。
        这样用户从按钮的明暗就能直接看出「这一步能不能做」，
        不必先点一下再被底栏的提示语告知。
        """
        pid = self._prompt_id
        p = self._find_prompt(pid) if pid else None
        is_builtin = bool(p.get("builtin")) if p else False
        is_active = bool(pid) and pid == config.get_active_prompt_id()
        _set_btn_enabled(self.btn_p_delete, bool(p) and not is_builtin)
        _set_btn_enabled(self.btn_p_reset, is_builtin)
        _set_btn_enabled(self.btn_p_activate, bool(p) and not is_active)
        _set_btn_enabled(self.btn_p_save, True)

    def _find_prompt(self, pid):
        if not pid:
            return None
        for p in config.get_prompts():
            if p.get("id") == pid:
                return p
        return None

    def _on_prompt_new(self):
        self._select_prompt(None)
        self.p_name.focus_set()
        self._flash("正在新增提示词：填写名称与内容后点「保存」", "info")

    def _on_prompt_save(self):
        name = self.p_name.get().strip()
        content = self.p_content.get("1.0", "end").strip()
        desc = self.p_desc.get().strip()
        if not self._validate([(self.p_name, name, "请填写提示词名称"),
                               (self.p_content, content, "请填写提示词内容")]):
            return
        try:
            if self._prompt_id:
                config.update_prompt(self._prompt_id, name, content, desc)
                msg = f"已保存提示词「{name}」"
                keep = self._prompt_id
            else:
                keep = config.add_prompt(name, content, desc)
                msg = f"已新增提示词「{name}」（点「设为生效」才会使用它）"
            self._reload_prompts(keep_id=keep)
            self._flash(msg, "ok")
        except Exception as e:
            self._flash(f"保存失败：{e}", "err")

    def _on_prompt_reset(self):
        pid = self._prompt_id
        if not pid:
            self._flash("内置提示词才能恢复默认；当前是新增模式", "warn")
            return
        if not self._find_prompt(pid).get("builtin"):
            self._flash("仅内置提示词支持「恢复默认」", "warn")
            return
        if not self._confirm(
                "恢复默认",
                f"把「{self._name_of_prompt(pid)}」的内容恢复为出厂设置？\n"
                "你对该项做的修改会丢失。"):
            return
        config.reset_prompt(pid)
        self._reload_prompts(keep_id=pid)
        self._flash(f"已恢复默认内容：{self._name_of_prompt(pid)}", "ok")

    def _on_prompt_delete(self):
        pid = self._prompt_id
        if not pid:
            self._flash("当前没有选中已保存的提示词", "warn")
            return
        p = self._find_prompt(pid)
        if p.get("builtin"):
            self._flash("内置提示词不可删除（可以「恢复默认」）", "warn")
            return
        if not self._confirm("删除确认",
                             f"确定删除提示词「{p.get('name')}」？此操作不可撤销。"):
            return
        config.delete_prompt(pid)
        self._prompt_id = None
        self._reload_prompts()
        self._flash(f"已删除提示词「{p.get('name')}」", "ok")

    def _on_prompt_activate(self):
        pid = self._prompt_id
        if not pid:
            self._flash("请先在左侧选中一条已保存的提示词", "warn")
            return
        config.set_active_prompt(pid)
        self._reload_prompts(keep_id=pid)
        self._flash(f"已切换生效提示词：{self._name_of_prompt(pid)}", "ok")

    # ================= 试运行 =================

    def _on_prompt_trial(self):
        """试运行当前编辑器里的提示词（即使还没点保存）。

        用编辑器当前文本临时替换配置中的对应项，调 ai_client.polish；
        未保存的新增模式则挂一个临时 id。网络请求走后台线程，
        结果通过 root.after 回到 UI 线程展示（tkinter 线程规则）。
        """
        content = self.p_content.get("1.0", "end").strip()
        name = self.p_name.get().strip() or "未命名"
        if not content:
            self._flash("请先填写提示词内容再试运行", "warn")
            return
        self._open_trial_dialog(name, content)

    def _open_trial_dialog(self, name, content):
        top = tk.Toplevel(self.root)
        top.title(f"试运行：{name}")
        top.configure(bg=BG)
        top.geometry("680x520")
        top.minsize(560, 420)
        try:
            top.attributes("-topmost", True)
        except Exception:
            pass

        header = tk.Frame(top, bg=PANEL)
        header.pack(fill="x")
        tk.Frame(header, bg=ACCENT, width=4).pack(side="left", fill="y")
        tk.Label(header, text=f"✦ 试运行：{name}", bg=PANEL, fg=TEXT,
                 font=TITLE_FONT, padx=12, pady=10).pack(side="left")
        tk.Label(header, text="用当前编辑器文本试跑，不影响已保存内容",
                 bg=PANEL, fg=MUTED, font=SMALL_FONT, padx=12).pack(side="right")

        body = tk.Frame(top, bg=BG, padx=14, pady=10)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(3, weight=1)

        _mk_label(body, "示例草稿（模拟微信输入框里的内容）",
                  MUTED, SMALL_FONT).grid(row=0, column=0, sticky="ew")
        _mk_label(body, "示例上下文（模拟对方最近发言，可空）",
                  MUTED, SMALL_FONT).grid(row=0, column=1, sticky="ew",
                                          padx=(8, 0))
        draft_txt = tk.Text(body, height=5, width=1, bg=PANEL, fg=TEXT,
                            insertbackground="#1f2329", relief="solid",
                            borderwidth=1, highlightthickness=1,
                            highlightbackground=BORDER,
                            highlightcolor=ACCENT,
                            font=self.editor_font, wrap="word", padx=8, pady=6)
        draft_txt.grid(row=1, column=0, sticky="nsew", pady=(2, 8))
        ctx_txt = tk.Text(body, height=5, width=1, bg=PANEL, fg=TEXT,
                          insertbackground="#1f2329", relief="solid",
                          borderwidth=1, highlightthickness=1,
                          highlightbackground=BORDER,
                          highlightcolor=ACCENT,
                          font=self.editor_font, wrap="word", padx=8, pady=6)
        ctx_txt.grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(2, 8))
        draft_txt.insert("1.0", "不好意思，这个需求可能要延期两天")

        btn_bar = tk.Frame(body, bg=BG)
        btn_bar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        run_btn = _mk_btn(btn_bar, "▶ 运行", None, "primary")
        run_btn.pack(side="left")
        _mk_btn(btn_bar, "关闭", top.destroy, "secondary").pack(
            side="left", padx=(8, 0))
        status = tk.Label(btn_bar, text="", bg=BG, fg=MUTED, font=SMALL_FONT)
        status.pack(side="left", padx=(12, 0))

        _mk_label(body, "运行结果", MUTED, SMALL_FONT).grid(
            row=3, column=0, columnspan=2, sticky="ew")
        result = tk.Text(body, width=1, bg=PANEL, fg=TEXT, relief="solid",
                         borderwidth=1, highlightthickness=1,
                         highlightbackground=BORDER,
                         font=self.editor_font, wrap="word", padx=8, pady=6,
                         state="disabled")
        result.grid(row=4, column=0, columnspan=2, sticky="nsew", pady=(2, 0))

        state = {"running": False, "dead": False}
        top.bind("<Destroy>",
                 lambda e: state.update(dead=True)
                 if str(e.widget) == str(top) else None)

        def _show_result(text, kind="info"):
            try:
                result.configure(state="normal")
                result.delete("1.0", "end")
                result.insert("1.0", text)
                result.configure(state="disabled",
                                 fg=BAD if kind == "err" else TEXT)
                status.configure(text="")
            except Exception:
                pass

        def _on_done(ok, payload):
            state["running"] = False
            try:
                run_btn.configure(text="▶ 运行")
                _set_btn_enabled(run_btn, True)
            except Exception:
                pass
            if state["dead"]:
                return
            if ok:
                lines = []
                for i, v in enumerate(payload, 1):
                    lines.append(f"—— 版本 {i} ——\n{v.strip()}")
                _show_result("\n\n".join(lines) if lines else "（无结果）")
                self._flash("试运行完成", "ok")
            else:
                _show_result(f"运行失败：\n{payload}", "err")
                self._flash("试运行失败，见对话框内提示", "err")

        def _run():
            if state["running"]:
                return
            draft = draft_txt.get("1.0", "end").strip()
            ctx = ctx_txt.get("1.0", "end").strip()
            if not draft and not ctx:
                status.configure(text="请至少填写示例草稿或上下文", fg=WARN)
                return
            state["running"] = True
            run_btn.configure(text="运行中…")
            _set_btn_enabled(run_btn, False)
            status.configure(text="正在请求大模型…", fg=MUTED)

            # 构造试运行配置：编辑器当前文本临时替换对应提示词
            import copy
            from config import load_config
            cfg = copy.deepcopy(load_config())
            prompts = cfg.get("prompts") or []
            pid = self._prompt_id
            if pid and any(p.get("id") == pid for p in prompts):
                for p in prompts:
                    if p.get("id") == pid:
                        p["content"] = content
            else:
                prompts.append({"id": "__trial__", "name": name,
                                "content": content, "builtin": False})
                cfg["active_prompt_id"] = "__trial__"
            cfg["prompts"] = prompts

            def worker():
                try:
                    from ai_client import polish
                    versions = polish(draft, ctx, cfg)
                    done = (True, versions)
                except Exception as e:
                    done = (False, str(e).splitlines()[0][:300] if str(e)
                            else "未知错误")
                try:
                    self.root.after(0, lambda: _on_done(*done))
                except Exception:
                    pass

            import threading
            threading.Thread(target=worker, daemon=True).start()

        run_btn.configure(command=_run)

    # ================= 模型与 Key 页签 =================

    def _build_models_tab(self):
        wrap = tk.Frame(self.tab_models, bg=BG)
        wrap.pack(fill="both", expand=True)
        wrap.columnconfigure(0, weight=2, minsize=250)
        wrap.columnconfigure(1, weight=3, minsize=330)
        wrap.rowconfigure(1, weight=1)

        self.model_status = tk.Label(wrap, text="", bg=BG, fg=OK,
                                     font=LABEL_FONT, anchor="w", justify="left",
                                     wraplength=560)
        self.model_status.grid(row=0, column=0, columnspan=2, sticky="ew",
                               padx=14, pady=(10, 6))

        left = tk.Frame(wrap, bg=BG)
        left.grid(row=1, column=0, sticky="nsew", padx=(14, 7), pady=(0, 12))
        bar = tk.Frame(left, bg=BG)
        bar.pack(fill="x", pady=(0, 6))
        _mk_btn(bar, "+ 新增模型", self._on_model_new, "primary").pack(side="left")
        self.mlist = _ScrollList(left)
        self.mlist.outer.pack(fill="both", expand=True)

        right = tk.Frame(wrap, bg=PANEL, padx=14, pady=12)
        right.grid(row=1, column=1, sticky="nsew", padx=(7, 14), pady=(0, 12))
        right.columnconfigure(0, weight=1)
        self.m_form_title = tk.Label(right, text="编辑模型", bg=PANEL, fg=TEXT,
                                     font=NAME_FONT, anchor="w")
        self.m_form_title.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        _mk_label(right, "配置名称（自己看的备注名）", MUTED, SMALL_FONT).grid(
            row=1, column=0, sticky="ew")
        self.m_name = _mk_entry(right)
        self.m_name.grid(row=2, column=0, sticky="ew", pady=(2, 8), ipady=4)

        _mk_label(right, "API 地址（以 http:// 或 https:// 开头）", MUTED,
                  SMALL_FONT).grid(row=3, column=0, sticky="ew")
        self.m_base = _mk_entry(right)
        self.m_base.grid(row=4, column=0, sticky="ew", pady=(2, 8), ipady=4)

        _mk_label(right, "模型名（如 deepseek-chat）", MUTED, SMALL_FONT).grid(
            row=5, column=0, sticky="ew")
        self.m_model = _mk_entry(right)
        self.m_model.grid(row=6, column=0, sticky="ew", pady=(2, 8), ipady=4)

        key_head = tk.Frame(right, bg=PANEL)
        key_head.grid(row=7, column=0, sticky="ew")
        _mk_label(key_head, "API Key", MUTED, SMALL_FONT).pack(side="left")
        self.key_visible = False
        self.key_toggle = tk.Label(key_head, text="显示", bg=PANEL, fg=ACCENT,
                                   font=SMALL_FONT, cursor="hand2")
        self.key_toggle.pack(side="left", padx=(8, 0))
        self.key_toggle.bind("<Button-1>", lambda e: self._toggle_key_visible())
        self.key_hint = tk.Label(key_head, text="", bg=PANEL, fg=MUTED,
                                 font=SMALL_FONT)
        self.key_hint.pack(side="right")

        self.m_key = tk.Entry(right, bg=PANEL, fg=TEXT,
                              insertbackground="#1f2329", relief="solid",
                              borderwidth=1, highlightthickness=1,
                              highlightbackground=BORDER,
                              highlightcolor=ACCENT, font=MONO_FONT, show="•")
        self.m_key.grid(row=8, column=0, sticky="ew", pady=(2, 4), ipady=4)

        self.m_hint = tk.Label(right,
                               text="Key 只保存在本机 config.json 中（明文），"
                                    "界面默认打码显示。\n"
                                    "请勿把该文件发给他人或上传到网盘。",
                               bg=PANEL, fg=MUTED, font=SMALL_FONT, anchor="w",
                               justify="left", wraplength=300)
        self.m_hint.grid(row=9, column=0, sticky="ew", pady=(0, 10))
        # 说明：这里刻意用「固定换行宽度」而不是跟随容器宽度 —— 动态跟随会让
        # Label 的请求宽度和 grid 列宽互相追赶，实测把右栏顶出窗口。宁可宽窗口
        # 下多留点留白，也不要内容被裁掉。

        # 操作按钮：与提示词页一致，一行四个等宽等距、随右栏自适应。
        # 保留 self.test_btn 这一名字，测试连接流程仍在用它切换状态。
        (self.btn_m_delete, self.test_btn,
         self.btn_m_activate, self.btn_m_save) = _mk_btn_row(
            right,
            [("删除", self._on_model_delete, "danger_outline"),
             ("测试连接", self._on_model_test, "secondary"),
             ("设为生效", self._on_model_activate, "secondary"),
             ("保存", self._on_model_save, "primary")],
            row=10)

    def _reload_models(self, keep_id=None):
        models = config.get_models()
        active_id = config.get_active_model_id()
        active = next((m for m in models if m.get("id") == active_id), None)
        self.model_status.configure(
            text=f"当前生效：{active.get('name') if active else '（未配置）'}"
                 f"　｜　共 {len(models)} 条模型配置"
                 f"　｜　Key：{key_utils.mask(active.get('api_key')) if active else '（未设置）'}",
            fg=OK)
        self._render_model_list(models, active_id)

        ids = [m.get("id") for m in models]
        target = keep_id or self._model_id or active_id
        self._select_model(target if target in ids else (ids[0] if ids else None))

    def _render_model_list(self, models, active_id):
        self.mlist.clear()
        self._model_rows = []
        if not models:
            _mk_label(self.mlist.inner, "（还没有模型配置，点上方「新增模型」）",
                      MUTED, SMALL_FONT).pack(padx=10, pady=10)
            return
        for m in models:
            self._make_model_row(m, active_id)

    def _make_model_row(self, m, active_id):
        mid = m.get("id", "")
        is_active = (mid == active_id)
        has_key = bool(m.get("api_key"))

        frame = tk.Frame(self.mlist.inner, bg=BORDER, padx=1)
        frame.pack(fill="x", pady=3, padx=2)
        body = tk.Frame(frame, bg=CARD_BG)
        body.pack(fill="x")
        bar = tk.Frame(body, bg=ACTIVE_BORDER if is_active else BORDER, width=4)
        bar.pack(side="left", fill="y")
        dot = tk.Canvas(body, width=DOT_SIZE, height=DOT_SIZE, bg=CARD_BG,
                        highlightthickness=0, bd=0, cursor="hand2")
        dot.pack(side="left", padx=(10, 6), pady=10)
        box = tk.Frame(body, bg=CARD_BG)
        box.pack(side="left", fill="x", expand=True, pady=7)

        top = tk.Frame(box, bg=CARD_BG)
        top.pack(fill="x")
        name_lbl = tk.Label(top, text=m.get("name", "未命名模型"), bg=CARD_BG,
                            fg=TEXT, font=LABEL_FONT, anchor="w")
        name_lbl.pack(side="left")
        if is_active:
            tk.Label(top, text="✓ 生效", bg=CARD_BG, fg=OK,
                     font=SMALL_FONT).pack(side="left", padx=(6, 0))
        if not has_key:
            tk.Label(top, text="未配置 Key", bg=CARD_BG, fg=WARN,
                     font=SMALL_FONT).pack(side="left", padx=(6, 0))

        tk.Label(box, text=m.get("model", "") or "（未填写模型名）", bg=CARD_BG,
                 fg=MUTED, font=SMALL_FONT, anchor="w", justify="left",
                 wraplength=200).pack(fill="x")
        tk.Label(box, text=key_utils.mask(m.get("api_key")), bg=CARD_BG,
                 fg=MUTED, font=MONO_FONT, anchor="w").pack(fill="x")

        def _pick(_e, mid=mid):
            self._select_model(mid)
        for w in (frame, body, bar, dot, box, name_lbl):
            w.bind("<Button-1>", _pick)

        self._draw_dot(dot, is_active, CARD_BG)
        self._model_rows.append({"id": mid, "frame": frame, "bar": bar,
                                 "body": body, "dot": dot})

    def _select_model(self, mid):
        self._model_id = mid
        self.model_var.set(mid or "")
        m = self._find_model(mid)
        if m is None:
            self.m_form_title.configure(text="新增模型（填写后点保存）")
            self.m_name.delete(0, "end")
            self.m_base.delete(0, "end")
            self.m_base.insert(0, config.DEFAULTS["api_base"])
            self.m_model.delete(0, "end")
            self.m_model.insert(0, config.DEFAULTS["model"])
            self.m_key.delete(0, "end")
            self._set_key_visible(False)
            self.key_hint.configure(text="")
            self._clear_field_errors()
            self._sync_model_buttons()
            return
        self.m_form_title.configure(text="编辑模型"
                                        + ("（当前生效）" if
                                           mid == config.get_active_model_id() else ""))
        self.m_name.delete(0, "end")
        self.m_name.insert(0, m.get("name", ""))
        self.m_base.delete(0, "end")
        self.m_base.insert(0, m.get("api_base", ""))
        self.m_model.delete(0, "end")
        self.m_model.insert(0, m.get("model", ""))
        self.m_key.delete(0, "end")
        self.m_key.insert(0, m.get("api_key", ""))
        self._set_key_visible(False)
        self.key_hint.configure(
            text=("已保存：" + key_utils.mask(m.get("api_key"))) if m.get("api_key")
            else "尚未配置 Key")
        self._clear_field_errors()
        self._sync_model_buttons()

    def _sync_model_buttons(self):
        """按当前选中项刷新模型页四个按钮的可用态。

        - 删除：只有已保存的模型才可删（新增模式下无可删对象）。
        - 测试连接：仅在测试进行中禁用；未填 Key 时仍可点，
          由字段标红给出更明确的缺项提示（比直接禁用更利于排查）。
        - 设为生效：本就是生效项、或尚未保存时无需再点。
        - 保存：始终可用。
        """
        mid = self._model_id
        m = self._find_model(mid) if mid else None
        is_active = bool(mid) and mid == config.get_active_model_id()
        _set_btn_enabled(self.btn_m_delete, bool(m))
        _set_btn_enabled(self.test_btn, not self._testing)
        _set_btn_enabled(self.btn_m_activate, bool(m) and not is_active)
        _set_btn_enabled(self.btn_m_save, True)

    def _find_model(self, mid):
        if not mid:
            return None
        for m in config.get_models():
            if m.get("id") == mid:
                return m
        return None

    def _set_key_visible(self, visible):
        self.key_visible = visible
        try:
            self.m_key.configure(show="" if visible else "•")
            self.key_toggle.configure(text="隐藏" if visible else "显示")
        except Exception:
            pass

    def _toggle_key_visible(self):
        self._set_key_visible(not self.key_visible)
        self._flash("Key 已" + ("显示（注意周围是否有他人）" if self.key_visible
                                else "隐藏"), "info")

    def _on_model_new(self):
        self._select_model(None)
        self.m_name.focus_set()
        self._flash("正在新增模型：填写名称、地址、模型名与 Key 后点「保存」", "info")

    def _collect_model_form(self):
        return {
            "id": self._model_id,
            "name": self.m_name.get().strip(),
            "api_base": self.m_base.get().strip(),
            "model": self.m_model.get().strip(),
            "api_key": self.m_key.get().strip(),
            "note": (self._find_model(self._model_id) or {}).get("note", ""),
        }

    def _validate_model(self, data, need_key=False):
        checks = [
            (self.m_name, data["name"], "请填写配置名称"),
            (self.m_base, data["api_base"], "请填写 API 地址"),
            (self.m_model, data["model"], "请填写模型名"),
        ]
        if need_key:
            checks.append((self.m_key, data["api_key"], "请先填写 API Key"))
        if not self._validate(checks):
            return False
        if not key_utils.is_http_url(data["api_base"]):
            self._mark_error(self.m_base)
            self._flash("API 地址需以 http:// 或 https:// 开头", "warn")
            return False
        if data["api_key"] and not key_utils.looks_like_key(data["api_key"]):
            self._flash("提示：这个 Key 看起来偏短或含空格，请确认复制完整"
                        "（多数以 sk- 开头，但兼容服务可能不同）", "warn")
        return True

    def _on_model_save(self):
        data = self._collect_model_form()
        if not self._validate_model(data):
            return
        before = {m.get("id") for m in config.get_models()}
        try:
            ok, msg = config.save_model(data)
        except Exception as e:
            self._flash(f"保存失败：{e}", "err")
            return
        if not ok:
            self._flash(msg, "err")
            return
        # 新增（没有 id）时，把焦点落到刚创建的那条，方便继续配置
        keep = data["id"]
        if not keep:
            added = [m for m in config.get_models()
                     if m.get("id") not in before]
            keep = added[0].get("id") if added else None
        self._reload_models(keep_id=keep)
        self._flash(msg, "ok")

    def _on_model_delete(self):
        if not self._model_id:
            self._flash("当前没有选中已保存的模型", "warn")
            return
        name = (self._find_model(self._model_id) or {}).get("name", "")
        if not self._confirm("删除确认",
                             f"确定删除模型配置「{name}」？此操作不可撤销。"):
            return
        ok, msg = config.delete_model(self._model_id)
        if not ok:
            self._flash(msg, "warn")
            return
        self._model_id = None
        self._reload_models()
        self._flash(msg, "ok")

    def _on_model_activate(self):
        if not self._model_id:
            self._flash("请先在左侧选中一条已保存的模型", "warn")
            return
        ok, msg = config.set_active_model(self._model_id)
        self._reload_models(keep_id=self._model_id)
        self._flash(msg, "ok" if ok else "warn")

    def _on_model_test(self):
        if self._testing:
            return
        data = self._collect_model_form()
        if not self._validate_model(data, need_key=True):
            return
        self._testing = True
        self.test_btn.configure(text="测试中…")
        _set_btn_enabled(self.test_btn, False)      # 统一走禁用态样式
        self._flash(f"正在用 {key_utils.mask(data['api_key'])} 连接大模型…", "info")
        cfg = {"api_key": data["api_key"], "api_base": data["api_base"],
               "model": data["model"]}

        def worker():
            try:
                from ai_client import test_api_key
                ok, msg = test_api_key(cfg)
            except Exception as e:
                ok, msg = False, f"测试失败：{e}"
            try:
                self.root.after(0, lambda: self._on_test_done(ok, msg))
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _on_test_done(self, ok, msg):
        self._testing = False
        try:
            self.test_btn.configure(text="测试连接")
        except Exception:
            pass
        self._sync_model_buttons()                  # 复原可用态与配色
        self._flash(("连接成功：" if ok else "") + msg, "ok" if ok else "err")

    # ================= 软件作者页签 =================

    def _build_author_tab(self):
        """「软件作者」页：作者信息 + 使用交流群 / 作者公众号两张二维码卡片。

        布局沿用「提示词 / 模型与 Key」两页的两栏 grid 权重（左说明 / 右内容），
        右栏再对半分成两张等宽卡片：每张各自持有白底二维码、独立按容器尺寸
        重绘（都限制最大边长）。因此小屏能扫、大屏不糊，也不会把布局顶破。
        """
        wrap = tk.Frame(self.tab_author, bg=BG)
        wrap.pack(fill="both", expand=True)
        wrap.columnconfigure(0, weight=1, minsize=244)
        wrap.columnconfigure(1, weight=2, minsize=372)
        wrap.rowconfigure(1, weight=1)

        # 状态条（与其它页签同一位置语义）
        self.author_status = tk.Label(
            wrap, text="扫码加入使用交流群、关注作者公众号，获取更新与帮助",
            bg=BG, fg=OK, font=LABEL_FONT, anchor="w", justify="left",
            wraplength=560)
        self.author_status.grid(row=0, column=0, columnspan=2, sticky="ew",
                                padx=14, pady=(10, 6))

        # ---- 左栏：作者信息 + 引导文案 + 两个入口按钮 ----
        left = tk.Frame(wrap, bg=PANEL, padx=16, pady=14)
        left.grid(row=1, column=0, sticky="nsew", padx=(14, 7), pady=(0, 12))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(2, weight=1)      # 文案吸收多余高度，按钮始终贴底

        _mk_label(left, "软件作者", MUTED, SMALL_FONT).grid(
            row=0, column=0, sticky="ew")
        tk.Label(left, text=AUTHOR_NAME, bg=PANEL, fg=TEXT, font=NAME_FONT,
                 anchor="w").grid(row=1, column=0, sticky="ew", pady=(2, 12))

        # 引导文案：沿用固定换行宽度（wraplength）的做法 —— 让 Label 跟随容器
        # 宽度会与 grid 列宽互相追赶，实测会把右栏顶出窗口。
        intro = tk.Label(
            left,
            text=(
                "这个助手会持续更新与完善，作者会在这里发布新版本与说明。\n\n"
                "想交流用法、反馈问题，欢迎扫码加入使用交流群：\n"
                "· 与其它使用者交流使用技巧与经验\n"
                "· 反馈问题、提出功能建议\n"
                "· 第一时间获取新版本与更新说明\n\n"
                "如果它帮到了你，也欢迎关注作者的公众号。"
            ),
            bg=PANEL, fg=MUTED, font=SMALL_FONT, anchor="nw", justify="left",
            wraplength=220,
        )
        intro.grid(row=2, column=0, sticky="new")
        _bind_wraplength(intro, left, pad=34)

        # 两个入口按钮等宽一行；资源缺失的那个不进按钮行，
        # 避免留一个「点了没反应」的按钮。
        specs = []
        if qr_available(GROUP_QR_ASSET):
            specs.append(("加入交流群",
                          lambda: self._open_qr(GROUP_QR_ASSET), "primary"))
        if qr_available(AUTHOR_QR_ASSET):
            specs.append(("作者公众号",
                          lambda: self._open_qr(AUTHOR_QR_ASSET), "secondary"))
        if specs:
            _mk_btn_row(left, specs, row=3, pady=(14, 0))

        # ---- 右栏：两张二维码卡片（等宽并排）----
        right = tk.Frame(wrap, bg=BG)
        right.grid(row=1, column=1, sticky="nsew", padx=(7, 14), pady=(0, 12))
        right.columnconfigure(0, weight=1, uniform="qrcard")
        right.columnconfigure(1, weight=1, uniform="qrcard")
        right.rowconfigure(0, weight=1)

        self._make_qr_card(
            right, 0, GROUP_QR_ASSET, "使用交流群",
            "微信扫码加入，交流用法、反馈问题。群二维码有时效（约 7 天），"
            "过期请找作者重新获取。")
        self._make_qr_card(
            right, 1, AUTHOR_QR_ASSET, "作者微信公众号",
            "微信扫码关注，获取版本更新、使用帮助与更多实用小工具。")

    def _make_qr_card(self, parent, column, asset, title, desc):
        """构建一张二维码卡片：标题 + 说明 + 白底二维码 + 「点击放大」按钮。

        资源缺失时卡片照样占位，只是白底区域直接写明缺了哪个文件 ——
        打包漏带图片时用户能看懂发生了什么，而不是对着一块空白发呆。
        """
        card = tk.Frame(parent, bg=PANEL, padx=14, pady=12)
        card.grid(row=0, column=column, sticky="nsew",
                  padx=(0, 7) if column == 0 else (7, 0))
        card.columnconfigure(0, weight=1)
        card.rowconfigure(2, weight=1)      # 白底区吸收多余高度

        tk.Label(card, text=title, bg=PANEL, fg=TEXT, font=NAME_FONT,
                 anchor="w").grid(row=0, column=0, sticky="ew")
        desc_lbl = tk.Label(card, text=desc, bg=PANEL, fg=MUTED,
                            font=SMALL_FONT, anchor="w", justify="left",
                            wraplength=200)
        desc_lbl.grid(row=1, column=0, sticky="ew", pady=(2, 8))
        _bind_wraplength(desc_lbl, card, pad=30)

        # 白底 holder：二维码需要足够对比度，不能直接压在深色面板上
        holder = tk.Frame(card, bg="#ffffff", padx=6, pady=6)
        holder.grid(row=2, column=0, sticky="nsew")
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)

        # 初始留空：资源是否缺失由 _render_qr 判定后才写提示文案 ——
        # 否则首帧会先闪一下「未找到」，看起来像真的缺图。
        label = tk.Label(holder, bg="#ffffff", bd=0, highlightthickness=0,
                         cursor="hand2", text="", fg=MUTED,
                         font=SMALL_FONT, justify="center")
        label.grid(row=0, column=0, sticky="nsew")
        label.bind("<Button-1>", lambda _e, a=asset: self._open_qr(a))

        _mk_btn(card, "点击放大", lambda: self._open_qr(asset),
                "secondary").grid(row=3, column=0, sticky="ew", pady=(10, 0))

        self._qr_states[asset] = {
            "state": {"box": (0, 0), "photo": None, "job": None},
            "label": label,
        }
        label.bind("<Configure>",
                   lambda _e, a=asset: self._queue_qr_render(a))

    def _render_all_qr(self):
        """渲染本页所有二维码卡片（页签显示后调用）。"""
        self._author_job = None              # 本回调已被触发，任务 id 作废
        if self._closing:
            return
        for asset in list(getattr(self, "_qr_states", {}).keys()):
            self._render_qr(asset)

    def _queue_qr_render(self, asset, _e=None):
        """容器尺寸变化时防抖重绘该二维码（拖动窗口时不必每帧重算缩放）。"""
        if self._closing:                    # 关窗中：destroy 会连发 Configure，别再建任务
            return
        entry = getattr(self, "_qr_states", {}).get(asset)
        if entry is None:
            return
        st = entry["state"]
        if st["job"] is not None:
            try:
                self.root.after_cancel(st["job"])
            except Exception:
                pass
            st["job"] = None
        try:
            st["job"] = self.root.after(90, lambda: self._render_qr(asset))
        except Exception:
            st["job"] = None

    def _render_qr(self, asset):
        """按当前容器尺寸重绘指定二维码；资源缺失时给出可操作的提示而非留白。"""
        if self._closing:
            return
        entry = getattr(self, "_qr_states", {}).get(asset)
        if entry is None:
            return
        st, label = entry["state"], entry["label"]
        st["job"] = None

        pil = _load_qr_pil(asset)
        if pil is None:
            st["photo"] = None
            st["box"] = (0, 0)
            try:
                label.configure(
                    image="",
                    text=f"未找到二维码图片\n（{asset}）\n\n把它放到程序目录后"
                         "重新打开本页即可显示",
                    fg=MUTED, font=SMALL_FONT, justify="center")
            except Exception:
                pass
            return

        w, h = label.winfo_width(), label.winfo_height()
        side = int(min(w, h, MAX_QR_SIDE))
        if side < 60:            # 容器尚未完成布局，等下一次 Configure 再画
            return
        if (side, side) == st["box"]:
            return
        st["box"] = (side, side)
        try:
            # master 必须传将要显示它的控件：PhotoImage 会注册到该控件所属的
            # Tcl 解释器，否则图片落到托盘那个解释器上、这里显示为空白（踩坑）。
            photo = _fit_photo(pil, side, side, master=label)
            if photo is not None:
                label.configure(image=photo, text="")
                st["photo"] = photo      # 必须持引用，否则被回收成空白
        except Exception:
            pass

    # ================= 校验与反馈 =================

    def _clear_field_errors(self):
        for w in (self.p_name, self.p_content, self.m_name, self.m_base,
                  self.m_model, self.m_key):
            try:
                w.configure(highlightbackground=BORDER)
            except Exception:
                pass

    def _mark_error(self, widget):
        try:
            widget.configure(highlightbackground=ERR_BORDER)
        except Exception:
            pass

    def _validate(self, checks):
        """checks: [(控件, 值, 错误文案)]；任一为空即标红并提示，返回 False。"""
        self._clear_field_errors()
        for widget, value, msg in checks:
            if not (value or "").strip():
                self._mark_error(widget)
                self._flash(msg, "err")
                try:
                    widget.focus_set()
                except Exception:
                    pass
                return False
        return True

    def _confirm(self, title, message):
        """删除等不可逆操作才弹确认框；父窗口绑定本页 root。"""
        try:
            from tkinter import messagebox
            return messagebox.askyesno(title, message, parent=self.root)
        except Exception:
            return True

    def _flash(self, msg, kind="ok"):
        colors = {"ok": OK, "warn": WARN, "err": BAD, "info": MUTED}
        try:
            self.status.configure(text=msg, fg=colors.get(kind, MUTED))
        except Exception:
            return
        if self._flash_job:
            try:
                self.root.after_cancel(self._flash_job)
            except Exception:
                pass
        self._flash_job = self.root.after(6000, self._show_default_status)

    def _show_default_status(self):
        self._flash_job = None
        try:
            pend = config.get_active_prompt()
            pname = pend.get("name", "—") if pend else "—"
            am = config.get_active_model()
            mname = am.get("name", "（未配置）") if am else "（未配置）"
            self.status.configure(
                text=f"当前生效提示词：{pname}　｜　当前生效模型：{mname}"
                     f"　｜　Esc 关闭",
                fg=MUTED)
        except Exception:
            pass


# ================= 二维码资源（使用交流群 / 作者公众号）=================

AUTHOR_QR_ASSET = "author_qrcode.png"    # 作者微信公众号
# 使用交流群：微信生成的群二维码自带有效期（约 7 天）。过期后只需用同名图片
# 替换 group_qrcode.png（放程序目录的会优先于打包内置的那张），界面无需改动 ——
# 文案里已提示用户「过期请找作者重新获取」。
GROUP_QR_ASSET = "group_qrcode.png"
AUTHOR_NAME = "地铁里的 AI 探员"          # 作者微信公众号名称（展示用）
MAX_QR_SIDE = 300                        # 页内二维码最大边长（两张并排，比单张时略小）
_QR_CACHE = {}                           # asset -> PIL.Image（只缓存加载成功的）


def qr_available(asset=AUTHOR_QR_ASSET):
    """二维码资源是否存在。缺失时不显示对应入口，避免点了没反应。"""
    try:
        return paths.asset(asset).exists()
    except Exception:
        return False


def _load_qr_pil(asset=AUTHOR_QR_ASSET):
    """加载指定二维码原图（PIL）。结果按资源名缓存，避免每次弹窗都读盘。"""
    if asset in _QR_CACHE:
        return _QR_CACHE[asset]
    try:
        from PIL import Image
        p = paths.asset(asset)
        if not p.exists():
            return None
        _QR_CACHE[asset] = Image.open(p).convert("RGB")
        return _QR_CACHE[asset]
    except Exception:
        return None


def _fit_photo(pil, box_w, box_h, master=None):
    """把原图按比例缩放进 box_w×box_h，返回 ImageTk.PhotoImage。

    用 LANCZOS 重采样保证放大后边缘干净（二维码是硬边线条图，
    低质量插值会产生灰边，直接影响识别率）。

    master 必须传「将要显示这张图的控件」（tk.Label 等）。原因（踩坑）：
      PhotoImage 不指定 master 时会注册到 tkinter._default_root 上，
      而本程序的 _default_root 是**主线程的托盘 Tk**，设置页却是子线程里
      另建的 Tk。图片被注册进托盘那个 Tcl 解释器后，设置页的 Label 找不到
      它 —— 现象是「弹窗正常显示、图片区域一片空白」。
      同理，本模块所有 tk 变量都显式绑 root，不能依赖默认 root。
    """
    from PIL import Image, ImageTk
    iw, ih = pil.size
    if iw <= 0 or ih <= 0 or box_w <= 0 or box_h <= 0:
        return None
    scale = min(box_w / float(iw), box_h / float(ih))
    nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
    img = pil if (nw, nh) == (iw, ih) else pil.resize((nw, nh), Image.LANCZOS)
    if master is None:
        return ImageTk.PhotoImage(img)
    return ImageTk.PhotoImage(img, master=master)


# 弹窗标题 / 副标题（按资源名区分；未登记的资源走默认文案）
QR_DIALOG_META = {
    GROUP_QR_ASSET: ("使用交流群", "微信扫码加入交流群"),
    AUTHOR_QR_ASSET: ("作者微信公众号", "微信扫一扫关注"),
}


def _show_qr_dialog(parent=None, asset=AUTHOR_QR_ASSET):
    """弹出可缩放的二维码大图窗口（点击底栏入口 / 卡片 / 按钮触发）。

    尺寸适配不同屏幕与不同比例的原图：
      - 长边按屏幕宽高推算并夹在 [280, 460] 之间 ——
        大屏不至于铺满，小屏也能保证二维码足够大、能扫；
      - 竖版长图（如作者公众号卡片）按高度换算，不裁切、不拉伸；
      - 拖动窗口边缘时按可用区域重新渲染（保持宽高比），
        所以放大后依旧清晰，而不是把位图拉伸变模糊。
    """
    pil = _load_qr_pil(asset)
    if pil is None:
        return None

    title, subtitle = QR_DIALOG_META.get(asset, ("二维码", "微信扫一扫"))

    top = tk.Toplevel(parent) if parent is not None else tk.Tk()
    top.title(title)
    top.configure(bg=BG)
    top.attributes("-topmost", True)
    try:
        top.iconify()               # 抑制弹出瞬间的闪烁
        top.deiconify()
    except Exception:
        pass

    header = tk.Frame(top, bg=PANEL)
    header.pack(fill="x")
    tk.Frame(header, bg=ACCENT, width=4).pack(side="left", fill="y")
    tk.Label(header, text=title, bg=PANEL, fg=TEXT,
             font=TITLE_FONT, padx=12, pady=10).pack(side="left")
    tk.Label(header, text=subtitle, bg=PANEL, fg=MUTED,
             font=SMALL_FONT, padx=12).pack(side="right")

    img_lbl = tk.Label(top, bg=BG, bd=0, highlightthickness=0)
    img_lbl.pack(fill="both", expand=True, padx=16, pady=(14, 8))

    # 图片本身已带说明（如作者公众号卡片），这里不再重复，只留操作提示
    tk.Label(top, text="拖动窗口边缘可放大，二维码会重新渲染保持清晰",
             bg=BG, fg=MUTED, font=SMALL_FONT).pack(pady=(0, 10))
    _mk_btn(top, "关闭", top.destroy, "secondary", width=10).pack(pady=(0, 14))

    state = {"box": (0, 0), "photo": None, "job": None, "dead": False}

    def render():
        state["job"] = None
        if state["dead"]:
            return
        aw = max(80, img_lbl.winfo_width() - 4)
        ah = max(80, img_lbl.winfo_height() - 4)
        if (aw, ah) == state["box"]:
            return
        state["box"] = (aw, ah)
        try:
            photo = _fit_photo(pil, aw, ah, master=img_lbl)
            if photo is not None:
                img_lbl.configure(image=photo)
                state["photo"] = photo      # 必须持引用，否则被回收成空白
        except Exception:
            pass

    def _queue_render(_e=None):
        if state["dead"]:
            return
        if state["job"] is not None:
            try:
                top.after_cancel(state["job"])
            except Exception:
                pass
        # 防抖：拖动窗口时不必每帧都重算缩放
        state["job"] = top.after(90, render)

    sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
    long_side = int(max(280, min(sw * 0.34, sh * 0.56, 460)))
    iw, ih = pil.size
    if iw >= ih:                    # 方图 / 横图：以宽度为准
        img_w = long_side
        img_h = max(1, int(long_side * ih / float(iw)))
    else:                           # 竖图（如作者公众号卡片）：以高度为准
        img_h = min(long_side, int(sh * 0.62))
        img_w = max(1, int(img_h * iw / float(ih)))
    top.geometry(f"{img_w + 44}x{img_h + 132}")
    top.minsize(240, 300)
    try:
        top.update_idletasks()
        top.geometry(f"+{max(0, (sw - top.winfo_width()) // 2)}"
                     f"+{max(0, (sh - top.winfo_height()) // 3)}")
    except Exception:
        pass

    def _on_destroy(event=None):
        """关窗时撤掉未触发的重绘（避免 `invalid command name ... "after" script`）。

        与设置页同理：destroy 过程中控件会连发 <Configure> 补建新任务，
        因此先竖 dead 标志、再撤销已有任务。子控件销毁也会走到这里，需过滤。
        """
        if event is not None and str(event.widget) != str(top):
            return
        state["dead"] = True
        if state["job"] is not None:
            try:
                top.after_cancel(state["job"])
            except Exception:
                pass
            state["job"] = None

    top.bind("<Destroy>", _on_destroy)
    img_lbl.bind("<Configure>", _queue_render)
    # 初始渲染的 id 也登记进 state，关窗时才能一并撤销
    state["job"] = top.after(30, render)
    top.bind("<Escape>", lambda _e: top.destroy())
    try:
        top.focus_set()
    except Exception:
        pass
    return top


class _QrHoverPreview:
    """悬停入口时在旁侧浮出的小尺寸二维码预览。

    延迟 DELAY_MS 才出现（类似 tooltip），避免鼠标扫过底栏就一闪一闪；
    鼠标移开立即销毁。浮层用 overrideredirect 去掉边框，视觉上是「贴」在
    按钮上方的一张卡片。
    """

    DELAY_MS = 380
    SIZE = 176

    def __init__(self, anchor, pil):
        self.anchor = anchor
        self.pil = pil
        self.top = None
        self.photo = None
        self.job = None

    def schedule(self, _e=None):
        self._cancel_job()
        try:
            self.job = self.anchor.after(self.DELAY_MS, self._show)
        except Exception:
            pass

    def _cancel_job(self):
        if self.job is not None:
            try:
                self.anchor.after_cancel(self.job)
            except Exception:
                pass
            self.job = None

    def _position(self, top):
        """把浮层摆到入口正上方；上方放不下就改到下方，并夹在屏幕内。

        入口尚未映射时（winfo_rootx/y 为 0）不定位 —— 否则会把浮层甩到
        屏幕左上角。这种情况一般不会发生，但多一层保护更稳妥。
        """
        ax, ay = self.anchor.winfo_rootx(), self.anchor.winfo_rooty()
        aw, ah = self.anchor.winfo_width(), self.anchor.winfo_height()
        if (ax, ay) == (0, 0) and aw <= 1:
            return False
        top.update_idletasks()
        w, h = top.winfo_width(), top.winfo_height()
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        x = min(max(0, ax + aw // 2 - w // 2), max(0, sw - w))
        y = ay - h - 8
        if y < 0:
            y = ay + ah + 8
        top.geometry(f"+{x}+{max(0, min(y, max(0, sh - h)))}")
        return True

    def _show(self):
        self.job = None
        if self.top is not None:
            return
        try:
            top = tk.Toplevel(self.anchor)
            top.overrideredirect(True)          # 无边框浮层
            top.attributes("-topmost", True)
            top.configure(bg=BORDER, padx=1, pady=1)
            lbl = tk.Label(top, bg="#ffffff", bd=0)
            lbl.pack()
            # master 传 lbl：图片必须注册进本页所在的解释器（详见 _fit_photo）
            photo = _fit_photo(self.pil, self.SIZE, self.SIZE, master=lbl)
            if photo is not None:
                lbl.configure(image=photo)
                self.photo = photo              # 持引用防回收

            if not self._position(top):
                top.destroy()                   # 入口尚未定位好，本次不显示
                return
            self.top = top
        except Exception:
            self.top = None

    def hide(self, _e=None):
        self._cancel_job()
        if self.top is not None:
            try:
                self.top.destroy()
            except Exception:
                pass
            self.top = None
            self.photo = None


def open_editor(tab="author"):
    """打开设置页（由 main.py 在独立线程中调用）。"""
    config.ensure_prompts_seeded()
    config.ensure_models_seeded()

    root = tk.Tk()
    root.title("提示词工作室")
    root.configure(bg=BG)

    # 自适应：按屏幕尺寸给一个合适的初始大小，小屏也不会超出边界
    try:
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    except Exception:
        sw, sh = 1366, 768
    w = min(1000, max(760, int(sw * 0.66)))
    h = min(780, max(600, int(sh * 0.82)))
    x = max(0, (sw - w) // 2)
    y = max(0, (sh - h) // 2)
    root.geometry(f"{w}x{h}+{x}+{y}")
    root.minsize(720, 560)          # 再小两栏就挤不开，用最小尺寸兜底
    root.attributes("-topmost", True)
    try:
        root.iconify()
        root.deiconify()
    except Exception:
        pass

    _Editor(root, tab=tab)
    root.mainloop()
