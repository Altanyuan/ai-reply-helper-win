# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（单文件 GUI 程序）。

用法：
    pyinstaller build.spec --noconfirm --clean
产出：
    dist/会聊.exe

要点：
  - console=False：无控制台黑框。注意 main.py 已对 sys.stdout is None 做判空。
  - datas 只含「只读资源」，绝不包含 config.json（里面有作者的真实 Key）。
  - 私人场景配置 config - *.json 一律不打包。
  - OCR 主引擎 RapidOCR 的模型资源与 ONNX Runtime 原生库，以及 WinRT 回退依赖，
    都有惰性导入/动态资源，故显式收集，保证单文件 exe 内可用。
"""
from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs, collect_submodules

# 打进 exe 的只读资源（相对本文件所在目录）
datas = [
    ("1.webp", "."),
    ("2.webp", "."),
    ("companions", "companions"),  # 智能伙伴皮肤资源（robot/penguin 两套动图）
    ("config.example.json", "."),
    ("author_qrcode.png", "."),   # 设置页「作者微信公众号」二维码
    ("icon.ico", "."),            # 回复窗口中的 AI 头像
]

# PyInstaller 静态分析抓不到的隐式导入
hiddenimports = [
    "pynput.keyboard._win32",   # Windows 键盘监听后端
    "pynput.mouse._win32",      # Windows 鼠标监听后端
    "PIL._webp",                # webp 动图解码（托盘动图必需）
    "uiautomation",             # UI Automation（3.x 直读 / 窗口定位）
    "ocr_reader",               # 截屏识别模块（capturer 内惰性 import）
    "capture_wgc",              # 微信 4.x 的 WGC 采集（capturer 内惰性 import）
]

# 原生库（.pyd/.dll）——RapidOCR、ONNX Runtime 与 WinRT OCR 运行时必须随包。
binaries = []
datas = list(datas)

# RapidOCR 模型、配置和 ONNX Runtime DLL；collect_all 同时收集动态子模块。
for _pkg in ("rapidocr", "onnxruntime"):
    _pkg_datas, _pkg_binaries, _pkg_hiddenimports = collect_all(_pkg)
    datas += _pkg_datas
    binaries += _pkg_binaries
    hiddenimports += _pkg_hiddenimports

# Windows 内置 OCR 的 winrt 分包：作为 RapidOCR 无法识别时的回退，收集子模块 + 原生库。
# 未安装这些包时，collect_* 只会告警并返回空列表，不会中断打包（走降级路径）。
for _pkg in (
    "winrt",
    "winrt.windows.media.ocr",
    "winrt.windows.globalization",
    "winrt.windows.graphics.imaging",
    "winrt.windows.storage.streams",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
):
    hiddenimports += collect_submodules(_pkg)
    binaries += collect_dynamic_libs(_pkg)

# 微信 4.x 的 WGC 采集：Rust 编译的 .pyd，且是惰性 import，静态分析扫不到，必须显式收。
# 注意它内部 import cv2（opencv-python 是它的硬依赖）—— 这会让打包体积增加约 40~50MB，
# 换来的是「窗口被遮挡也能截 + 不抢前台 + 消息区动态定位」。
for _pkg in ("windows_capture",):
    hiddenimports += collect_submodules(_pkg)
    binaries += collect_dynamic_libs(_pkg)

# 明确排除，避免把测试/调试依赖打进包。
#
# 2026-11-09 体积瘦身（第一档）：解析 PKG-00.toc 实测载荷 327.5 MiB → 原 exe 167 MiB；
# 按下面清单 + 过滤 cv2 的 ffmpeg dll 之后，exe 实测 167 MiB → 114.3 MiB
# （-52.8 MiB / -31.6%）。每一项依据见行内注释，且都可以单独注释掉回滚。
# 保留项已核对：uiautomation / comtypes / shapely / rapidocr(+两个 onnx 模型) /
# onnxruntime / cv2.pyd / tcl+tk 均仍在包里。
excludes = [
    "pytest",
    "tkinter.test",
    # scipy 只被 shapely/conftest.py（测试夹具）引用，项目零引用；
    # 实测走完 RapidOCR + UIA 全流程后 sys.modules 里没有 scipy。占 67 MiB。
    "scipy",
    "scipy.libs",
    # lxml 来自 markitdown/bs4 那条线，本项目零引用；bs4 自身也会回退 html.parser。
    "lxml",
    # cryptography 只在 requests/help.py 打印版本号时用到（try/except 包着），
    # 排除后 requests 走 stdlib ssl，TLS 与代理照常。占 7.9 MiB（另加 OpenSSL 5.8）。
    "cryptography",
    # pywin32 只被 comtypes 的 test 文件引用；实测 UIA 初始化后 pywintypes 仍未加载。
    "pythonwin",
    "win32api",
    "win32con",
    "win32gui",
    "win32com",
    "win32com.client",
    "pywintypes",
    # 只解码 webp/png/ico，不处理 AVIF（该插件自身 7.5 MiB）。
    "PIL.AvifImagePlugin",
    # 环境里装了但本项目不用的重型库，避免被静态分析顺藤摸瓜带进来。
    "matplotlib",
    "pandas",
    "IPython",
    "bs4",
    "markitdown",
]

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

# cv2 自带的 ffmpeg 只服务于 cv2.VideoCapture / VideoWriter（视频编解码）。
# 本项目只用 cv2 做图像运算（RapidOCR 的预处理），所以这一个 dll（26.7 MiB 载荷）
# 纯属陪跑。cv2 是在真正用到 videoio 时才去加载它，import cv2 不受影响。
#
# 已验证（把 site-packages 里该 dll 改名后跑）：import cv2 成功，cvtColor /
# GaussianBlur / Sobel / floodFill / imencode 全部正常 → 属于纯运行时按需加载。
# 注意：过滤必须作用在 a.binaries 上（而不是 Analysis 的 binaries 入参），
# 否则 hook 后加的这份 dll 会漏网。
a.binaries = [b for b in a.binaries if "opencv_videoio_ffmpeg" not in b[0]]

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="会聊",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # 未安装 UPX 时也能正常打包（不压缩而已）
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,      # GUI 模式：不弹黑框
    icon="icon.ico",    # 程序图标（微信风格，由 icon.png 转换生成）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
