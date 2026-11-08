"""智能伙伴（companions.py）单元测试。

覆盖：
- 皮肤注册表资源真实存在且为 RGBA 动画 webp；
- 默认配置解析为机器人；企鹅皮肤解析出双待命动画 + 舞蹈；
- dance_enabled 关闭时 dance 为 None；
- 旧配置 idle_gif/active_gif 自定义覆盖优先；
- config.load_config 对嵌套 companion 的默认值与按键合并。
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import companions
import config


def _repo_root():
    return Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class RegistryAssetTests(unittest.TestCase):
    """注册表里的每个资源文件都必须真实存在、是 RGBA、且有动画帧。"""

    def test_all_registry_assets_exist(self):
        from PIL import Image
        for cid, skin in companions.COMPANIONS.items():
            rels = list(skin["idle"]) + [skin["working"]]
            if skin.get("dance"):
                rels.append(skin["dance"])
            for rel in rels:
                p = _repo_root() / "companions" / rel
                self.assertTrue(p.exists(), "缺少资源: %s" % p)
                with Image.open(p) as im:
                    self.assertEqual(im.mode, "RGBA", "%s 应为 RGBA" % p)
                    self.assertGreater(getattr(im, "n_frames", 1), 1, "%s 应为动画" % p)

    def test_penguin_assets_corners_transparent(self):
        from PIL import Image
        skin = companions.COMPANIONS["penguin"]
        for rel in list(skin["idle"]) + [skin["working"], skin["dance"]]:
            p = _repo_root() / "companions" / rel
            with Image.open(p) as im:
                im.seek(0)
                px = im.convert("RGBA").load()
                w, h = im.size
                for xy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
                    self.assertEqual(px[xy][3], 0, "%s 角点 %s 不透明" % (p, xy))


class ResolveTests(unittest.TestCase):
    def test_default_resolves_to_robot(self):
        spec = companions.resolve({})
        self.assertEqual(spec.character, "robot")
        self.assertEqual(len(spec.idle), 1)
        self.assertIsNone(spec.dance)
        self.assertFalse(spec.custom)

    def test_penguin_resolves_two_idle_and_dance(self):
        spec = companions.resolve({"companion": {"character": "penguin"}})
        self.assertEqual(spec.character, "penguin")
        self.assertEqual(len(spec.idle), 2, "企鹅待命应为双动作轮播")
        self.assertTrue(spec.idle[0].endswith("idle_a.webp"))
        self.assertIsNotNone(spec.dance, "企鹅应带 右键舞蹈资源")
        self.assertTrue(spec.dance_enabled)

    def test_dance_disabled_when_enabled_off(self):
        spec = companions.resolve(
            {"companion": {"character": "penguin", "dance_enabled": False}})
        self.assertIsNone(spec.dance, "关闭彩蛋后 dance 应为 None")
        self.assertFalse(spec.dance_enabled)

    def test_legacy_gif_override_wins(self):
        spec = companions.resolve({
            "idle_gif": "my_idle.webp",
            "active_gif": "my_active.webp",
            "companion": {"character": "penguin"},
        })
        self.assertTrue(spec.custom)
        self.assertEqual(spec.character, "custom")
        self.assertTrue(spec.idle[0].endswith("my_idle.webp"))
        self.assertIsNone(spec.dance)

    def test_default_gif_values_are_not_custom(self):
        spec = companions.resolve({"idle_gif": "1.webp", "active_gif": "2.webp"})
        self.assertFalse(spec.custom, "默认值不算自定义覆盖")

    def test_unknown_character_falls_back_to_robot(self):
        spec = companions.resolve({"companion": {"character": "alien"}})
        self.assertEqual(spec.character, "robot")


class ConfigMergeTests(unittest.TestCase):
    def test_defaults_include_companion(self):
        self.assertEqual(config.DEFAULTS["companion"]["character"], "robot")
        self.assertTrue(config.DEFAULTS["companion"]["dance_enabled"])

    def test_load_config_merges_companion_keys(self):
        fake = {"companion": {"character": "penguin"}}
        import unittest.mock as mock
        with mock.patch.object(config, "_read_json", return_value=fake):
            cfg = config.load_config()
        self.assertEqual(cfg["companion"]["character"], "penguin")
        self.assertTrue(cfg["companion"]["dance_enabled"],
                        "未提供的键应保留默认值（按键合并而非整表覆盖）")


if __name__ == "__main__":
    unittest.main()
