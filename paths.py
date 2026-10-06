"""统一路径管理：区分「只读资源」与「可写数据」，兼容 PyInstaller 打包。

为什么需要这个模块：
  1. PyInstaller `--onefile` 会把程序解压到临时目录 `sys._MEIPASS` 运行，
     程序退出即删除。若把 config.json 写在 `__file__` 旁边，配置**每次都会丢失**。
  2. 安装到 Program Files 后普通用户无写权限，日志/锁文件写入会直接崩溃。

因此：
  - 只读资源（1.webp / 2.webp 等）：打包进 exe，运行时从 `sys._MEIPASS` 读。
  - 可写数据（config.json / polish.log / 单实例锁）：放到用户目录
    `%LOCALAPPDATA%\\ai-reply-helper`，一定有写权限且免管理员。

源码运行时（未打包）为保持向后兼容，可写数据仍使用项目目录。
"""
import os
import sys
from pathlib import Path

APP_NAME = "ai-reply-helper"


def is_frozen() -> bool:
    """是否已被 PyInstaller 打包（区别于源码运行）。"""
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """只读资源目录：打包后为 sys._MEIPASS，源码运行时为脚本所在目录。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """可写数据目录（自动创建）。

    打包后：%LOCALAPPDATA%\\ai-reply-helper（避开 Program Files 只读与临时目录丢失）。
    源码运行：项目目录，沿用既有的 config.json，行为不变。
    """
    if is_frozen():
        base = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
        target = Path(base) / APP_NAME
    else:
        target = Path(__file__).resolve().parent
    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return target


def config_path() -> Path:
    """配置文件路径。"""
    return data_dir() / "config.json"


def log_path() -> Path:
    """日志文件路径：优先放在程序（安装）目录，便于用户直接找到和排查。

    用户级安装到 %LOCALAPPDATA%\\Programs\\ai-reply-helper 后该目录可写，
    日志就生成在安装路径下；若程序目录只读（如被手动拷进 Program Files
    或放在只读 U 盘），自动回落到 data_dir()，避免启动崩溃。
    """
    if is_frozen():
        exe_dir = Path(sys.executable).resolve().parent
        try:
            probe = exe_dir / ".write_probe"
            probe.touch()
            probe.unlink()
            return exe_dir / "polish.log"
        except Exception:
            pass
    return data_dir() / "polish.log"


def lock_path() -> Path:
    """单实例锁文件路径。"""
    return data_dir() / ".wechat_polish.lock"


def asset(name: str) -> Path:
    """定位资源文件（如 1.webp / 2.webp）。

    优先使用「程序同目录」的外部同名文件（方便用户替换动图皮肤），
    找不到再回落到打包进 exe 的内部资源。
    """
    try:
        if is_frozen():
            external = Path(sys.executable).resolve().parent / name
        else:
            external = Path(__file__).resolve().parent / name
        if external.exists():
            return external
    except Exception:
        pass
    return resource_dir() / name
