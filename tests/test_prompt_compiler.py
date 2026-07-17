import copy
import unittest

from prompt_compiler import PromptCompileError, compile_generation_prompt


class PromptCompilerTests(unittest.TestCase):
    def payload(self):
        return {
            "poem": {
                "id": "jing-ye-si",
                "content_version_id": "content_jing-ye-si_v1",
                "content_version": 1,
                "title": "静夜思",
                "author": "李白",
                "dynasty": "唐",
                "lines": ["床前明月光", "疑是地上霜", "举头望明月", "低头思故乡"],
                "theme": "思乡",
                "mood": "清冷、克制",
            },
            "instruction": {
                "id": "instruction_global_v1",
                "version": 1,
                "name": "全局规范 v1",
                "content": {
                    "audience": "教育出版团队",
                    "visual_goal": "诗意准确、唐代语境合理、系列风格统一",
                    "composition_rules": ["主体清楚", "保留排版安全区"],
                    "historical_rules": ["器物符合唐代语境"],
                    "global_avoid": ["画面文字", "水印"],
                },
            },
            "requirement": {
                "id": "req_jing_v1",
                "version": 1,
                "instruction_id": "instruction_global_v1",
                "content": {
                    "theme": "思乡",
                    "mood": "清冷、克制",
                    "time_and_place": "唐代夜晚室内",
                    "subject": "独坐旅人",
                    "core_imagery": ["月光", "床前", "霜感"],
                    "must_have": ["月光", "低头姿态"],
                    "avoid": ["巨大月亮", "画面文字"],
                    "historical_risks": ["家具与服饰"],
                },
            },
            "direction": {
                "id": "dir_jing_narrative_v1",
                "version": 1,
                "type": "narrative",
                "content": {
                    "title": "客舍月夜",
                    "subject": "独坐旅人",
                    "shot": "中远景",
                    "foreground": "床沿与衣褶",
                    "midground": "低头人物",
                    "background": "纸窗月光",
                    "action": "由抬头转为低头",
                    "lighting": "冷月光",
                    "palette": "月白与黛青",
                    "whitespace": "右上留白",
                    "preserve": ["孤独尺度"],
                    "avoid": ["戏剧化哭泣"],
                },
            },
            "style": {
                "id": "ink-whitespace",
                "version_id": "stylev_ink-whitespace_v1",
                "version": 1,
                "name": "极简水墨留白",
                "prompt_fragment": "minimal Chinese ink wash painting",
                "palette": ["#EEEAE0", "#303634"],
            },
            "aspect_ratio": "portrait",
            "sample_index": 1,
        }

    def test_compilation_is_deterministic_and_matches_snapshot_hash(self):
        first = compile_generation_prompt(self.payload(), "openai")
        second = compile_generation_prompt(self.payload(), "openai")
        self.assertEqual(first, second)
        self.assertEqual(first["template_version"], "openai-six-segment-v1")
        self.assertEqual(
            first["hash"],
            "f37bdfe9c762595671e9aa800d735f76ee0fa6b9d11af913366a8873a883691f",
        )
        for heading in (
            "[01 CONTENT / 诗词正文]",
            "[02 REQUIREMENT / 内容需求]",
            "[03 DIRECTION / 已批准画面方向]",
            "[04 STYLE / 冻结风格版本]",
            "[05 OUTPUT / 输出规格]",
            "[06 GLOBAL / 全局规范]",
        ):
            self.assertIn(heading, first["text"])
        self.assertEqual(
            first["source_refs"]["style_version_id"],
            "stylev_ink-whitespace_v1",
        )

    def test_provider_direction_and_rework_change_hash_without_losing_lineage(self):
        payload = self.payload()
        openai = compile_generation_prompt(payload, "openai")
        demo = compile_generation_prompt(payload, "demo")
        self.assertNotEqual(openai["hash"], demo["hash"])

        changed = copy.deepcopy(payload)
        changed["direction"]["content"]["shot"] = "极远景"
        self.assertNotEqual(
            openai["hash"], compile_generation_prompt(changed, "openai")["hash"]
        )

        changed["rework"] = {
            "order_id": "rework_001",
            "parent_image_id": "a" * 32,
            "preserve": ["人物姿态"],
            "change": ["减少背景树木"],
            "avoid": ["新增角色"],
            "note": "保持月光方向",
        }
        rework = compile_generation_prompt(changed, "openai")
        self.assertIn("[07 REWORK / 结构化返工]", rework["text"])
        self.assertEqual(rework["segments"]["rework"]["parent_image_id"], "a" * 32)

    def test_missing_required_segment_is_rejected(self):
        payload = self.payload()
        payload["poem"]["lines"] = []
        with self.assertRaises(PromptCompileError) as context:
            compile_generation_prompt(payload, "openai")
        self.assertEqual(context.exception.code, "PROMPT_CONTENT_MISSING")


if __name__ == "__main__":
    unittest.main()
