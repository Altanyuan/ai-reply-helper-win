import os
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from contact_rules import config_for_prompt, resolve_prompt_id
except ImportError:
    config_for_prompt = None
    resolve_prompt_id = None


class ContactRuleResolutionTests(unittest.TestCase):
    def test_exact_name_selects_only_its_prompt(self):
        self.assertIsNotNone(resolve_prompt_id, "contact rule resolver is not implemented")
        prompt_id, reason = resolve_prompt_id(
            "王总",
            [{"name": "王总", "prompt_id": "business"}],
            [{"id": "business"}, {"id": "friendly"}],
            "global",
        )
        self.assertEqual((prompt_id, reason), ("business", "matched"))

    def test_partial_or_case_changed_name_uses_default(self):
        self.assertIsNotNone(resolve_prompt_id, "contact rule resolver is not implemented")
        rules = [{"name": "王总", "prompt_id": "business"}]
        prompts = [{"id": "business"}, {"id": "global"}]
        self.assertEqual(resolve_prompt_id("王", rules, prompts, "global")[0], "global")
        self.assertEqual(resolve_prompt_id("王总 ", rules, prompts, "global")[0], "business")
        self.assertEqual(resolve_prompt_id("Wang总", rules, prompts, "global")[0], "global")

    def test_missing_title_duplicate_rules_and_stale_prompt_use_default(self):
        self.assertIsNotNone(resolve_prompt_id, "contact rule resolver is not implemented")
        prompts = [{"id": "business"}, {"id": "global"}]
        self.assertEqual(resolve_prompt_id(None, [], prompts, "global")[0], "global")
        self.assertEqual(
            resolve_prompt_id("妈妈", [
                {"name": "妈妈", "prompt_id": "friendly"},
                {"name": "妈妈", "prompt_id": "business"},
            ], prompts + [{"id": "friendly"}], "global"),
            ("global", "ambiguous"),
        )
        self.assertEqual(
            resolve_prompt_id("妈妈", [{"name": "妈妈", "prompt_id": "deleted"}], prompts, "global"),
            ("global", "invalid"),
        )

    def test_unique_chat_title_candidate_only(self):
        from capture_wgc import select_chat_title

        self.assertEqual(
            select_chat_title([("妈妈", (20, 10, 70, 30))], 900), "妈妈",
        )
        self.assertIsNone(select_chat_title([
            ("妈妈", (20, 10, 70, 30)),
            ("妈妈2", (90, 10, 140, 30)),
        ], 900))

    def test_title_symbol_boxes_are_ignored(self):
        """返回箭头之类的纯符号框不能把「唯一候选」挤成两个。"""
        from capture_wgc import select_chat_title

        self.assertEqual(select_chat_title([
            ("‹", (6, 12, 18, 32)),
            ("王总", (30, 10, 90, 34)),
        ], 900), "王总")
        self.assertIsNone(select_chat_title([("‹", (6, 12, 18, 32))], 900))

    def test_split_title_boxes_are_merged(self):
        """长群名被 OCR 拆成相邻两段时要合并，而不是直接判定为无法识别。"""
        from capture_wgc import select_chat_title

        self.assertEqual(select_chat_title([
            ("华图王七七", (30, 10, 150, 34)),
            ("（你必上岸！）", (152, 12, 260, 32)),
        ], 900), "华图王七七（你必上岸！）")
        # 上下两行（如昵称 + 副标题）不能合并，仍按多候选拒绝
        self.assertIsNone(select_chat_title([
            ("妈妈", (30, 10, 150, 34)),
            ("对方正在输入", (30, 40, 200, 62)),
        ], 900))

    def test_title_right_side_buttons_are_excluded(self):
        """左 25% 之外的工具按钮文字不参与候选。"""
        from capture_wgc import select_chat_title

        self.assertEqual(select_chat_title([
            ("王总", (30, 10, 90, 34)),
            ("聊天信息", (700, 10, 780, 34)),
        ], 900), "王总")

    def test_small_icon_box_is_not_appended_to_title(self):
        """真机形态：左侧图标被 OCR 成矮小字符，不能粘到名字末尾。"""
        from capture_wgc import select_chat_title

        self.assertEqual(select_chat_title([
            ("【武农商-金网络】系统对接群(27)", (125, 36, 430, 66)),
            ("卡", (58, 44, 72, 58)),
        ], 503), "【武农商-金网络】系统对接群(27)")
        # 正常字高的单字昵称仍然保留
        self.assertEqual(select_chat_title([("卡", (30, 36, 60, 66))], 503), "卡")

    def test_per_run_prompt_does_not_change_global_config(self):
        self.assertIsNotNone(config_for_prompt, "per-run config helper is not implemented")
        global_cfg = {"active_prompt_id": "global", "contact_rules": []}
        run_cfg = config_for_prompt(global_cfg, "business")
        self.assertEqual(global_cfg["active_prompt_id"], "global")
        self.assertEqual(run_cfg["active_prompt_id"], "business")
        self.assertIsNot(run_cfg, global_cfg)

    def test_rules_are_saved_and_loaded_from_local_config(self):
        import config

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with patch.object(config, "_config_path", return_value=path):
                config.set_contact_rules([
                    {"name": "妈妈", "prompt_id": "builtin_friendly"},
                    {"name": " ", "prompt_id": "builtin_general"},
                ])
                stored = json.loads(path.read_text(encoding="utf-8"))
                loaded = config.get_contact_rules()

        self.assertEqual(len(stored["contact_rules"]), 1)
        self.assertEqual(loaded[0]["name"], "妈妈")
        self.assertEqual(loaded[0]["prompt_id"], "builtin_friendly")
        self.assertTrue(loaded[0]["id"])


if __name__ == "__main__":
    unittest.main()
