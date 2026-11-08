"""智能伙伴（皮肤系统）：皮肤注册表与资源解析。

与 UI 完全解耦，只负责"配置 → 素材路径"的解析，便于单测。

设计要点：
- 两套皮肤：robot（机器人）、penguin（企鹅），各含待命(idle)、工作(working)动画；
  企鹅待命是双动作顺序轮播（举星星徽章 idle_a + 水晶球 idle_b），
  企鹅另有右键舞蹈彩蛋（dance），机器人没有。
- 旧配置兼容：用户若把 idle_gif/active_gif 改成非默认值，视为"自定义皮肤覆盖"，
  优先级高于内置皮肤，保证老用户行为不破坏。
- 资源定位统一走 paths.companion_asset：程序同目录 companions/ 优先（免打包换肤），
  找不到再用打包进 exe 的内部资源。
"""
from dataclasses import dataclass
from typing import List, Optional

import paths

COMPANIONS = {
    "robot": {
        "name": "机器人",
        "idle": ["robot/idle.webp"],
        "working": "robot/working.webp",
        "dance": None,
    },
    "penguin": {
        "name": "企鹅",
        "idle": ["penguin/idle_a.webp", "penguin/idle_b.webp"],
        "working": "penguin/working.webp",
        "dance": "penguin/dance.webp",
    },
}

DEFAULT_CHARACTER = "robot"

# 旧版浮动动图的默认文件名：与默认值不同的取值才视为用户自定义覆盖
_LEGACY_DEFAULTS = ("1.webp", "2.webp")


@dataclass
class CompanionSpec:
    """一次解析得到的皮肤素材集合（全部是绝对路径字符串）。"""
    character: str            # 皮肤 id（"robot"/"penguin"/"custom"）
    name: str                 # 显示名
    idle: List[str]           # 待命动画，按列表顺序轮播（>=1 段）
    working: str              # 工作动画
    dance: Optional[str]      # 右键彩蛋动画；None 表示该皮肤无彩蛋或已关闭
    dance_enabled: bool
    custom: bool = False      # 是否来自旧配置 idle_gif/active_gif 自定义覆盖


def available_companions():
    """设置页用的皮肤清单。"""
    return [
        {"id": cid, "name": c["name"], "has_dance": bool(c.get("dance"))}
        for cid, c in COMPANIONS.items()
    ]


def _abs(rel: str) -> str:
    return str(paths.companion_asset(rel))


def _legacy_override(cfg) -> Optional[CompanionSpec]:
    """用户改过旧键 idle_gif/active_gif（非默认）→ 返回自定义皮肤。"""
    idle_gif = cfg.get("idle_gif")
    active_gif = cfg.get("active_gif")
    if not idle_gif or not active_gif:
        return None
    if str(idle_gif) == _LEGACY_DEFAULTS[0] and str(active_gif) == _LEGACY_DEFAULTS[1]:
        return None
    return CompanionSpec(
        character="custom",
        name="自定义",
        idle=[str(paths.asset(str(idle_gif)))],
        working=str(paths.asset(str(active_gif))),
        dance=None,
        dance_enabled=False,
        custom=True,
    )


def resolve(cfg) -> CompanionSpec:
    """根据配置解析当前皮肤素材。任何输入都保证返回可用结果。"""
    comp = cfg.get("companion")
    enabled = True
    character = DEFAULT_CHARACTER
    if isinstance(comp, dict):
        enabled = bool(comp.get("dance_enabled", comp.get("dance_on_hover", True)))
        character = str(comp.get("character", DEFAULT_CHARACTER))

    legacy = _legacy_override(cfg)
    if legacy is not None:
        legacy.dance_enabled = enabled
        return legacy

    skin = COMPANIONS.get(character) or COMPANIONS[DEFAULT_CHARACTER]
    idle = [_abs(p) for p in skin["idle"]]
    working = _abs(skin["working"])
    dance_rel = skin.get("dance")
    dance = _abs(dance_rel) if (dance_rel and enabled) else None
    return CompanionSpec(
        character=character if character in COMPANIONS else DEFAULT_CHARACTER,
        name=skin["name"],
        idle=idle,
        working=working,
        dance=dance,
        dance_enabled=enabled,
    )
