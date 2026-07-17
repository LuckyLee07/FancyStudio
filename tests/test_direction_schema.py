import tempfile
import unittest
from pathlib import Path
from unittest import mock

from direction_schema import (
    GENERATOR_VERSION,
    REQUIRED_FIELDS,
    SCHEMA_VERSION,
    diversity_report,
    schema_document,
    validate_direction_set,
    validate_with_single_repair,
)
from sop_store import DEFAULT_PROJECT_ID, SopStore, WorkflowError


ROOT = Path(__file__).resolve().parents[1]
SOURCE_TEXT = "床前明月光，疑是地上霜，举头望明月，低头思故乡"


def proposal(
    direction_type,
    subject_mode,
    shot_scale,
    narrative_mode,
    suffix,
):
    return {
        "type": direction_type,
        "title": f"月夜方向{suffix}",
        "visual_thesis": f"以不同视觉结构表达思乡{suffix}",
        "subject": f"方向主体{suffix}",
        "subject_mode": subject_mode,
        "scene": "唐代夜晚旅舍",
        "shot": f"{shot_scale} 对应景别",
        "shot_scale": shot_scale,
        "narrative_mode": narrative_mode,
        "foreground": f"前景{suffix}",
        "midground": f"中景{suffix}",
        "background": f"背景{suffix}",
        "action": f"动作{suffix}",
        "composition": f"独立构图结构{suffix}",
        "lighting": "克制月光",
        "palette": "月白与黛青",
        "whitespace": "中高留白",
        "preserve": ["明月"],
        "avoid": ["现代器物"],
        "text_safe_area": f"{suffix}侧低细节区域",
        "risk_note": "服饰与家具需要人工复核",
        "interpretation_layers": {
            "poem_facts": [
                {"claim": "原诗明确出现明月", "evidence_quote": "床前明月光"}
            ],
            "reasonable_inferences": [
                {"claim": "场景按旅舍处理", "basis": "由床前与羁旅主题推导"}
            ],
            "creative_choices": [
                {"claim": f"采用构图{suffix}", "purpose": "与另外两个方向形成差异"}
            ],
        },
        "art_director_note": "",
        "locked_fields": [],
    }


def valid_set():
    return [
        proposal("narrative", "human_focus", "medium", "narrative", "甲"),
        proposal("atmospheric", "environment_focus", "wide", "atmosphere", "乙"),
        proposal("symbolic", "object_focus", "close", "symbolism", "丙"),
    ]


class DirectionSchemaTests(unittest.TestCase):
    def test_schema_runtime_contract_and_diversity_report_align(self):
        schema = schema_document()
        self.assertEqual(schema["title"], "DirectionProposal v1")
        self.assertEqual(set(schema["required"]), set(REQUIRED_FIELDS))
        issues, report = validate_direction_set(valid_set(), source_text=SOURCE_TEXT)
        self.assertEqual(issues, [])
        self.assertTrue(report["valid"])
        self.assertEqual(report["minimum_axis_differences"], 3)
        self.assertTrue(all(item["difference_count"] == 3 for item in report["pairs"]))

    def test_shape_errors_receive_one_repair_but_semantic_diversity_does_not(self):
        items = valid_set()
        items[0]["unexpected"] = "remove"
        items[0]["preserve"] = [" 明月 ", "明月"]
        del items[0]["art_director_note"]
        repaired, result = validate_with_single_repair(items, source_text=SOURCE_TEXT)
        self.assertTrue(result["valid"])
        self.assertEqual(result["repair_attempts"], 1)
        self.assertNotIn("unexpected", repaired[0])
        self.assertEqual(repaired[0]["preserve"], ["明月"])

        collapsed = valid_set()
        collapsed[1]["subject_mode"] = "human_focus"
        collapsed[1]["shot_scale"] = "medium"
        _, failed = validate_with_single_repair(collapsed, source_text=SOURCE_TEXT)
        self.assertFalse(failed["valid"])
        self.assertIn(
            "DIRECTION_DIVERSITY_INSUFFICIENT",
            {item["code"] for item in failed["final_issues"]},
        )

    def test_poem_fact_quote_must_exist_in_current_content_version(self):
        items = valid_set()
        items[2]["interpretation_layers"]["poem_facts"][0]["evidence_quote"] = "不存在的诗句"
        issues, _ = validate_direction_set(items, source_text=SOURCE_TEXT)
        self.assertIn("QUOTE_NOT_IN_SOURCE", {item["code"] for item in issues})


class DirectionGenerationContractTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = SopStore(
            Path(self.temp_dir.name) / "studio.db",
            ROOT / "data" / "poems.json",
            ROOT / "data" / "styles.json",
        )
        self.editor = {"id": "editor-direction", "role": "content_editor"}
        self.art = {"id": "art-direction", "role": "art_director"}

    def tearDown(self):
        self.temp_dir.cleanup()

    def approve_requirement(self, poem_id):
        generated = self.store.generate_requirements(
            DEFAULT_PROJECT_ID,
            [poem_id],
            actor=self.editor,
        )["results"][0]
        self.store.decide_requirement(
            generated["requirement_id"],
            "approve",
            actor=self.editor,
        )

    def test_atomic_three_direction_generation_is_versioned_and_cached(self):
        self.approve_requirement("jing-ye-si")
        first = self.store.generate_directions(
            DEFAULT_PROJECT_ID,
            ["jing-ye-si"],
            actor=self.art,
        )["results"][0]
        self.assertTrue(first["ok"])
        self.assertEqual(len(first["direction_ids"]), 3)
        self.assertEqual(first["diversity"]["minimum_axis_differences"], 3)
        current = [
            item for item in self.store.directions() if item["poem_id"] == "jing-ye-si"
        ]
        self.assertEqual({item["schema_version"] for item in current}, {SCHEMA_VERSION})
        self.assertEqual({item["generator_version"] for item in current}, {GENERATOR_VERSION})
        self.assertTrue(all(item["validation"]["valid"] for item in current))

        with mock.patch.object(
            self.store,
            "_generate_direction_candidates",
            side_effect=AssertionError("cache should bypass planner"),
        ):
            second = self.store.generate_directions(
                DEFAULT_PROJECT_ID,
                ["jing-ye-si"],
                actor=self.art,
            )["results"][0]
        self.assertTrue(second["cache_hit"])
        self.assertEqual(first["input_hash"], second["input_hash"])
        runs = self.store.direction_generation_runs(
            DEFAULT_PROJECT_ID,
            poem_id="jing-ye-si",
        )
        self.assertEqual(len(runs), 2)
        self.assertTrue(runs[0]["cache_hit"])

    def test_invalid_set_writes_no_partial_directions_and_can_recover(self):
        self.approve_requirement("jiang-xue")
        original = self.store._generate_direction_candidates

        def invalid(poem, requirement, source_text):
            items = original(poem, requirement, source_text)
            items[1]["subject_mode"] = items[0]["subject_mode"]
            items[1]["shot_scale"] = items[0]["shot_scale"]
            return items

        with mock.patch.object(
            self.store,
            "_generate_direction_candidates",
            side_effect=invalid,
        ):
            failed = self.store.generate_directions(
                DEFAULT_PROJECT_ID,
                ["jiang-xue"],
                actor=self.art,
            )["results"][0]
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["code"], "DIRECTION_SET_INVALID")
        self.assertEqual(
            [item for item in self.store.directions() if item["poem_id"] == "jiang-xue"],
            [],
        )
        failures = self.store.direction_generation_runs(
            DEFAULT_PROJECT_ID,
            poem_id="jiang-xue",
            status="failed",
            unresolved_only=True,
        )
        self.assertEqual(len(failures), 1)
        snapshot = self.store.snapshot()
        self.assertEqual(len(snapshot["direction_generation_failures"]), 1)
        self.assertIn(
            "failed_direction_runs",
            {item["id"] for item in snapshot["production_report"]["anomalies"]},
        )

        recovered = self.store.generate_directions(
            DEFAULT_PROJECT_ID,
            ["jiang-xue"],
            actor=self.art,
        )["results"][0]
        self.assertTrue(recovered["ok"])
        self.assertEqual(
            self.store.direction_generation_runs(
                DEFAULT_PROJECT_ID,
                poem_id="jiang-xue",
                status="failed",
                unresolved_only=True,
            ),
            [],
        )

    def test_manual_revision_cannot_collapse_direction_axes(self):
        self.approve_requirement("chun-xiao")
        generated = self.store.generate_directions(
            DEFAULT_PROJECT_ID,
            ["chun-xiao"],
            actor=self.art,
        )["results"][0]
        narrative_id = generated["direction_ids"][0]
        with self.assertRaises(WorkflowError) as context:
            self.store.revise_direction(
                narrative_id,
                {"subject_mode": "environment_focus", "shot_scale": "wide"},
                actor=self.art,
            )
        self.assertEqual(context.exception.code, "DIRECTION_SET_INVALID")


if __name__ == "__main__":
    unittest.main()
