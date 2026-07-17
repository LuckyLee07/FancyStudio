import json
import tempfile
import time
import unittest
from pathlib import Path

from direction_schema import (
    GENERATOR_VERSION as DIRECTION_GENERATOR_VERSION,
    SCHEMA_VERSION as DIRECTION_SCHEMA_VERSION,
    validate_direction_proposal,
)
from sop_store import DEFAULT_PROJECT_ID, SopStore, utc_now


ROOT = Path(__file__).resolve().parents[1]


class ProductionScaleTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = SopStore(
            Path(self.temp_dir.name) / "studio.db",
            ROOT / "data" / "poems.json",
            ROOT / "data" / "styles.json",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_three_hundred_poems_and_one_thousand_task_batch_stay_bounded(self):
        records = [
            {
                "id": f"scale-poem-{index:03d}",
                "title": f"规模测试诗 {index:03d}",
                "author": f"测试作者 {index % 20:02d}",
                "dynasty": "唐",
                "lines": ["青山入远目", "明月照归舟"],
                "theme": ["山水", "送别", "思乡", "田园"][index % 4],
                "mood": "克制、清远",
                "imagery": ["青山", "明月", "归舟"],
                "source": "自动化规模测试数据",
            }
            for index in range(1, 291)
        ]
        imported = self.store.import_poems(
            DEFAULT_PROJECT_ID,
            records,
            actor={"id": "scale-fixture", "role": "system"},
        )
        self.assertEqual(imported["imported"], 290)
        self.assertEqual(self.store.summary()["total_poems"], 300)

        all_poem_ids = [
            row["id"]
            for row in self.store.list_poems(limit=500)["items"]
        ]
        selected_poem_ids = all_poem_ids[:250]
        direction_ids = []
        now = utc_now()
        instruction_id = self.store.instruction()["id"]
        with self.store.lock, self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for index, poem_id in enumerate(selected_poem_ids):
                    requirement_id = f"req_scale_{index:04d}"
                    direction_id = f"dir_scale_{index:04d}"
                    generation_run_id = f"dir_run_scale_{index:04d}"
                    direction_ids.append(direction_id)
                    poem_row = connection.execute(
                        "SELECT lines_json FROM poems WHERE id = ?",
                        (poem_id,),
                    ).fetchone()
                    poem_lines = json.loads(poem_row["lines_json"])
                    source_text = "，".join(poem_lines)
                    evidence_quote = poem_lines[0]
                    direction_content = {
                        "type": "narrative",
                        "title": "归舟入画",
                        "visual_thesis": "以归舟穿行青山月色承载行旅中的克制乡愁",
                        "subject": "月下归舟",
                        "subject_mode": "human_focus",
                        "scene": "青山与水面相接的月夜归途",
                        "shot": "远景俯视，人物仅作舟中小点",
                        "shot_scale": "wide",
                        "narrative_mode": "narrative",
                        "foreground": "近岸芦苇与水纹",
                        "midground": "一叶归舟沿月光前行",
                        "background": "层叠青山隐入夜色",
                        "action": "归舟缓慢驶向远山",
                        "composition": "水道形成斜向引导线，主体落在右下三分点",
                        "lighting": "清冷月光与微弱舟灯形成冷暖对照",
                        "palette": "黛青、月白与少量暖褐",
                        "whitespace": "左上天空保留大面积呼吸感",
                        "preserve": ["青山", "归舟", "月夜气氛"],
                        "avoid": ["现代建筑", "画面文字", "夸张人物表情"],
                        "text_safe_area": "左上天空区域，避开山脊与月亮",
                        "risk_note": "人物服饰和舟型需符合唐代语境",
                        "interpretation_layers": {
                            "poem_facts": [
                                {
                                    "claim": "画面依据当前诗文正文",
                                    "evidence_quote": evidence_quote,
                                }
                            ],
                            "reasonable_inferences": [
                                {
                                    "claim": "使用月夜水路表现克制清远的情绪",
                                    "basis": "诗词情绪与核心意象共同指向清冷行旅",
                                }
                            ],
                            "creative_choices": [
                                {
                                    "claim": "增加微弱舟灯作为视觉焦点",
                                    "purpose": "在冷色画面中稳定主体层级",
                                }
                            ],
                        },
                        "art_director_note": "",
                        "locked_fields": [],
                    }
                    self.assertEqual(
                        validate_direction_proposal(
                            direction_content,
                            source_text=source_text,
                        ),
                        [],
                    )
                    connection.execute(
                        """
                        INSERT INTO requirements(
                            id, poem_id, instruction_id, version, is_current,
                            content_json, status, created_by, approved_by,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, 1, 1, ?, 'approved', 'scale-fixture',
                                  'scale-fixture', ?, ?)
                        """,
                        (
                            requirement_id,
                            poem_id,
                            instruction_id,
                            json.dumps(
                                {
                                    "theme": "规模测试",
                                    "must_have": ["青山"],
                                    "avoid": ["文字"],
                                },
                                ensure_ascii=False,
                            ),
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO directions(
                            id, poem_id, requirement_id, version, type,
                            is_current, content_json, status, created_by,
                            approved_by, created_at, updated_at,
                            schema_version, generator_version, input_hash,
                            cache_hit, validation_json, generation_run_id
                        ) VALUES (?, ?, ?, 1, 'narrative', 1, ?, 'approved',
                                  'scale-fixture', 'scale-fixture', ?, ?,
                                  ?, ?, ?, 0, ?, ?)
                        """,
                        (
                            direction_id,
                            poem_id,
                            requirement_id,
                            json.dumps(direction_content, ensure_ascii=False),
                            now,
                            now,
                            DIRECTION_SCHEMA_VERSION,
                            DIRECTION_GENERATOR_VERSION,
                            f"scale-input-{index:04d}",
                            json.dumps(
                                {
                                    "valid": True,
                                    "schema_version": DIRECTION_SCHEMA_VERSION,
                                    "fixture": "performance-scale",
                                },
                                ensure_ascii=False,
                            ),
                            generation_run_id,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE content_versions
                        SET status='approved', approved_by='scale-fixture'
                        WHERE poem_id=?
                        """,
                        (poem_id,),
                    )
                    connection.execute(
                        """
                        UPDATE poems SET status='ready_for_production',
                            blocked_reason='', updated_at=? WHERE id=?
                        """,
                        (now, poem_id),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

        started = time.perf_counter()
        batch = self.store.create_batch(
            DEFAULT_PROJECT_ID,
            selected_poem_ids,
            direction_ids=direction_ids,
            style_id="ink-whitespace",
            aspect_ratio="portrait",
            count_per_direction=4,
            provider="demo",
            model="demo-renderer",
            unit_cost=0,
            actor={"id": "scale-producer", "role": "producer"},
        )
        create_seconds = time.perf_counter() - started
        self.assertEqual(batch["task_count"], 1000)
        self.assertLess(create_seconds, 5.0)

        started = time.perf_counter()
        tasks = self.store.tasks(batch_id=batch["id"], limit=1000)
        task_read_seconds = time.perf_counter() - started
        self.assertEqual(len(tasks), 1000)
        self.assertEqual(len({task["idempotency_key"] for task in tasks}), 1000)
        self.assertLess(task_read_seconds, 2.0)

        task_page = self.store.task_page(
            batch_id=batch["id"], limit=75, offset=900
        )
        self.assertEqual(task_page["total"], 1000)
        self.assertEqual(len(task_page["items"]), 75)
        self.assertTrue(task_page["has_previous"])
        self.assertTrue(task_page["has_next"])
        self.assertEqual(task_page["offset"], 900)
        filtered_tasks = self.store.task_page(
            batch_id=batch["id"], q="规模测试诗", limit=25
        )
        expected_filtered_total = sum(
            poem_id.startswith("scale-poem-") for poem_id in selected_poem_ids
        ) * 4
        self.assertEqual(filtered_tasks["total"], expected_filtered_total)
        self.assertTrue(
            all("规模测试诗" in item["poem_title"] for item in filtered_tasks["items"])
        )

        started = time.perf_counter()
        page = self.store.list_poems(limit=50, offset=250)
        summary = self.store.summary()
        report = self.store.production_report(days=7)
        aggregate_seconds = time.perf_counter() - started
        self.assertEqual(page["total"], 300)
        self.assertEqual(len(page["items"]), 50)
        self.assertEqual(summary["total_poems"], 300)
        self.assertEqual(report["tasks"]["total"], 1000)
        self.assertLess(aggregate_seconds, 2.0)


if __name__ == "__main__":
    unittest.main()
