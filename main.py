import logging
import os
import subprocess
import atexit
import sys
import threading
import time
import queue as _queue
from pathlib import Path
import uiautomation as auto
from pynput import keyboard, mouse as pynput_mouse

import config
import first_run
import paths
import wechat_window
from capturer import capture_draft, read_wechat_recent, read_wechat_input
from ai_client import polish
from paste_back import paste_back
from ui import show_choices, show_error
from tray_widget import make_tray

tray = None  # 右下角浮动动图组件，main() 中初始化

logging.basicConfig(
    filename=str(paths.log_path()),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)
log = logging.getLogger("polish")

_lock = threading.Lock()
_config_lock = threading.Lock()  # 防止配置页重复打开

# 版本标识：每次启动写入日志，便于确认跑的是修复版（run_ui 弹框路由）
APP_VERSION = "2026-10-10-dev"

_LOCK_PATH = paths.lock_path()


def _pid_alive(pid):
    try:
        os.kill(pid, 0)            # signal 0：仅检测存活，不发送信号
        return True
    except OSError:
        return False


def _cleanup_lock():
    try:
        if _LOCK_PATH.exists():
            _LOCK_PATH.unlink()
    except Exception:
        pass


def _ensure_single_instance():
    """单实例：若已有本程序实例在跑，先终止它再启动，避免旧进程（卡 2.webp 等）残留。

    这样无论用户之前从哪个目录/副本启动过、是否留下卡死的旧进程，只要重新双击
    正确的 run.bat，都会得到「唯一一个、最新修复版」的实例。
    """
    if _LOCK_PATH.exists():
        old_pid = None
        try:
            old_pid = int(_LOCK_PATH.read_text().strip())
        except Exception:
            old_pid = None
        if old_pid and _pid_alive(old_pid):
            log.warning(f"检测到旧实例(PID={old_pid})，正在终止以启动新实例…")
            print(f"[AI润色助手] 发现旧实例(PID={old_pid})，已自动终止，启动新实例。")
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(old_pid), "/F", "/T"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=5,
                )
            except Exception as e:
                log.warning(f"终止旧实例失败: {e}")
            import time as _t
            for _ in range(10):
                if not _pid_alive(old_pid):
                    break
                _t.sleep(0.2)
    try:
        _LOCK_PATH.write_text(str(os.getpid()))
    except Exception:
        pass
    atexit.register(_cleanup_lock)


def run_ui(build_fn):
    """把弹框构建函数安全地放到【主线程】执行（避免子线程新建 Tk 卡死）。

    - 有托盘且托盘根已就绪：通过 tray.run_ui() 入队，由主循环在托盘的 Tcl
      解释器里构建 Toplevel；build_fn 立即返回、不阻塞业务线程。
    - 否则（无托盘兜底场景）：另起线程自建 Tk + mainloop。
    """
    if tray is not None and tray.get_root() is not None:
        tray.run_ui(build_fn)
    else:
        threading.Thread(target=build_fn, daemon=True).start()


def _hotkey_to_pynput(hk):
    mapping = {
        "ctrl": "<ctrl>",
        "shift": "<shift>",
        "alt": "<alt>",
        "space": "<space>",
        "cmd": "<cmd>",
    }
    # 注意：只有修饰键需要 <...> 包裹；普通字符（如 p）必须保持裸写，
    # 否则 pynput 会把 <p> 当成 Key 枚举查找，导致 KeyError / ValueError。
    return "+".join(mapping.get(p.strip().lower(), p.strip().lower())
                    for p in hk.replace("+", " ").split())


_POLL_INTERVAL = 0.4      # 轮询微信窗口是否出现的间隔（秒）
_GRAB_GRACE = 3.0         # 窗口出现后，留给实际抓取内容的宽限时间（秒）


def _grab_wechat(cfg, timeout=10.0):
    """在独立的 daemon 后台线程里等待并抓取微信上下文 + 输入框草稿。

    行为（满足“等待超过 N 秒仍拿不到才报错”的诉求）：
      • 在 timeout 秒内以 _POLL_INTERVAL 轮询微信主窗口是否出现；
        —— 用户按热键后才去打开微信的场景也能被覆盖。
      • 窗口一出现就立即抓取内容（上下文 + 草稿）并返回，不傻等到 timeout。
      • 若 timeout 秒内窗口始终没出现（或抓取异常），才返回超时标记，
        由调用方弹一次提示框。

    返回：
      {"context": str, "draft": str}            成功
      {"__timeout__": True, "window_found": b}  超时/失败（b 表示期间是否曾检测到窗口）
    超时后本函数立即返回（不阻塞触发线程），后台线程自行结束。
    """
    result_q = _queue.Queue()

    def _worker():
        window_found = False
        try:
            # 后台线程需自行初始化 COM 单元，否则 UIA 调用报
            # "尚未调用 CoInitialize"
            with auto.UIAutomationInitializerInThread():
                deadline = time.time() + timeout
                attempt = 0
                while True:
                    attempt += 1
                    # 单轮抓取异常（如微信刚启动、UIA 尚未就绪）不当作最终失败，
                    # 而是计入预算继续重试，避免过早弹框。
                    try:
                        # 兼容微信 3.x / 4.x：按「进程名 + 标题」定位，不再硬编码类名
                        # （4.x 用 Qt 重写，类名形如 Qt51514QWindowIcon 且随 Qt 版本变化）
                        win = wechat_window.find_wechat_control()
                        if win is not None:
                            window_found = True
                            hwnd = None
                            try:
                                hwnd = int(win.NativeWindowHandle)
                            except Exception:
                                pass
                            ctx = read_wechat_recent(
                                int(cfg.get("context_messages", 5)),
                                use_ocr=bool(cfg.get("ocr_context", True)),
                                hwnd=hwnd,
                                backend=cfg.get("capture_4x", "wgc"),
                            )
                            d = read_wechat_input()
                            if not d or not d.strip():
                                # 内部已对“无微信”做闸门，不会误发键
                                d = capture_draft()
                            result_q.put(
                                {"context": ctx or "", "draft": (d or "").strip()}
                            )
                            return
                    except Exception as e:
                        log.warning(f"第 {attempt} 次抓取微信内容失败，将重试: {e}")
                    if time.time() >= deadline:
                        break
                    time.sleep(_POLL_INTERVAL)
                result_q.put({"__timeout__": True, "window_found": window_found})
        except Exception as e:
            log.exception(f"抓取微信上下文异常: {e}")
            try:
                result_q.put({"__timeout__": True, "window_found": window_found})
            except Exception:
                pass

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    try:
        # 硬兜底：等待窗口 timeout 秒 + 抓取宽限，避免极端情况下无限等待
        box = result_q.get(timeout=timeout + _GRAB_GRACE)
    except _queue.Empty:
        # 超时：放弃等待，daemon 后台线程最终自行结束，不阻塞本线程
        log.warning(f"获取微信窗口/上下文超时（>{timeout}s），放弃本次抓取")
        return {"__timeout__": True, "window_found": False}
    return box


def _handle():
    cfg = config.load_config()
    log.info("热键触发")
    if tray:
        tray.show_working()  # 按热键 → 润润切到工作中状态
    # 记录当前前台窗口（通常是微信输入框），用于回填时还原焦点
    target = None
    try:
        target = auto.GetForegroundWindow()
    except Exception:
        pass

    # 统一为“最多等待 N 秒”：期间持续轮询微信窗口（用户可在此期间打开微信），
    # 窗口一出现就立即抓取内容；只有超过 N 秒仍拿不到，才弹一次提示框。
    # 不再做“窗口不存在就立即弹框”的瞬时校验，避免误报且给用户反应时间。
    timeout = float(cfg.get("grab_timeout", 10))
    grabbed = _grab_wechat(cfg, timeout=timeout)
    if grabbed is None or grabbed.get("__timeout__"):
        window_found = bool(grabbed and grabbed.get("window_found"))
        log.warning(
            f"获取微信聊天窗口超时（>{timeout}s，期间检测到窗口={window_found}）"
        )
        if window_found:
            msg = (
                f"已检测到微信，但在 {timeout:.0f} 秒内读取聊天内容失败。\n\n"
                "建议：\n"
                "• 确认微信已登录并打开了具体的聊天窗口；\n"
                "• 窗口未被最小化、卡死；\n"
                "• 在输入框里写好草稿后重新按热键重试。"
            )
        elif wechat_window.is_wechat_running():
            # 微信进程在，但主窗口定位失败 —— 通常是缩在托盘里，或未知改版
            log.warning("微信进程存在但主窗口定位失败（可能缩在托盘或版本改版）")
            msg = (
                f"检测到微信正在运行，但 {timeout:.0f} 秒内未能定位到微信主窗口。\n\n"
                "建议：\n"
                "• 把微信主窗口从最小化/托盘状态恢复出来，不要缩在托盘里；\n"
                "• 进入具体的聊天窗口后再重试；\n"
                "• 若仍不行，可能是当前微信版本的窗口结构有变化，\n"
                "  可查看 polish.log 里「微信窗口已定位」相关日志确认定位结果。"
            )
        else:
            msg = (
                f"等待 {timeout:.0f} 秒仍未检测到微信窗口，无法获取聊天内容。\n\n"
                "建议：\n"
                "• 先打开微信并进入要回复的聊天窗口；\n"
                "• 在输入框里写好草稿；\n"
                "• 然后重新按热键（或点击右下角图标）重试。"
            )

        def _e2():
            show_error(
                msg,
                master=tray.get_root() if tray else None,
                on_close=lambda: tray.show_idle() if tray else None,
            )
        if tray:
            tray.show_error()  # 抓取失败 → 润润疑惑
        run_ui(_e2)
        return

    context = grabbed["context"]
    draft = grabbed["draft"]   # 已 strip 的字符串

    if not draft and not context.strip():
        def _e3():
            show_error(
                "输入框为空，且没有读到聊天上下文，无法生成。\n\n"
                "请在微信输入框里写好草稿（哪怕几个关键词）再按热键；\n"
                "若想结合聊天记录，请确认聊天窗口已打开且消息可见。\n\n"
                "注：微信 4.x 界面不向系统暴露控件，程序改用「截屏识别」读取\n"
                "聊天内容，需要系统安装中文 OCR 语言包；若未安装或识别失败，\n"
                "会降级为「无上下文润色」——此时把对方的话粘进输入框即可。",
                master=tray.get_root() if tray else None,
                on_close=lambda: tray.show_idle() if tray else None,
            )
        if tray:
            tray.show_error()  # 无内容可生成 → 润润疑惑
        run_ui(_e3)
        return

    mode = "根据上下文生成回复" if not draft else "润色草稿"
    log.info(f"草稿长度={len(draft)} 上下文长度={len(context)} 模式={mode}")

    try:
        versions = polish(draft, context, cfg)
    except Exception as e:
        log.exception(f"AI 调用失败: {e}")
        raw = str(e)
        low = raw.lower()
        if "401" in raw or "invalid" in low or "authentication" in low:
            hint = ("大模型返回 401：API Key 无效。\n"
                    "请到 platform.deepseek.com 的【API keys】重新生成 Key，\n"
                    "然后按鼠标中键打开「设置 → 模型与 Key」替换并保存；\n"
                    "也可用环境变量 AI_API_KEY 临时覆盖。")
        elif any(k in low for k in ("ssl", "eof", "certificate", "connection",
                                    "max retries", "proxy", "getaddrinfo",
                                    "timed out", "timeout", "unreachable")):
            # 这类错误在「开着代理 / 走公司网络 / 杀软做 HTTPS 扫描」的机器上很常见，
            # 原始异常是一长串英文堆栈，用户看不出所以然 —— 这里翻译成能照着做的步骤。
            hint = ("网络连接失败：没能连上大模型接口（连接被中断 / TLS 握手失败）。\n\n"
                    "排查建议（按顺序试）：\n"
                    "• 关掉代理、VPN、加速器再试；若必须用代理，把 api.deepseek.com 设为直连；\n"
                    "• 公司/校园网络常拦这类请求，换成手机热点试一下；\n"
                    "• 杀毒软件的「HTTPS 扫描 / 网页防护」也会插一脚，临时关掉再试；\n"
                    "• 确认系统时间准确（时间偏差过大会导致 TLS 握手失败）。\n\n"
                    f"原始错误：{raw.splitlines()[0][:200]}")
        else:
            hint = raw.splitlines()[0][:300] if raw else "未知错误"
        def _e4():
            show_error(
                f"AI 润色失败：\n{hint}",
                master=tray.get_root() if tray else None,
                on_close=lambda: tray.show_idle() if tray else None,
            )
        if tray:
            tray.show_error()  # AI 调用失败 → 润润疑惑
        run_ui(_e4)
        return

    if not versions:
        log.warning("AI 未返回任何版本")
        def _e5():
            show_error(
                "AI 未返回任何版本，请检查模型配置或网络。",
                master=tray.get_root() if tray else None,
                on_close=lambda: tray.show_idle() if tray else None,
            )
        if tray:
            tray.show_error()  # AI 未返回版本 → 润润疑惑
        run_ui(_e5)
        return

    def choose(text):
        # 回填放到独立线程并初始化 COM：paste_back 内部用 uiautomation 定位输入框，
        # 在主线程直接调用会报 CoInitialize；另起线程 + UIAutomationInitializerInThread 最稳。
        def _paste():
            try:
                with auto.UIAutomationInitializerInThread():
                    paste_back(text, target_hwnd=target)
            except Exception as e:
                log.exception(f"回填失败: {e}")
        threading.Thread(target=_paste, daemon=True).start()

    def _e6():
        show_choices(
            versions, choose,
            master=tray.get_root() if tray else None,
            on_close=lambda: tray.show_idle() if tray else None,
        )
    if tray:
        tray.show_done()  # 结果窗弹出 → 润润开心
    run_ui(_e6)
    return


def trigger():
    """点击右下角动图图标时触发：与热键等价，在独立守护线程跑同一套 _handle 流程。"""
    t = threading.Thread(target=on_activate, daemon=True)
    t.start()


def open_config_editor():
    """打开设置页（默认停在「软件作者」页，点击鼠标中键触发）。
    另起线程跑 tkinter，避免阻塞监听。

    用非阻塞锁防止重复打开——中键按下会触发一次 on_click(pressed=True)，
    若配置页已开则直接忽略，避免堆叠多个窗口。
    """
    if not _config_lock.acquire(blocking=False):
        return

    def _runner():
        try:
            import config_editor
            config_editor.open_editor()
        finally:
            _config_lock.release()

    t = threading.Thread(target=_runner, daemon=True)
    t.start()


def on_activate():
    if not _lock.acquire(blocking=False):
        log.info("已有处理在进行，忽略本次触发")
        return
    try:
        # 关键修复：pynput 的热键回调运行在子线程，而 uiautomation 需要在该线程
        # 初始化 COM 单元（CoInitialize），否则报
        # "尚未调用 CoInitialize / Can not load UIAutomationCore.dll"。
        # UIAutomationInitializerInThread 进入时初始化、退出时反初始化。
        with auto.UIAutomationInitializerInThread():
            _handle()
    except Exception as e:
        log.exception(f"处理失败: {e}")
    finally:
        _lock.release()


def main():
    global tray
    _ensure_single_instance()      # 先接管旧实例，保证唯一且为最新修复版
    cfg = config.load_config()

    # 控制台实时日志：在 run.bat 窗口直接看到提示词 / 响应（config.json 的 console_log）
    # 注意：--noconsole 打包时 sys.stdout 为 None，必须判空，否则挂载 handler 会崩溃
    if cfg.get("console_log", True) and sys.stdout is not None:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        _console = logging.StreamHandler(sys.stdout)
        _console.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        logging.getLogger().addHandler(_console)

    # 首次运行引导：分发版不含任何 Key，必须由使用者填写自己的
    # （放在创建托盘之前，此时主线程还没有 Tk 根，不会与之冲突）
    if first_run.need_setup(cfg):
        log.info("未检测到 API Key，弹出首次配置引导")
        if not first_run.run_setup_wizard(cfg):
            log.info("用户未完成 API Key 配置，程序退出")
            return
        cfg = config.load_config()
        log.info("首次配置完成，已保存 API Key")

    combo = _hotkey_to_pynput(cfg.get("hotkey", "ctrl+alt+p"))

    # 启动时记录一次 OCR 可用性：微信 4.x 读聊天上下文依赖它，
    # 写进日志便于用户/开发者排查「读不到上下文」的原因（不影响主流程）。
    if cfg.get("ocr_context", True):
        try:
            import ocr_reader
            log.info(f"OCR 状态: {ocr_reader.ocr_status_text()}")
        except Exception as e:
            log.warning(f"OCR 状态探测失败（不影响主流程）: {e}")

    # 右下角浮动动图：润润四状态（idle=待机 / working=按热键后 /
    # done=结果窗弹出 / error=失败），done、error 会自动回 idle
    # 图标可用鼠标左键拖动；松手后位置写入 config.json 的 tray_pos，下次启动沿用。
    def _save_tray_pos(x, y):
        try:
            config.update_config({"tray_pos": [int(x), int(y)]})
        except Exception as e:
            log.warning(f"写入托盘动图位置失败（不影响本次使用）: {e}")

    tray = make_tray(
        {
            "idle": str(paths.asset(cfg.get("tray_idle_gif", "runrun-idle.webp"))),
            "working": str(paths.asset(cfg.get("tray_working_gif", "runrun-working.webp"))),
            "done": str(paths.asset(cfg.get("tray_done_gif", "runrun-done.webp"))),
            "error": str(paths.asset(cfg.get("tray_error_gif", "runrun-error.webp"))),
        },
        size=int(cfg.get("tray_size", 220)),
        pos=cfg.get("tray_pos"),
        on_move=_save_tray_pos,
    )
    if tray:
        # 点击右下角动图 = 与按热键等价的另一种触发方式
        tray.set_command(trigger)

    log.info(f"=== 启动 build={APP_VERSION}（弹框修复 run_ui 已启用），热键={combo} ===")
    print(f"[AI润色助手] 已启动 build={APP_VERSION}，热键 {combo}。在微信输入框打字后按热键即可润色。")
    print(f"[AI润色助手] 点击「鼠标中键（滚轮按下）」任意位置即可打开设置页"
          f"（默认「软件作者」页，含公众号二维码）；Ctrl+C 退出。")

    hotkeys = keyboard.GlobalHotKeys({combo: on_activate})
    hotkeys.start()

    # 全局鼠标监听：中键按下 -> 打开配置页（与键盘热键互斥独立，互不阻塞）
    def _on_mouse_press(x, y, button, pressed):
        if pressed and button == pynput_mouse.Button.middle:
            log.info("鼠标中键按下 -> 打开配置页")
            open_config_editor()

    mouse_listener = pynput_mouse.Listener(on_click=_on_mouse_press)
    mouse_listener.start()

    try:
        if tray:
            tray.run()          # 主线程跑 mainloop（进程常驻）；Ctrl+C 或 stop() 退出
        else:
            import time
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        hotkeys.stop()
        mouse_listener.stop()
        if tray:
            tray.stop()
        log.info("已退出")


if __name__ == "__main__":
    main()
