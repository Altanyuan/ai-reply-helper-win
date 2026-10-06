"""首次运行引导：引导用户填写**自己的** DeepSeek API Key。

为什么需要：
  分发给朋友的程序里不含任何 API Key（作者的 Key 不进包，避免泄露与被刷费用）。
  因此首次启动若检测不到 api_key，就弹出本窗口引导填写，否则程序无法工作。

设计要点：
  - 提供 DeepSeek 申请指引与免费额度说明，降低朋友的上手成本。
  - 「测试连接」用最小请求（max_tokens=1）验证 Key，几乎不产生费用。
  - 网络请求放在子线程，避免卡住 tkinter 界面。
  - 配色沿用 config_editor.py 的 CodeBuddy 深色主题，视觉风格统一。
"""
import threading
import tkinter as tk

import config
from ai_client import test_api_key

# ---- 配色（与 config_editor.py 保持一致）----
BG = "#1e1e1e"
PANEL = "#252526"
BORDER = "#3c3c3c"
TEXT = "#d4d4d4"
MUTED = "#9da5b4"
ACCENT = "#0e639c"
ACCENT_HOVER = "#1177bb"
SECONDARY = "#3a3d41"
SECONDARY_HOVER = "#4a4d51"
OK = "#4ec9b0"
WARN = "#dcdcaa"
BAD = "#f48771"

LABEL_FONT = ("Microsoft YaHei", 10)
TITLE_FONT = ("Microsoft YaHei", 13, "bold")
SMALL_FONT = ("Microsoft YaHei", 9)

APPLY_URL = "https://platform.deepseek.com/api_keys"


def need_setup(cfg):
    """是否需要首次配置：没有 api_key 即需要。"""
    return not (cfg or {}).get("api_key")


class _Wizard:
    def __init__(self, root, cfg, on_done):
        self.root = root
        self.cfg = cfg
        self.on_done = on_done
        self._testing = False
        self._build()

    # ---------- 布局 ----------
    def _build(self):
        # 顶栏
        header = tk.Frame(self.root, bg=PANEL, height=46)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Frame(header, bg=ACCENT, width=4).pack(side="left", fill="y")
        tk.Label(header, text="首次使用 · 配置 API Key", bg=PANEL, fg="#ffffff",
                 font=TITLE_FONT, padx=12).pack(side="left")

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=20, pady=(16, 8))

        # 说明
        intro = (
            "首次使用必须配置 API Key（必填，否则无法启动）。\n"
            "这个助手需要调用大模型来润色话术；请填写你自己的 Key —— "
            "它只保存在你本机，不会上传给任何人。"
        )
        tk.Label(body, text=intro, bg=BG, fg=TEXT, font=LABEL_FONT,
                 anchor="w", justify="left", wraplength=600).pack(fill="x")

        # 申请指引
        guide = tk.Frame(body, bg=PANEL, highlightthickness=1,
                         highlightbackground=BORDER)
        guide.pack(fill="x", pady=(12, 14))
        tk.Label(
            guide,
            text=(
                "还没有 Key？三步搞定：\n"
                f"1. 打开 {APPLY_URL}\n"
                "2. 用手机号注册 / 登录（新用户有免费额度，日常使用基本够）\n"
                "3. 点「创建 API Key」，复制以 sk- 开头的那串字符，粘贴到下面"
            ),
            bg=PANEL, fg=MUTED, font=SMALL_FONT, anchor="w", justify="left",
            wraplength=580, padx=12, pady=10,
        ).pack(fill="x")

        # 输入框（默认打码，点「显示」才展开）
        head = tk.Frame(body, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="API Key（以 sk- 开头）", bg=BG, fg=MUTED,
                 font=LABEL_FONT, anchor="w").pack(side="left")
        self._visible = False
        self.key_toggle = tk.Label(head, text="显示", bg=BG, fg=ACCENT,
                                   font=SMALL_FONT, cursor="hand2")
        self.key_toggle.pack(side="left", padx=(8, 0))
        self.key_toggle.bind("<Button-1>", lambda e: self._toggle_visible())
        self.entry = tk.Entry(body, bg=PANEL, fg=TEXT, insertbackground="#aeafad",
                              relief="solid", borderwidth=1,
                              highlightbackground=BORDER, highlightthickness=1,
                              font=LABEL_FONT, show="•")
        self.entry.pack(fill="x", pady=(2, 4), ipady=6)
        self.entry.focus_set()
        self.entry.bind("<Return>", lambda e: self._on_save())

        # 状态提示
        self.status = tk.Label(body, text="", bg=BG, fg=MUTED, font=SMALL_FONT,
                               anchor="w", wraplength=600, justify="left")
        self.status.pack(fill="x", pady=(6, 0))

        # 按钮
        btns = tk.Frame(self.root, bg=BG)
        btns.pack(fill="x", padx=20, pady=(6, 16))
        tk.Button(btns, text="退出", command=self._on_cancel, bg=SECONDARY,
                  fg=TEXT, activebackground=SECONDARY_HOVER, font=LABEL_FONT,
                  width=10, relief="flat", pady=7, cursor="hand2"
                  ).pack(side="left")
        self.test_btn = tk.Button(btns, text="测试连接", command=self._on_test,
                                  bg=SECONDARY, fg=TEXT,
                                  activebackground=SECONDARY_HOVER,
                                  font=LABEL_FONT, width=10, relief="flat",
                                  pady=7, cursor="hand2")
        self.test_btn.pack(side="right", padx=(8, 0))
        self.save_btn = tk.Button(btns, text="保存并启动", command=self._on_save,
                                  bg=ACCENT, fg="white",
                                  activebackground=ACCENT_HOVER,
                                  activeforeground="white",
                                  font=("Microsoft YaHei", 10, "bold"),
                                  width=12, relief="flat", pady=7,
                                  cursor="hand2")
        self.save_btn.pack(side="right")

    # ---------- 交互 ----------
    def _set_status(self, msg, color=MUTED):
        self.status.configure(text=msg, fg=color)

    def _toggle_visible(self):
        self._visible = not self._visible
        try:
            self.entry.configure(show="" if self._visible else "•")
            self.key_toggle.configure(text="隐藏" if self._visible else "显示")
        except Exception:
            pass

    def _on_test(self):
        if self._testing:
            return
        key = self.entry.get().strip()
        if not key:
            self._set_status("请先粘贴 API Key 再测试。", WARN)
            return

        self._testing = True
        self.test_btn.configure(state="disabled", text="测试中…")
        self._set_status("正在连接 DeepSeek 验证 Key…", MUTED)

        cfg = dict(self.cfg)
        cfg["api_key"] = key

        def _worker():
            ok, msg = test_api_key(cfg)
            # 回到主线程更新界面（子线程不能直接碰 tkinter）
            try:
                self.root.after(0, lambda: self._on_test_done(ok, msg))
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _on_test_done(self, ok, msg):
        self._testing = False
        self.test_btn.configure(state="normal", text="测试连接")
        self._set_status(msg, OK if ok else BAD)

    def _on_save(self):
        key = self.entry.get().strip()
        if not key:
            self._set_status("请粘贴 API Key（以 sk- 开头）。", WARN)
            return
        if not key.startswith("sk-"):
            self._set_status(
                "这个 Key 看起来不太对（应以 sk- 开头），请确认复制完整。", WARN
            )
            return
        try:
            # 写入「模型配置」而不是裸 api_key：这样设置页里能直接看到并管理它。
            # save_model 也会把地址 / 模型名 / Key 同步到顶层字段，保证兼容。
            mid = config.ensure_default_model()
            ok, msg = config.save_model({
                "id": mid,
                "name": "默认模型",
                "api_base": (self.cfg.get("api_base")
                             or config.DEFAULTS["api_base"]),
                "model": (self.cfg.get("model") or config.DEFAULTS["model"]),
                "api_key": key,
                "note": "首次配置写入",
            })
            if not ok:
                self._set_status(msg, BAD)
                return
        except Exception as e:
            self._set_status(f"保存失败：{e}", BAD)
            return
        self.on_done(True)

    def _on_cancel(self):
        self.on_done(False)


def run_setup_wizard(cfg):
    """弹出引导窗口，阻塞直到用户完成或退出。

    返回 True 表示已保存 Key（可以启动主程序）；False 表示用户放弃（应退出）。
    """
    root = tk.Tk()
    root.title("ai-reply-helper - 首次配置")
    root.configure(bg=BG)
    root.geometry("660x560")
    root.minsize(600, 500)
    root.attributes("-topmost", True)
    try:
        root.iconify()
        root.deiconify()
    except Exception:
        pass

    result = {"saved": False}

    def _done(ok):
        result["saved"] = ok
        try:
            root.quit()
        except Exception:
            pass

    _Wizard(root, cfg, _done)
    root.mainloop()
    try:
        root.destroy()
    except Exception:
        pass
    return result["saved"]
