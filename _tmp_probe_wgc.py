"""临时脚本：检查 windows-capture 的依赖与 API（解包试用，不污染环境）。用完即删。"""
import glob
import sys
import zipfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

whl = glob.glob("_tmp_pkg/*.whl")[0]
z = zipfile.ZipFile(whl)
out = "_tmp_pkg/x"
z.extractall(out)

print("=== __init__.py 是否 import cv2/opencv ===")
src = z.read("windows_capture/__init__.py").decode("utf-8")
print(src[:1200])
print("... cv2 出现:", "cv2" in src, "| opencv 出现:", "opencv" in src)
print("=== METADATA 依赖 ===")
meta = z.read("windows_capture-2.0.1.dist-info/METADATA").decode("utf-8")
for line in meta.splitlines():
    if line.startswith(("Requires", "Summary", "Version", "Requires-Python")):
        print(" ", line)

sys.path.insert(0, out)
try:
    import windows_capture as wc
    print("=== 导入成功 ===")
    print("导出:", [n for n in dir(wc) if not n.startswith("_")])
    print("WindowsCapture.__doc__:")
    print((wc.WindowsCapture.__doc__ or "(无)")[:2000])
except Exception as e:
    print("=== 导入失败 ===", type(e).__name__, e)
