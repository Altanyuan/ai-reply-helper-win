"""消息区定位回归测试（2026-11-09 「收下右侧空白带」事故）。

事故现场（另一台机器 polish.log）：
  整帧 1102x801 → 消息区 (931,108)-(1096,785) 尺寸 165x677
  → 165px 宽、贴着右边缘的空白带 → OCR 只有一个框 → 上下文为空 → 弹框。

成因：`_area_with_bg` 用「最长连续段」取左右边界，聊天内容密集时中间若干列的
底色占比掉到阈值以下，面板被切成碎片，最长的一段反而是右侧空白边；而
`_MIN_W = 100` 只是个绝对最小值，拦不住「位置错但尺寸够」。

这几个用例锁住三件事：
  · 内容密集（逐列底色占比只有 0.2）时，靠阈值逐级放宽仍能框住整块面板；
  · 只剩一条窄空白带时，必须判为定位失败，而不是拿它去 OCR；
  · 图标竖栏（与面板同色）与左侧会话列表不许被并进消息区。
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capture_wgc  # noqa: E402

PANEL = (237, 237, 237)   # 聊天面板底色（与左侧图标竖栏完全相同 —— 经典坑）
WHITE = (255, 255, 255)   # 会话列表 / 输入框底色
INK = (60, 60, 60)        # 气泡正文

W, H = 1200, 400
RAIL, SESSION = 30, 400   # 图标竖栏 0~30，会话列表 30~400，面板 400~1200


def _frame(bands=()):
    """合成一帧 4.x 窗口：竖栏 / 会话列表 / 聊天面板 / 输入框（+ 若干气泡块）。"""
    img = np.full((H, W, 3), WHITE, np.uint8)
    img[:, :RAIL] = PANEL
    img[:, RAIL:SESSION] = WHITE
    img[:, SESSION:] = PANEL
    img[:40, :] = WHITE      # 标题栏
    img[340:, :] = WHITE     # 输入框
    for x0, y0, x1, y1 in bands:
        img[y0:y1, x0:x1] = INK
    return img


def test_sparse_chat_locates_whole_panel():
    """内容稀疏：整块面板被框住，且不把图标竖栏 / 会话列表并进来。"""
    frame = _frame([(430, 120, 700, 170), (450, 210, 760, 260)])
    area = capture_wgc._area_with_bg(frame, PANEL)
    assert area is not None, "稀疏聊天区必须能定位"
    x0, _y_top, x1, _y_in, bg = area
    assert x0 >= SESSION, f"左边界不该跨进会话列表: x0={x0}"
    assert x1 == W, f"右边界应贴住面板右边: x1={x1}"
    assert x1 - x0 >= W * 0.3
    assert tuple(bg) == PANEL


def test_dense_chat_still_locates_whole_panel():
    """内容密集：逐列底色占比只有 0.2（第一档阈值 0.3 过不了），靠放宽阈值兜住。

    这就是事故里 165px 空白带的触发条件 —— 修复前会掉进右侧空白边。
    """
    bands = [(400, 100, 900, 180), (400, 200, 900, 280)]
    frame = _frame(bands)
    # 先确认这帧确实复现了「面板列占比偏低」的前提
    isbg = np.abs(frame.astype(np.int16) - np.array(PANEL)).sum(-1) <= capture_wgc._BG_TOL
    col = isbg[H // 4: H * 3 // 4].mean(0)
    assert 0.08 < col[600] < 0.3, f"构造的密集帧不符合前提: col[600]={col[600]:.2f}"

    area = capture_wgc._area_with_bg(frame, PANEL)
    assert area is not None, "内容密集时不许定位失败"
    x0, _y_top, x1, _y_in, _bg = area
    assert x0 <= SESSION + 10, f"左边界应仍落在面板起点附近: x0={x0}"
    assert x1 - x0 >= W * 0.3


def test_narrow_right_strip_is_rejected():
    """只剩右侧一条 180px 空白带 → 必须判为定位失败（不许交给 OCR 白跑一趟）。"""
    frame = _frame([(400, 100, 1020, 296)])   # 中段被盖住 98% 的采样行
    assert capture_wgc._area_with_bg(frame, PANEL) is None


def test_longest_span_merges_small_gaps_only():
    """空洞合并：小洞并成一段；会话列表那种大洞不许并（否则会跨到图标竖栏）。"""
    mask = np.zeros(400, bool)
    mask[10:30] = True
    mask[33:40] = True          # 3px 空洞（内容切断）
    assert capture_wgc._longest_span(mask, gap=10) == (10, 40)

    mask2 = np.zeros(400, bool)
    mask2[0:30] = True          # 图标竖栏
    mask2[300:380] = True       # 聊天面板，中间隔着 270px 的会话列表
    assert capture_wgc._longest_span(mask2, gap=80) == (300, 380)

    assert capture_wgc._longest_span(np.zeros(50, bool), gap=8) == (0, 0)


def test_widen_area_only_for_narrow_area():
    """宽区域重试：只对「明显偏窄」的定位结果生效。"""
    narrow = (1020, 80, 1200, 340, PANEL)
    wide = capture_wgc.widen_area((H, W), narrow)
    assert wide is not None
    assert wide[0] == int(W * capture_wgc._WIDE_LEFT_RATIO)
    assert wide[2] >= 1200

    normal = (400, 80, 1200, 340, PANEL)
    assert capture_wgc.widen_area((H, W), normal) is None, "正常宽度的区域不该重试"


def test_chrome_text_filter():
    """界面标签过滤：4.x 暴露的 2~3 个节点必须被判为噪声，真实聊天内容要留下。"""
    from capturer import _is_chrome_text

    assert _is_chrome_text("", "微信") is True
    assert _is_chrome_text("微信", "微信") is True, "根窗口自身的 Name 就是标题"
    assert _is_chrome_text("Weixin", "微信") is True, "Qt 进程名"
    assert _is_chrome_text("MMUIRenderSubWindowHW", "微信") is True, "Qt 内部 Pane（带下划线）"
    assert _is_chrome_text("在吗？这周能上线吗", "微信") is False
    assert _is_chrome_text("ok", "微信") is False, "小写英文口语不算界面标签"
    assert _is_chrome_text("对方：文件序号是多少", "微信") is False


# ==================== 交接版 v2.0.4 带来的三条（同色图标栏回归）====================
# 线上 bug 回归：微信 4.x 最左边有一条图标竖栏，底色与聊天面板完全相同
# （237,237,237），中间隔着白色的会话列表。旧实现取「第一个超过阈值的列」当
# 左边界，于是 x0≈1、x1≈窗口最右，「面板」横跨整个窗口 → 按整行统计纯面板行
# 永远达不到 90% → 上下界算不出来 → chat_area 返回 None，调用方退化到按
# 1920x1030 标定的写死比例裁剪，把会话列表里的联系人名和消息预览一起裁进消息区
# （实测现象：上下文里凭空多出「网..星期四」这种会话列表文字）。


def test_longest_run_picks_longest_segment():
    assert capture_wgc._longest_run([False, True, True, False, True, True, True, False]) == (4, 7)
    assert capture_wgc._longest_run([True, True]) == (0, 2)
    assert capture_wgc._longest_run([False, False]) == (0, 0)
    assert capture_wgc._longest_run([False, True, False, True, True]) == (3, 5)


def _synth_real_frame(w=1225, h=888, rail=63, list_right=303):
    """还原真机布局：图标栏与聊天面板同色，中间会话列表是白色。"""
    frame = np.full((h, w, 3), 237, dtype=np.uint8)
    frame[:, rail:list_right] = 255          # 会话列表（白底）
    frame[100:112, 80:290] = 30              # 会话列表里的两行文字
    frame[180:192, 80:290] = 30
    frame[90:130, 380:470] = 255             # 聊天面板里的白色气泡
    frame[60:64, :] = 220                    # 标题栏下沿
    frame[660:664, :] = 220                  # 输入框上沿
    return frame


def test_chat_area_skips_same_colored_icon_rail():
    area = capture_wgc.chat_area(_synth_real_frame())
    assert area is not None, "定位失败会退化到写死比例，把会话列表裁进消息区"
    x0, y_top, x1, y_in, bg = area
    assert x0 >= 300, f"左边界 {x0} 落进了图标栏/会话列表"
    assert x1 > 1100, f"右边界 {x1} 太靠左"
    assert y_in - y_top > 200, (y_top, y_in)
    assert tuple(int(v) for v in bg) == (237, 237, 237)


def test_chat_area_returns_none_on_tiny_frame():
    assert capture_wgc.chat_area(np.full((80, 150, 3), 237, dtype=np.uint8)) is None


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
