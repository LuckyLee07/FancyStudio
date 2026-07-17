import json
import unittest

from style_schema import (
    ART_BIBLE_REQUIRED_FIELDS,
    STYLE_REQUIRED_FIELDS,
    art_bible_schema_document,
    style_pack_schema_document,
    validate_art_bible,
    validate_style_pack,
)


class StyleContractTests(unittest.TestCase):
    def art_bible(self):
        return {
            "palette_rules": ["低至中饱和"],
            "line_rules": ["线条服务结构"],
            "character_proportion_rules": ["人物比例自然"],
            "spatial_rules": ["保留文字安全区"],
            "material_rules": ["纸本纹理可复现"],
            "text_prohibitions": ["画面不得出现文字"],
            "historical_boundaries": ["服饰器物符合唐代语境"],
            "benchmark_policy": {
                "benchmark_poem_count": 12,
                "min_poems_per_release": 5,
                "min_samples_per_poem": 4,
                "min_style_match_score": 75,
                "max_off_topic_rate": 0.2,
            },
        }

    def style_pack(self):
        return {
            "style_id": "ink-whitespace",
            "name": "极简水墨留白",
            "short_name": "水墨留白",
            "semantic_version": "1.1.0",
            "description": "大留白和克制墨色。",
            "prompt_fragment": "minimal Chinese ink wash",
            "release_notes": "调整纸张灰阶。",
            "art_bible_version_id": "artbible_global_v1",
            "visual_traits": {
                "line": "干湿并用的墨线",
                "texture": "生宣纸纤维",
                "lighting": "墨色虚实",
                "contrast": "局部高对比",
                "saturation": "近单色",
                "whitespace": "高留白",
            },
            "character_design": {
                "proportion": "自然比例",
                "expression": "克制",
                "costume": "唐代服饰轮廓",
            },
            "palette": ["#EEEAE0", "#303634"],
            "applicable_topics": ["山水"],
            "avoid": ["现代器物"],
            "risks": ["画面过空"],
            "positive_examples": ["主体虽小但焦点明确"],
            "negative_examples": ["所有诗都套用孤舟背影"],
            "settings": {
                "background": "#EEEAE0",
                "foreground": "#303634",
                "accent": "#767B78",
                "paper": "cool",
            },
        }

    def test_json_schemas_and_runtime_required_fields_align(self):
        self.assertEqual(
            set(art_bible_schema_document()["required"]),
            set(ART_BIBLE_REQUIRED_FIELDS),
        )
        self.assertEqual(
            set(style_pack_schema_document()["required"]),
            set(STYLE_REQUIRED_FIELDS),
        )
        self.assertEqual(validate_art_bible(self.art_bible()), [])
        self.assertEqual(validate_style_pack(self.style_pack()), [])

    def test_art_bible_rejects_weak_release_policy(self):
        payload = self.art_bible()
        payload["benchmark_policy"]["min_poems_per_release"] = 4
        issues = validate_art_bible(payload)
        self.assertIn("INVALID_INTEGER_RANGE", {item["code"] for item in issues})

    def test_style_pack_requires_semver_examples_risks_and_visual_contract(self):
        payload = self.style_pack()
        payload["semantic_version"] = "v2"
        payload["negative_examples"] = []
        del payload["visual_traits"]["line"]
        issues = validate_style_pack(payload)
        codes = {item["code"] for item in issues}
        self.assertIn("INVALID_SEMVER", codes)
        self.assertIn("NON_EMPTY_ARRAY_REQUIRED", codes)
        self.assertIn("NON_EMPTY_REQUIRED", codes)


if __name__ == "__main__":
    unittest.main()
