import json
import tempfile
import time
import unittest
from pathlib import Path

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
                    direction_ids.append(direction_id)
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
                            approved_by, created_at, updated_at
                        ) VALUES (?, ?, ?, 1, 'narrative', 1, ?, 'approved',
                                  'scale-fixture', 'scale-fixture', ?, ?)
                        """,
                        (
                            direction_id,
                            poem_id,
                            requirement_id,
                            json.dumps(
                                {"subject": "归舟", "shot": "远景"},
                                ensure_ascii=False,
                            ),
                            now,
                            now,
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
