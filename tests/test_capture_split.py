"""采集分流回归测试：**3.x 的 UIA 直读必须零改动、且永远不被 4.x 逻辑影响**。

这几个用例锁住的是「两条路径互不干扰」这件事：
  · 3.x 命中（_read_via_uia 有结果）→ 直接返回，**不许碰 4.x 的任何代码**；
  · 3.x 落空 → 才轮到 4.x 的 WGC；
  · WGC 落空 → 才轮到截屏 OCR 兜底；
  · 配置成 ocr（回退模式）→ 跳过 WGC，直接走截屏 OCR；
  · 没有微信窗口 → 一步都不走，返回空字符串。

不依赖真实微信窗口，全部用桩替换，可离线运行。
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capturer  # noqa: E402

HWND = 12345


def _run(uia, wgc=None, ocr=None, backend="wgc", use_ocr=True):
    """跑一次 read_wechat_recent，返回 (结果, 各路径的调用记录)。"""
    calls = {"uia": 0, "wgc": 0, "ocr": 0}

    def _uia(n):
        calls["uia"] += 1
        return uia

    def _wgc(hwnd, n):
        calls["wgc"] += 1
        return wgc or ""

    def _ocr(hwnd, n):
        calls["ocr"] += 1
        return ocr or ""

    with patch.object(capturer.wechat_window, "find_wechat_hwnd", return_value=HWND), \
            patch.object(capturer.wechat_window, "wake_accessibility", return_value=None), \
            patch.object(capturer, "_read_via_uia", side_effect=_uia), \
            patch.object(capturer, "_read_via_wgc", side_effect=_wgc), \
            patch.object(capturer, "_read_via_screen_ocr", side_effect=_ocr):
        return capturer.read_wechat_recent(5, use_ocr=use_ocr, hwnd=HWND,
                                           backend=backend), calls


def test_uia_hit_skips_wgc_entirely():
    """3.x 命中时：只走 UIA，4.x 的两条路径一步都不许走。"""
    text, calls = _run(uia="对方：文件序号是多少", wgc="不该被调用", ocr="不该被调用")
    assert text == "对方：文件序号是多少"
    assert calls == {"uia": 1, "wgc": 0, "ocr": 0}, calls


def test_uia_miss_then_wgc():
    """3.x 落空（4.x 场景）时：先走 WGC，命中就返回，不再走截屏 OCR。"""
    text, calls = _run(uia="", wgc="对方：在吗", ocr="不该被调用")
    assert text == "对方：在吗"
    assert calls == {"uia": 1, "wgc": 1, "ocr": 0}, calls


def test_wgc_miss_falls_back_to_screen_ocr():
    """WGC 也拿不到时：才轮到旧的截屏 OCR 兜底。"""
    text, calls = _run(uia="", wgc="", ocr="对方：兜底")
    assert text == "对方：兜底"
    assert calls == {"uia": 1, "wgc": 1, "ocr": 1}, calls


def test_backend_ocr_skips_wgc():
    """配置成 ocr（回退模式）：跳过 WGC，直接走截屏 OCR。"""
    text, calls = _run(uia="", wgc="不该被调用", ocr="对方：回退模式", backend="ocr")
    assert text == "对方：回退模式"
    assert calls == {"uia": 1, "wgc": 0, "ocr": 1}, calls


def test_use_ocr_false_disables_all_4x_paths():
    """关掉 ocr_context（use_ocr=False）：两条 4.x 路径都不走。"""
    text, calls = _run(uia="", wgc="不该被调用", ocr="不该被调用", use_ocr=False)
    assert text == ""
    assert calls == {"uia": 1, "wgc": 0, "ocr": 0}, calls


def test_no_window_returns_empty_without_touching_anything():
    """找不到微信窗口：一步都不走，直接返回空（降级为无上下文润色）。"""
    calls = {"uia": 0}

    def _uia(n):
        calls["uia"] += 1
        return "不该被调用"

    with patch.object(capturer.wechat_window, "find_wechat_hwnd", return_value=None), \
            patch.object(capturer, "_read_via_uia", side_effect=_uia):
        assert capturer.read_wechat_recent(5, hwnd=None) == ""
    assert calls["uia"] == 0, "没有窗口时不该尝试任何采集路径"


def test_wake_accessibility_still_runs_before_uia():
    """唤醒无障碍桥必须仍在 UIA 之前执行（3.x 的既有顺序不能变）。"""
    order = []
    with patch.object(capturer.wechat_window, "find_wechat_hwnd", return_value=HWND), \
            patch.object(capturer.wechat_window, "wake_accessibility",
                         side_effect=lambda *a, **k: order.append("wake")), \
            patch.object(capturer, "_read_via_uia",
                         side_effect=lambda n: order.append("uia") or "命中"):
        capturer.read_wechat_recent(5, hwnd=HWND)
    assert order == ["wake", "uia"], order


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
