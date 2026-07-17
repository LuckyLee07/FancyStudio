import tempfile
import unittest
from pathlib import Path
from unittest import mock

from requirement_schema import (
    GENERATOR_VERSION,
    REQUIRED_FIELDS,
    SCHEMA_VERSION,
    schema_document,
    validate_requirement_card,
    validate_with_single_repair,
)
from sop_store import DEFAULT_PROJECT_ID, SopStore, WorkflowError


ROOT = Path(__file__).resolve().parents[1]


def valid_card(source_text="床前明月光，疑是地上霜"):
    return {
        "theme": "羁旅思乡",
        "mood": "清寂",
        "time_and_place": "秋夜客舍",
        "subject": "独处的旅人",
        "core_imagery": ["明月", "霜色"],
        "composition": "月光从窗外落入，人物只留克制背影。",
        "must_have": ["月光"],
        "avoid": ["现代器物"],
        "historical_risks": ["室内陈设需复核"],
        "uncertainties": ["诗中没有明确季节与建筑形制"],
        "evidence": [
            {
                "source": "original_poem",
                "quote": source_text,
                "supports": ["theme", "mood", "core_imagery"],
            }
        ],
        "confidence": {
            "time_and_place": {
                "score": 0.45,
                "level": "low",
                "basis": "地点与季节是解释性推断",
                "requires_review": True,
            },
            "subject": {
                "score": 0.74,
                "level": "medium",
                "basis": "由诗意推导",
                "requires_review": False,
            },
            "composition": {
                "score": 0.68,
                "level": "medium",
                "basis": "构图是生产建议",
                "requires_review": False,
            },
            "historical_risks": {
                "score": 0.4,
                "level": "low",
                "basis": "尚未逐项核对史料",
                "requires_review": True,
            },
        },
        "editor_note": "",
        "locked_fields": [],
    }


class RequirementSchemaTests(unittest.TestCase):
    def test_schema_document_and_runtime_contract_stay_aligned(self):
        schema = schema_document()
        self.assertEqual(schema["title"], "RequirementCard v1")
        self.assertEqual(set(schema["required"]), set(REQUIRED_FIELDS))
        self.assertEqual(validate_requirement_card(valid_card()), [])

    def test_shape_errors_receive_exactly_one_repair(self):
        card = valid_card()
        card["unknown"] = "remove me"
        card["core_imagery"] = [" 明月 ", "明月"]
        del card["confidence"]

        repaired, result = validate_with_single_repair(
            card,
            source_text="床前明月光，疑是地上霜",
        )

        self.assertTrue(result["valid"])
        self.assertEqual(result["repair_attempts"], 1)
        self.assertNotIn("unknown", repaired)
        self.assertEqual(repaired["core_imagery"], ["明月"])
        self.assertTrue(repaired["confidence"]["time_and_place"]["requires_review"])

    def test_semantic_evidence_failure_remains_failed_after_repair(self):
        card = valid_card()
        card["evidence"][0]["quote"] = "并不存在于原诗的句子"

        _, result = validate_with_single_repair(
            card,
            source_text="床前明月光，疑是地上霜",
        )

        self.assertFalse(result["valid"])
        self.assertEqual(result["repair_attempts"], 1)
        self.assertIn(
            "QUOTE_NOT_IN_SOURCE",
            {item["code"] for item in result["final_issues"]},
        )


class RequirementGenerationContractTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = SopStore(
            Path(self.temp_dir.name) / "studio.db",
            ROOT / "data" / "poems.json",
            ROOT / "data" / "styles.json",
        )
        self.actor = {"id": "editor-contract", "role": "content_editor"}

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_same_versioned_input_uses_validated_cache(self):
        first = self.store.generate_requirements(
            DEFAULT_PROJECT_ID,
            ["jing-ye-si"],
            actor=self.actor,
        )["results"][0]

        with mock.patch.object(
            self.store,
            "_generate_requirement_candidate",
            side_effect=AssertionError("cache should bypass generator"),
        ):
            second = self.store.generate_requirements(
                DEFAULT_PROJECT_ID,
                ["jing-ye-si"],
                actor=self.actor,
            )["results"][0]

        self.assertTrue(second["cache_hit"])
        self.assertEqual(first["input_hash"], second["input_hash"])
        self.assertEqual(len(second["input_hash"]), 64)
        current = next(
            item for item in self.store.requirements() if item["poem_id"] == "jing-ye-si"
        )
        self.assertEqual(current["schema_version"], SCHEMA_VERSION)
        self.assertEqual(current["generator_version"], GENERATOR_VERSION)
        self.assertTrue(current["validation"]["valid"])
        runs = self.store.requirement_generation_runs(
            DEFAULT_PROJECT_ID,
            poem_id="jing-ye-si",
        )
        self.assertEqual(len(runs), 2)
        self.assertTrue(runs[0]["cache_hit"])

    def test_invalid_output_isolated_per_poem_and_recoverable(self):
        original = self.store._generate_requirement_candidate

        def candidate(poem, content_version, instruction):
            if poem["id"] == "jiang-xue":
                return {"theme": "残缺对象"}
            return original(poem, content_version, instruction)

        with mock.patch.object(
            self.store,
            "_generate_requirement_candidate",
            side_effect=candidate,
        ):
            result = self.store.generate_requirements(
                DEFAULT_PROJECT_ID,
                ["jiang-xue", "chun-xiao"],
                actor=self.actor,
            )

        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 1)
        failed = next(item for item in result["results"] if not item["ok"])
        self.assertEqual(failed["code"], "REQUIREMENT_SCHEMA_INVALID")
        failures = self.store.requirement_generation_runs(
            DEFAULT_PROJECT_ID,
            poem_id="jiang-xue",
            status="failed",
            unresolved_only=True,
        )
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["repair_attempts"], 1)
        self.assertEqual(
            next(item for item in self.store.list_poems()["items"] if item["id"] == "jiang-xue")["status"],
            "blocked",
        )
        snapshot = self.store.snapshot()
        self.assertEqual(len(snapshot["requirement_generation_failures"]), 1)
        self.assertIn(
            "failed_requirement_runs",
            {item["id"] for item in snapshot["production_report"]["anomalies"]},
        )
        detail = self.store.poem_detail("jiang-xue")
        self.assertEqual(detail["counts"]["requirement_generation_runs"], 1)

        recovered = self.store.generate_requirements(
            DEFAULT_PROJECT_ID,
            ["jiang-xue"],
            actor=self.actor,
        )["results"][0]
        self.assertTrue(recovered["ok"])
        self.assertEqual(
            self.store.requirement_generation_runs(
                DEFAULT_PROJECT_ID,
                poem_id="jiang-xue",
                status="failed",
                unresolved_only=True,
            ),
            [],
        )

    def test_repair_attempt_is_persisted_with_normalized_output(self):
        original = self.store._generate_requirement_candidate

        def repairable(poem, content_version, instruction):
            card = original(poem, content_version, instruction)
            card["unexpected"] = "remove"
            card["core_imagery"] = [card["core_imagery"][0], card["core_imagery"][0]]
            del card["confidence"]
            return card

        with mock.patch.object(
            self.store,
            "_generate_requirement_candidate",
            side_effect=repairable,
        ):
            result = self.store.generate_requirements(
                DEFAULT_PROJECT_ID,
                ["song-meng-hao-ran"],
                actor=self.actor,
            )["results"][0]

        self.assertTrue(result["ok"])
        self.assertEqual(result["repair_attempts"], 1)
        run = self.store.requirement_generation_runs(
            DEFAULT_PROJECT_ID,
            poem_id="song-meng-hao-ran",
        )[0]
        self.assertEqual(run["repair_attempts"], 1)
        self.assertTrue(run["validation"]["valid"])
        self.assertNotIn("unexpected", run["normalized_output"])
        self.assertIn("confidence", run["normalized_output"])

    def test_manual_revision_must_keep_schema_valid(self):
        generated = self.store.generate_requirements(
            DEFAULT_PROJECT_ID,
            ["chun-xiao"],
            actor=self.actor,
        )["results"][0]

        with self.assertRaises(WorkflowError) as context:
            self.store.revise_requirement(
                generated["requirement_id"],
                {"core_imagery": []},
                actor=self.actor,
            )
        self.assertEqual(context.exception.code, "REQUIREMENT_SCHEMA_INVALID")


if __name__ == "__main__":
    unittest.main()
