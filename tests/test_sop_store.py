import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from qc_engine import inspect_image
from review_schema import compose_qc_result
from sop_store import DEFAULT_PROJECT_ID, SopStore, WorkflowError
from visual_reviewer import review_image


ROOT = Path(__file__).resolve().parents[1]


class SopStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "studio.db"
        self.store = SopStore(
            self.database_path,
            ROOT / "data" / "poems.json",
            ROOT / "data" / "styles.json",
        )
        self.actor = {"id": "editor-01", "role": "content_editor"}

    def ready_poem(self, poem_id="jing-ye-si"):
        requirement_result = self.store.generate_requirements(
            DEFAULT_PROJECT_ID,
            [poem_id],
            actor=self.actor,
        )
        requirement_id = requirement_result["results"][0]["requirement_id"]
        self.store.decide_requirement(
            requirement_id,
            "approve",
            actor=self.actor,
        )
        direction_result = self.store.generate_directions(
            DEFAULT_PROJECT_ID,
            [poem_id],
            actor={"id": "art-01", "role": "art_director"},
        )
        direction_id = direction_result["results"][0]["direction_ids"][0]
        self.store.decide_direction(
            direction_id,
            "approve",
            actor={"id": "art-01", "role": "art_director"},
        )
        return direction_id

    def reviewed_inspection(self, path, task):
        local = inspect_image(path, task["aspect_ratio"])
        policy = self.store.published_qc_policy(task["project_id"])
        prompt = task["prompt"]
        direction = prompt.get("direction") or {}
        result, metadata = review_image(
            path,
            image_provider="demo",
            context={
                "poem": prompt.get("poem") or {},
                "requirement": (prompt.get("requirement") or {}).get("content") or {},
                "direction": {
                    "type": direction.get("type"),
                    **(direction.get("content") or {}),
                },
                "style": prompt.get("style") or {},
            },
            policy_version_id=policy["id"],
        )
        return compose_qc_result(
            local,
            result,
            policy["content"],
            policy_version_id=policy["id"],
            reviewer_metadata=metadata,
        )

    def final_candidate(self, poem_id="shan-ju-qiu-ming", image_id="c" * 32):
        direction_id = self.ready_poem(poem_id)
        batch = self.store.create_batch(
            DEFAULT_PROJECT_ID,
            [poem_id],
            direction_ids=[direction_id],
            style_id="ink-whitespace",
            aspect_ratio="portrait",
            count_per_direction=1,
            provider="demo",
            model="demo-renderer",
            unit_cost=0,
            actor=self.actor,
        )
        self.store.start_batch(batch["id"], actor=self.actor)
        task = self.store.claim_next_task(batch["id"])
        path = Path(self.temp_dir.name) / f"{image_id}.svg"
        path.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1536"><rect width="1024" height="1536" fill="#f2eee2"/><circle cx="400" cy="450" r="150" fill="#79928f"/></svg>',
            encoding="utf-8",
        )
        record = {
            "id": image_id,
            "url": f"/generated/{image_id}.svg",
            "prompt": "traceable final candidate prompt",
            "created_at": "2026-07-18T00:00:00+00:00",
        }
        image = self.store.register_production_image(
            record, task, self.reviewed_inspection(path, task)
        )
        self.store.complete_task(
            task["id"], task["attempt_id"],
            output_image_id=image_id, actual_cost=0, duration_ms=15,
        )
        image = self.store.decide_image(
            image_id,
            "final_candidate",
            reason_tags=["诗意准确", "构图最佳"],
            actor={"id": "art-01", "role": "art_director"},
        )
        return image, path

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_seed_creates_project_poems_instruction_and_content_versions(self):
        snapshot = self.store.snapshot()

        self.assertEqual(snapshot["summary"]["project"]["id"], DEFAULT_PROJECT_ID)
        self.assertEqual(snapshot["summary"]["total_poems"], 12)
        self.assertEqual(
            snapshot["summary"]["status_counts"]["requirement_draft"], 12
        )
        self.assertEqual(len(snapshot["poems"]), 12)
        self.assertEqual(snapshot["requirements"], [])
        self.assertEqual(snapshot["directions"], [])
        self.assertEqual(snapshot["instruction"]["status"], "published")
        self.assertEqual(len(snapshot["instruction_versions"]), 1)
        self.assertEqual(len(snapshot["style_packs"]), 6)
        self.assertTrue(
            all(style["status"] == "active" for style in snapshot["style_packs"])
        )
        self.assertEqual(snapshot["art_bible"]["semantic_version"], "1.0.0")
        self.assertEqual(len(snapshot["style_benchmark_poems"]), 12)
        self.assertEqual(snapshot["qc_policy"]["content"]["semantic_version"], "1.0.0")
        self.assertEqual(snapshot["qc_calibration"]["sample_count"], 0)

    def test_poem_detail_returns_complete_trace_without_local_asset_paths(self):
        image, _ = self.final_candidate("shan-ju-qiu-ming", "d" * 32)
        detail = self.store.poem_detail("shan-ju-qiu-ming")
        self.assertEqual(detail["poem"]["title"], "山居秋暝")
        self.assertEqual(detail["counts"]["content_versions"], 1)
        self.assertEqual(detail["counts"]["requirements"], 1)
        self.assertEqual(detail["counts"]["directions"], 3)
        self.assertEqual(detail["counts"]["tasks"], 1)
        self.assertEqual(detail["counts"]["images"], 1)
        self.assertEqual(detail["images"][0]["id"], image["id"])
        self.assertNotIn("file_path", detail["images"][0])
        self.assertTrue(
            any(item["action"] == "image.final_candidate" for item in detail["audit_events"])
        )
        with self.assertRaises(WorkflowError) as missing:
            self.store.poem_detail("missing-poem")
        self.assertEqual(missing.exception.status, 404)

    def test_instruction_and_style_versions_publish_atomically_and_freeze_batches(self):
        content = {
            "audience": "少儿出版团队",
            "visual_goal": "唐代语境准确，系列视觉统一",
            "composition_rules": ["主体清晰", "保留排版安全区"],
            "historical_rules": ["器物符合唐代语境"],
            "global_avoid": ["文字", "水印"],
        }
        draft = self.store.create_instruction_version(
            DEFAULT_PROJECT_ID,
            name="少儿出版创作规范 v2",
            content=content,
            actor=self.actor,
        )
        self.assertEqual(draft["version"], 2)
        self.assertEqual(draft["status"], "draft")
        with self.assertRaises(WorkflowError) as role_context:
            self.store.publish_instruction_version(draft["id"], actor=self.actor)
        self.assertEqual(role_context.exception.status, 403)
        published = self.store.publish_instruction_version(
            draft["id"], actor={"id": "producer-01", "role": "producer"}
        )
        self.assertEqual(published["status"], "published")
        versions = self.store.instructions()
        self.assertEqual([item["status"] for item in versions], ["published", "retired"])

        style_v2 = self.store.create_style_pack_version(
            DEFAULT_PROJECT_ID,
            style_id="ink-whitespace",
            name="极简水墨留白 · 印刷增强",
            short_name="水墨印刷",
            description="增强纸张层次并限制纯黑面积。",
            semantic_version="1.1.0",
            release_notes="调整印刷灰阶与纸张层次，保持水墨留白基线。",
            art_bible_version_id=self.store.published_art_bible()["id"],
            prompt_fragment="minimal ink wash, print-safe tonal range",
            palette=["#F0EDE5", "#292E2C"],
            settings={
                "background": "#F0EDE5",
                "foreground": "#292E2C",
                "accent": "#767B78",
                "paper": "cool",
            },
            applicable_topics=["山水", "羁旅"],
            visual_traits={
                "line": "干湿并用的克制墨线",
                "texture": "可印刷的冷调纸纹",
                "lighting": "墨色虚实",
                "contrast": "局部高对比",
                "saturation": "近单色",
                "whitespace": "高留白",
            },
            character_design={
                "proportion": "自然比例且尺度偏小",
                "expression": "含蓄克制",
                "costume": "唐代服饰轮廓经审核",
            },
            avoid=["现代器物", "画面文字"],
            risks=["画面过空", "跨诗构图同质化"],
            positive_examples=["主体虽小但焦点明确"],
            negative_examples=["所有诗都套用孤舟背影"],
            actor={"id": "art-01", "role": "art_director"},
        )
        self.assertEqual(style_v2["version"], 2)
        with self.assertRaises(WorkflowError) as benchmark_gate:
            self.store.publish_style_pack_version(
                style_v2["id"], actor={"id": "art-01", "role": "art_director"}
            )
        self.assertEqual(benchmark_gate.exception.code, "STYLE_BENCHMARK_REQUIRED")

        benchmark_poem_ids = [
            "jing-ye-si",
            "jiang-xue",
            "lu-zhai",
            "feng-qiao-ye-bo",
            "chun-xiao",
        ]
        approved_directions = {
            poem_id: self.ready_poem(poem_id) for poem_id in benchmark_poem_ids
        }
        benchmark = self.store.create_style_benchmark_run(
            style_v2["id"],
            poem_ids=benchmark_poem_ids,
            provider="demo",
            model="demo-renderer",
            unit_cost=0,
            actor={"id": "art-01", "role": "art_director"},
        )
        self.assertEqual(benchmark["batch"]["task_count"], 20)
        started = self.store.start_style_benchmark(
            benchmark["run"]["id"],
            actor={"id": "art-01", "role": "art_director"},
        )
        self.assertEqual(started["batch"]["status"], "queued")
        for index in range(20):
            task = self.store.claim_next_task(benchmark["batch"]["id"])
            self.assertIsNotNone(task)
            image_id = f"{index + 1:032x}"
            path = Path(self.temp_dir.name) / f"benchmark-{index}.svg"
            path.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1536">'
                f'<rect width="1024" height="1536" fill="#{index + 1:06x}"/>'
                '</svg>',
                encoding="utf-8",
            )
            self.store.register_production_image(
                {
                    "id": image_id,
                    "url": f"/generated/{image_id}.svg",
                    "prompt": "style benchmark sample",
                },
                task,
                inspect_image(path, "portrait"),
            )
            self.store.complete_task(
                task["id"],
                task["attempt_id"],
                output_image_id=image_id,
                actual_cost=0,
                duration_ms=5,
            )
        with self.assertRaises(WorkflowError) as isolated_image:
            self.store.decide_image(
                f"{1:032x}",
                "candidate",
                actor={"id": "art-01", "role": "art_director"},
            )
        self.assertEqual(
            isolated_image.exception.code, "STYLE_BENCHMARK_IMAGE_ISOLATED"
        )
        evaluated = self.store.evaluate_style_benchmark(
            benchmark["run"]["id"],
            style_match_score=88,
            off_topic_rate=0.1,
            favorite_rate=0.45,
            notes="五首小样符合水墨留白基线。",
            actor={"id": "art-01", "role": "art_director"},
        )
        self.assertEqual(evaluated["status"], "passed")
        self.assertEqual(evaluated["metrics"]["sample_count"], 20)
        published_style = self.store.publish_style_pack_version(
            style_v2["id"], actor={"id": "art-01", "role": "art_director"}
        )
        self.assertEqual(published_style["status"], "active")
        current_style = self.store.published_style_pack(
            DEFAULT_PROJECT_ID, "ink-whitespace"
        )
        self.assertEqual(current_style["id"], style_v2["id"])

        direction_id = approved_directions["jing-ye-si"]
        batch = self.store.create_batch(
            DEFAULT_PROJECT_ID,
            ["jing-ye-si"],
            direction_ids=[direction_id],
            style_id="ink-whitespace",
            aspect_ratio="portrait",
            count_per_direction=1,
            provider="demo",
            model="demo-renderer",
            unit_cost=0,
            actor={"id": "producer-01", "role": "producer"},
        )
        self.assertEqual(batch["style_version_id"], style_v2["id"])
        task = self.store.tasks(batch_id=batch["id"])[0]
        self.assertEqual(task["prompt"]["style"]["version_id"], style_v2["id"])
        self.assertEqual(
            task["prompt"]["requirement"]["instruction_id"], published["id"]
        )
        compiled = task["prompt"]["compiled"]
        self.assertEqual(compiled["template_version"], "demo-six-segment-v3")
        self.assertEqual(len(compiled["hash"]), 64)
        self.assertEqual(compiled["source_refs"]["style_version_id"], style_v2["id"])
        self.assertEqual(compiled["source_refs"]["instruction_version_id"], published["id"])
        self.assertEqual(compiled["segments"]["style"]["version_id"], style_v2["id"])
        self.assertEqual(compiled["segments"]["instruction"]["id"], published["id"])

        disposable = self.store.create_instruction_version(
            DEFAULT_PROJECT_ID,
            name="不会发布的试验草稿",
            content=content,
            actor=self.actor,
        )
        retired = self.store.retire_instruction_draft(
            disposable["id"],
            reason="试验规则未通过内容评审",
            actor=self.actor,
        )
        self.assertEqual(retired["status"], "retired")
        with self.assertRaises(WorkflowError) as published_lock:
            self.store.retire_instruction_draft(
                published["id"],
                reason="不允许直接作废发布版本",
                actor=self.actor,
            )
        self.assertEqual(
            published_lock.exception.code, "PUBLISHED_INSTRUCTION_LOCKED"
        )

    def test_requirement_direction_approval_reaches_ready_for_production(self):
        result = self.store.generate_requirements(
            DEFAULT_PROJECT_ID,
            ["jing-ye-si", "jiang-xue"],
            actor=self.actor,
        )
        self.assertEqual(result["succeeded"], 2)
        self.assertEqual(result["failed"], 0)

        requirements = {
            item["poem_id"]: item for item in self.store.requirements()
        }
        jing_requirement = requirements["jing-ye-si"]
        self.assertEqual(jing_requirement["status"], "in_review")
        self.assertIn("月光", jing_requirement["content"]["core_imagery"])
        self.assertTrue(jing_requirement["content"]["evidence"])

        approved = self.store.decide_requirement(
            jing_requirement["id"],
            "approve",
            actor=self.actor,
        )
        self.assertEqual(approved["status"], "approved")

        direction_result = self.store.generate_directions(
            DEFAULT_PROJECT_ID,
            ["jing-ye-si"],
            actor={"id": "art-director-01", "role": "art_director"},
        )
        self.assertEqual(direction_result["succeeded"], 1)
        directions = [
            item
            for item in self.store.directions()
            if item["poem_id"] == "jing-ye-si"
        ]
        self.assertEqual({item["type"] for item in directions}, {
            "narrative",
            "atmospheric",
            "symbolic",
        })
        self.assertTrue(all(item["status"] == "in_review" for item in directions))

        selected = self.store.decide_direction(
            directions[0]["id"],
            "approve",
            actor={"id": "art-director-01", "role": "art_director"},
        )
        self.assertEqual(selected["status"], "approved")
        poem = next(
            item
            for item in self.store.list_poems()["items"]
            if item["id"] == "jing-ye-si"
        )
        self.assertEqual(poem["status"], "ready_for_production")
        self.assertEqual(poem["approved_direction_count"], 1)

    def test_direction_generation_requires_approved_requirement(self):
        # Bulk operations report item-level gate failures rather than rolling
        # back unrelated poems.
        result = self.store.generate_directions(
            DEFAULT_PROJECT_ID,
            ["jing-ye-si"],
            actor={"id": "art-gate", "role": "art_director"},
        )
        self.assertEqual(result["succeeded"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(
            result["results"][0]["code"], "APPROVED_REQUIREMENT_REQUIRED"
        )

    def test_direction_regeneration_preserves_locked_fields_and_stops_after_production(self):
        generated = self.store.generate_requirements(
            DEFAULT_PROJECT_ID,
            ["chun-xiao"],
            actor=self.actor,
        )
        self.store.decide_requirement(
            generated["results"][0]["requirement_id"],
            "approve",
            actor=self.actor,
        )
        art_actor = {"id": "art-regenerate", "role": "art_director"}
        first = self.store.generate_directions(
            DEFAULT_PROJECT_ID,
            ["chun-xiao"],
            actor=art_actor,
        )
        narrative_id = first["results"][0]["direction_ids"][0]
        revised = self.store.revise_direction(
            narrative_id,
            {
                "subject": "锁定为雨后庭院，不出现人物",
                "locked_fields": ["subject"],
            },
            actor=art_actor,
        )
        regenerated = self.store.generate_directions(
            DEFAULT_PROJECT_ID,
            ["chun-xiao"],
            preserve_locked=True,
            actor=art_actor,
        )
        self.assertEqual(regenerated["succeeded"], 1)
        self.assertEqual(
            regenerated["results"][0]["preserved_fields"]["narrative"],
            ["subject"],
        )
        current = [
            item
            for item in self.store.directions()
            if item["poem_id"] == "chun-xiao" and item["type"] == "narrative"
        ][0]
        self.assertEqual(current["version"], 3)
        self.assertEqual(current["content"]["subject"], revised["content"]["subject"])
        self.assertEqual(current["content"]["locked_fields"], ["subject"])

        self.store.decide_direction(current["id"], "approve", actor=art_actor)
        self.store.create_batch(
            DEFAULT_PROJECT_ID,
            ["chun-xiao"],
            direction_ids=[current["id"]],
            style_id="ink-whitespace",
            aspect_ratio="portrait",
            count_per_direction=1,
            provider="demo",
            model="demo-renderer",
            unit_cost=0,
            actor={"id": "producer-lock", "role": "producer"},
        )
        blocked = self.store.generate_directions(
            DEFAULT_PROJECT_ID,
            ["chun-xiao"],
            actor=art_actor,
        )
        self.assertEqual(blocked["failed"], 1)
        self.assertEqual(blocked["results"][0]["code"], "DIRECTION_IN_PRODUCTION")

        with self.assertRaises(WorkflowError) as forbidden:
            self.store.generate_directions(
                DEFAULT_PROJECT_ID,
                ["chun-xiao"],
                actor={"id": "content-only", "role": "content_editor"},
            )
        self.assertEqual(forbidden.exception.code, "ROLE_FORBIDDEN")

    def test_direction_revision_copy_disable_and_production_lock(self):
        direction_id = self.ready_poem("jing-ye-si")
        with self.assertRaises(WorkflowError) as forbidden:
            self.store.revise_direction(
                direction_id,
                {"title": "无权限修订"},
                actor={"id": "content-only", "role": "content_editor"},
            )
        self.assertEqual(forbidden.exception.code, "ROLE_FORBIDDEN")

        revised = self.store.revise_direction(
            direction_id,
            {
                "title": "月下乡思 · 修订构图",
                "locked_fields": ["subject"],
                "art_director_note": "冻结主体，只调整构图层次。",
            },
            actor={"id": "art-versioner", "role": "art_director"},
        )
        self.assertEqual(revised["version"], 2)
        self.assertEqual(revised["status"], "in_review")
        self.assertEqual(revised["content"]["title"], "月下乡思 · 修订构图")
        history = self.store.directions(current_only=False)
        original = next(item for item in history if item["id"] == direction_id)
        self.assertEqual(original["is_current"], 0)

        with self.assertRaises(WorkflowError) as locked:
            self.store.revise_direction(
                revised["id"],
                {"subject": "试图覆盖已锁定主体"},
                actor={"id": "art-versioner", "role": "art_director"},
            )
        self.assertEqual(locked.exception.code, "DIRECTION_FIELD_LOCKED")

        copied = self.store.copy_direction(
            revised["id"], actor={"id": "art-versioner", "role": "art_director"}
        )
        self.assertEqual(copied["version"], 3)
        self.assertEqual(copied["content"], revised["content"])
        disabled = self.store.disable_direction(
            copied["id"],
            reason="该构图方向不再适用于当前系列",
            actor={"id": "art-versioner", "role": "art_director"},
        )
        self.assertEqual(disabled["status"], "disabled")
        self.assertEqual(
            next(
                item
                for item in self.store.list_poems()["items"]
                if item["id"] == "jing-ye-si"
            )["status"],
            "direction_review",
        )

        frozen_direction_id = self.ready_poem("jiang-xue")
        self.store.create_batch(
            DEFAULT_PROJECT_ID,
            ["jiang-xue"],
            direction_ids=[frozen_direction_id],
            style_id="ink-whitespace",
            aspect_ratio="portrait",
            count_per_direction=1,
            provider="demo",
            model="demo-renderer",
            unit_cost=0,
            actor={"id": "producer-lock", "role": "producer"},
        )
        with self.assertRaises(WorkflowError) as production_lock:
            self.store.revise_direction(
                frozen_direction_id,
                {"title": "生产后不应被覆盖"},
                actor={"id": "art-versioner", "role": "art_director"},
            )
        self.assertEqual(
            production_lock.exception.code, "DIRECTION_LOCKED_BY_PRODUCTION"
        )

    def test_rejection_requires_reason_and_returns_poem_to_draft(self):
        result = self.store.generate_requirements(
            DEFAULT_PROJECT_ID,
            ["jiang-xue"],
            actor=self.actor,
        )
        requirement_id = result["results"][0]["requirement_id"]

        with self.assertRaises(WorkflowError) as context:
            self.store.decide_requirement(
                requirement_id,
                "reject",
                actor=self.actor,
            )
        self.assertEqual(context.exception.code, "REASON_REQUIRED")

        rejected = self.store.decide_requirement(
            requirement_id,
            "reject",
            reason="历史风险字段过于笼统",
            actor=self.actor,
        )
        self.assertEqual(rejected["status"], "rejected")
        poem = next(
            item
            for item in self.store.list_poems()["items"]
            if item["id"] == "jiang-xue"
        )
        self.assertEqual(poem["status"], "requirement_draft")

    def test_requirement_revision_is_versioned_and_audited(self):
        result = self.store.generate_requirements(
            DEFAULT_PROJECT_ID,
            ["chun-xiao"],
            actor=self.actor,
        )
        first_id = result["results"][0]["requirement_id"]
        revised = self.store.revise_requirement(
            first_id,
            {
                "composition": "庭院远景，人物不出现",
                "editor_note": "突出雨后落花，不要儿童角色",
                "locked_fields": ["composition", "editor_note"],
            },
            actor=self.actor,
        )

        self.assertNotEqual(revised["id"], first_id)
        self.assertEqual(revised["version"], 2)
        self.assertEqual(revised["content"]["composition"], "庭院远景，人物不出现")
        current = [
            item
            for item in self.store.requirements()
            if item["poem_id"] == "chun-xiao"
        ]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["id"], revised["id"])
        all_versions = [
            item
            for item in self.store.requirements(current_only=False)
            if item["poem_id"] == "chun-xiao"
        ]
        self.assertEqual(len(all_versions), 2)

        audit = self.store.audit_events(
            target_type="requirement",
            target_id=revised["id"],
        )
        self.assertEqual(audit[0]["action"], "requirement.revised")
        self.assertEqual(audit[0]["actor_id"], "editor-01")
        self.assertEqual(audit[0]["after"]["version"], 2)

        regenerated = self.store.generate_requirements(
            DEFAULT_PROJECT_ID,
            ["chun-xiao"],
            preserve_locked=True,
            actor=self.actor,
        )
        self.assertEqual(
            regenerated["results"][0]["preserved_fields"],
            ["composition", "editor_note"],
        )
        latest = next(
            item
            for item in self.store.requirements()
            if item["poem_id"] == "chun-xiao"
        )
        self.assertEqual(latest["version"], 3)
        self.assertEqual(latest["content"]["composition"], "庭院远景，人物不出现")
        self.assertEqual(
            latest["content"]["editor_note"],
            "突出雨后落花，不要儿童角色",
        )

    def test_bulk_requirement_decisions_report_item_level_results(self):
        generated = self.store.generate_requirements(
            DEFAULT_PROJECT_ID,
            ["lu-zhai", "feng-qiao-ye-bo"],
            actor=self.actor,
        )
        ids = [item["requirement_id"] for item in generated["results"]]
        result = self.store.bulk_decide_requirements(
            [*ids, "req_missing"],
            "approve",
            actor=self.actor,
        )
        self.assertEqual(result["succeeded"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["results"][-1]["code"], "REQUIREMENT_NOT_FOUND")
        with self.assertRaises(WorkflowError) as forbidden:
            self.store.decide_requirement(
                ids[0],
                "approve",
                actor={"id": "art-only", "role": "art_director"},
            )
        self.assertEqual(forbidden.exception.code, "ROLE_FORBIDDEN")

    def test_bulk_direction_decisions_report_item_level_results_and_audit_each_item(self):
        generated = self.store.generate_requirements(
            DEFAULT_PROJECT_ID,
            ["lu-zhai", "feng-qiao-ye-bo"],
            actor=self.actor,
        )
        self.store.bulk_decide_requirements(
            [item["requirement_id"] for item in generated["results"]],
            "approve",
            actor=self.actor,
        )
        directions = self.store.generate_directions(
            DEFAULT_PROJECT_ID,
            ["lu-zhai", "feng-qiao-ye-bo"],
            actor={"id": "art-bulk", "role": "art_director"},
        )
        ids = [
            item["direction_ids"][0]
            for item in directions["results"]
        ]
        result = self.store.bulk_decide_directions(
            [*ids, "dir_missing"],
            "approve",
            actor={"id": "art-bulk", "role": "art_director"},
        )
        self.assertEqual(result["succeeded"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["results"][-1]["code"], "DIRECTION_NOT_FOUND")
        audit = [
            item
            for item in self.store.audit_events(target_type="direction", limit=20)
            if item["action"] == "direction.approved"
        ]
        self.assertEqual(
            {item["target_id"] for item in audit if item["target_id"] in ids},
            set(ids),
        )
        forbidden = self.store.bulk_decide_directions(
            [directions["results"][0]["direction_ids"][1]],
            "approve",
            actor={"id": "content-only", "role": "content_editor"},
        )
        self.assertEqual(forbidden["failed"], 1)
        self.assertEqual(forbidden["results"][0]["code"], "ROLE_FORBIDDEN")

    def test_state_persists_when_store_is_reopened(self):
        result = self.store.generate_requirements(
            DEFAULT_PROJECT_ID,
            ["lu-zhai"],
            actor=self.actor,
        )
        requirement_id = result["results"][0]["requirement_id"]

        reopened = SopStore(
            self.database_path,
            ROOT / "data" / "poems.json",
            ROOT / "data" / "styles.json",
        )
        requirement = next(
            item
            for item in reopened.requirements()
            if item["poem_id"] == "lu-zhai"
        )
        self.assertEqual(requirement["id"], requirement_id)
        self.assertEqual(reopened.summary()["todos"]["requirement_review"], 1)

    def test_poem_import_previews_conflicts_and_commits_atomically(self):
        records = [
            {
                "id": "wang-lu-shan-pu-bu",
                "title": "望庐山瀑布",
                "author": "李白",
                "dynasty": "唐",
                "lines": [
                    "日照香炉生紫烟",
                    "遥看瀑布挂前川",
                    "飞流直下三千尺",
                    "疑是银河落九天",
                ],
                "theme": "山水",
                "mood": "壮阔、明亮",
                "imagery": ["香炉峰", "瀑布", "银河"],
                "source": "项目自有公版整理",
            },
            {
                "id": "zao-fa-bai-di-cheng",
                "title": "早发白帝城",
                "author": "李白",
                "lines": ["朝辞白帝彩云间", "千里江陵一日还"],
            },
        ]
        preview = self.store.preview_poem_import(DEFAULT_PROJECT_ID, records)
        self.assertTrue(preview["can_commit"])
        self.assertEqual(preview["counts"]["new"], 2)
        self.assertEqual(preview["counts"]["warnings"], 1)

        committed = self.store.import_poems(
            DEFAULT_PROJECT_ID,
            records,
            actor=self.actor,
        )
        self.assertEqual(committed["imported"], 2)
        self.assertEqual(self.store.summary()["total_poems"], 14)
        imported = next(
            item
            for item in self.store.list_poems()["items"]
            if item["id"] == "wang-lu-shan-pu-bu"
        )
        self.assertEqual(imported["status"], "content_review")
        approved_content = self.store.approve_content(
            "wang-lu-shan-pu-bu",
            actor=self.actor,
        )
        self.assertEqual(approved_content["status"], "requirement_draft")
        with self.assertRaises(WorkflowError) as source_context:
            self.store.approve_content(
                "zao-fa-bai-di-cheng",
                actor=self.actor,
            )
        self.assertEqual(source_context.exception.code, "SOURCE_REQUIRED")

        repeated = self.store.preview_poem_import(DEFAULT_PROJECT_ID, records)
        self.assertTrue(repeated["can_commit"])
        self.assertEqual(repeated["counts"]["unchanged"], 2)
        repeated_commit = self.store.import_poems(
            DEFAULT_PROJECT_ID,
            records,
            actor=self.actor,
        )
        self.assertEqual(repeated_commit["imported"], 0)
        self.assertEqual(repeated_commit["unchanged"], 2)

        conflict_records = [
            {
                **records[0],
                "lines": ["被错误修改的正文"],
            },
            {
                "id": "bad id",
                "title": "无效记录",
                "author": "佚名",
                "lines": ["正文"],
                "source": "测试",
            },
        ]
        conflict = self.store.preview_poem_import(
            DEFAULT_PROJECT_ID, conflict_records
        )
        self.assertFalse(conflict["can_commit"])
        self.assertEqual(conflict["counts"]["conflict"], 1)
        self.assertEqual(conflict["counts"]["invalid"], 1)
        with self.assertRaises(WorkflowError) as context:
            self.store.import_poems(
                DEFAULT_PROJECT_ID,
                conflict_records,
                actor=self.actor,
            )
        self.assertEqual(context.exception.code, "IMPORT_BLOCKED")
        self.assertEqual(self.store.summary()["total_poems"], 14)

    def test_batch_estimate_execution_attempts_and_partial_failure(self):
        direction_id = self.ready_poem()
        estimate = self.store.estimate_batch(
            DEFAULT_PROJECT_ID,
            ["jing-ye-si"],
            direction_ids=[direction_id],
            style_id="light-gongbi",
            aspect_ratio="portrait",
            count_per_direction=2,
            provider="demo",
            model="demo-renderer",
            unit_cost=0,
        )
        self.assertEqual(estimate["poem_count"], 1)
        self.assertEqual(estimate["direction_count"], 1)
        self.assertEqual(estimate["task_count"], 2)
        self.assertTrue(estimate["can_start"])

        batch = self.store.create_batch(
            DEFAULT_PROJECT_ID,
            ["jing-ye-si"],
            direction_ids=[direction_id],
            name="静夜思首轮",
            style_id="light-gongbi",
            aspect_ratio="portrait",
            count_per_direction=2,
            provider="demo",
            model="demo-renderer",
            unit_cost=0,
            actor=self.actor,
        )
        tasks = self.store.tasks(batch_id=batch["id"])
        self.assertEqual(len(tasks), 2)
        self.assertTrue(all(task["status"] == "pending" for task in tasks))
        self.assertEqual(len({task["idempotency_key"] for task in tasks}), 2)

        started = self.store.start_batch(batch["id"], actor=self.actor)
        self.assertEqual(started["status"], "queued")
        first = self.store.claim_next_task(batch["id"])
        self.assertEqual(first["status"], "running")
        self.store.complete_task(
            first["id"],
            first["attempt_id"],
            output_image_id="image-001",
            actual_cost=0,
            duration_ms=120,
        )
        second = self.store.claim_next_task(batch["id"])
        self.store.fail_task(
            second["id"],
            second["attempt_id"],
            error_code="BAD_REQUEST",
            error_message="测试不可重试错误",
            retryable=False,
            duration_ms=20,
        )
        final_state = self.store.execution_state(batch["id"])
        self.assertEqual(final_state["batch"]["status"], "partially_failed")
        self.assertEqual(final_state["counts"]["succeeded"], 1)
        self.assertEqual(final_state["counts"]["failed"], 1)
        self.assertEqual(len(self.store.attempts(first["id"])), 1)
        poem = next(
            item
            for item in self.store.list_poems()["items"]
            if item["id"] == "jing-ye-si"
        )
        self.assertEqual(poem["status"], "candidate_review")

    def test_production_report_aggregates_throughput_errors_and_anomalies(self):
        direction_id = self.ready_poem("jiang-xue")
        batch = self.store.create_batch(
            DEFAULT_PROJECT_ID,
            ["jiang-xue"],
            direction_ids=[direction_id],
            style_id="ink-whitespace",
            aspect_ratio="portrait",
            count_per_direction=1,
            provider="demo",
            model="demo-renderer",
            unit_cost=0,
            actor={"id": "producer-report", "role": "producer"},
        )
        self.store.start_batch(batch["id"], actor=self.actor)
        task = self.store.claim_next_task(batch["id"])
        self.store.fail_task(
            task["id"],
            task["attempt_id"],
            error_code="INVALID_REQUEST",
            error_message="测试参数错误",
            retryable=False,
            duration_ms=8,
        )

        report = self.store.production_report(DEFAULT_PROJECT_ID, days=7)
        self.assertEqual(report["tasks"]["failed"], 1)
        self.assertEqual(report["tasks"]["success_rate"], 0)
        self.assertEqual(report["error_breakdown"][0]["code"], "INVALID_REQUEST")
        anomaly_ids = {item["id"] for item in report["anomalies"]}
        self.assertIn("failed_tasks", anomaly_ids)
        self.assertEqual(len(report["daily"]), 7)

    def test_provider_circuit_pauses_all_matching_active_batches(self):
        batch_ids = []
        for poem_id in ("jing-ye-si", "jiang-xue"):
            direction_id = self.ready_poem(poem_id)
            batch = self.store.create_batch(
                DEFAULT_PROJECT_ID,
                [poem_id],
                direction_ids=[direction_id],
                style_id="ink-whitespace",
                aspect_ratio="portrait",
                count_per_direction=1,
                provider="demo",
                model="demo-renderer",
                unit_cost=0,
                actor={"id": "producer-circuit", "role": "producer"},
            )
            self.store.start_batch(batch["id"], actor=self.actor)
            batch_ids.append(batch["id"])

        paused = self.store.pause_provider_batches(
            "demo",
            reason="连续网络错误触发熔断",
            actor={"id": "provider-circuit", "role": "system"},
        )
        self.assertEqual(set(paused), set(batch_ids))
        self.assertTrue(all(self.store.batch(batch_id)["status"] == "paused" for batch_id in batch_ids))
        paused_poems = self.store.list_poems(status="paused")
        self.assertEqual(paused_poems["total"], 2)
        actions = {event["action"] for event in self.store.audit_events(limit=50)}
        self.assertIn("batch.provider_circuit_paused", actions)

    def test_batch_budget_gate_pause_resume_and_crash_recovery(self):
        direction_id = self.ready_poem("jiang-xue")
        self.store.set_budget_policy(
            DEFAULT_PROJECT_ID,
            hard_limit=0.01,
            actor=self.actor,
        )
        batch = self.store.create_batch(
            DEFAULT_PROJECT_ID,
            ["jiang-xue"],
            direction_ids=[direction_id],
            style_id="ink-whitespace",
            aspect_ratio="portrait",
            count_per_direction=2,
            provider="openai",
            model="test-image-model",
            unit_cost=0.06,
            actor=self.actor,
        )
        blocked = self.store.start_batch(batch["id"], actor=self.actor)
        self.assertEqual(blocked["status"], "budget_blocked")

        self.store.set_budget_policy(
            DEFAULT_PROJECT_ID,
            hard_limit=1,
            actor=self.actor,
        )
        started = self.store.start_batch(batch["id"], actor=self.actor)
        self.assertEqual(started["status"], "queued")
        claimed = self.store.claim_next_task(batch["id"])
        paused = self.store.pause_batch(batch["id"], actor=self.actor)
        self.assertEqual(paused["status"], "paused")
        self.assertIsNone(self.store.claim_next_task(batch["id"]))
        self.store.complete_task(
            claimed["id"],
            claimed["attempt_id"],
            output_image_id="image-before-pause",
            actual_cost=0.06,
            duration_ms=40,
        )
        resumed = self.store.start_batch(batch["id"], actor=self.actor)
        self.assertEqual(resumed["status"], "queued")
        claimed_after_resume = self.store.claim_next_task(batch["id"])
        self.assertIsNotNone(claimed_after_resume)

        reopened = SopStore(
            self.database_path,
            ROOT / "data" / "poems.json",
            ROOT / "data" / "styles.json",
        )
        state = reopened.execution_state(batch["id"])
        self.assertEqual(state["batch"]["status"], "paused")
        self.assertEqual(state["counts"]["succeeded"], 1)
        self.assertEqual(state["counts"]["blocked"], 1)
        blocked_task = next(
            task for task in reopened.tasks(batch_id=batch["id"])
            if task["status"] == "blocked"
        )
        self.assertEqual(blocked_task["last_error_code"], "OUTCOME_UNKNOWN")
        with self.assertRaises(WorkflowError) as context:
            reopened.retry_failed_tasks(batch["id"], actor=self.actor)
        self.assertEqual(
            context.exception.code, "UNKNOWN_OUTCOME_CONFIRMATION_REQUIRED"
        )
        retried = reopened.retry_failed_tasks(
            batch["id"],
            confirm_unknown=True,
            actor=self.actor,
        )
        self.assertEqual(retried["status"], "queued")

    def test_retryable_failure_uses_a_new_attempt_and_converges(self):
        direction_id = self.ready_poem("feng-qiao-ye-bo")
        batch = self.store.create_batch(
            DEFAULT_PROJECT_ID,
            ["feng-qiao-ye-bo"],
            direction_ids=[direction_id],
            style_id="moonlit-blue-green",
            aspect_ratio="portrait",
            count_per_direction=1,
            provider="openai",
            model="test-image-model",
            unit_cost=0.06,
            actor=self.actor,
        )
        self.store.start_batch(batch["id"], actor=self.actor)
        first = self.store.claim_next_task(batch["id"])
        waiting = self.store.fail_task(
            first["id"],
            first["attempt_id"],
            error_code="RATE_LIMITED",
            error_message="HTTP 429",
            retryable=True,
            duration_ms=15,
        )
        self.assertEqual(waiting["status"], "retry_waiting")
        self.assertIsNone(self.store.claim_next_task(batch["id"]))

        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE generation_tasks SET retry_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", first["id"]),
            )
        second = self.store.claim_next_task(batch["id"])
        self.assertEqual(second["attempt_number"], 2)
        self.store.complete_task(
            second["id"],
            second["attempt_id"],
            output_image_id="image-after-retry",
            actual_cost=0.06,
            duration_ms=30,
        )
        self.assertEqual(self.store.batch(batch["id"])["status"], "completed")
        attempts = self.store.attempts(first["id"])
        self.assertEqual([attempt["status"] for attempt in attempts], ["failed", "succeeded"])

    def test_qc_duplicate_override_review_and_rework_are_audited(self):
        direction_id = self.ready_poem("lu-zhai")
        batch = self.store.create_batch(
            DEFAULT_PROJECT_ID,
            ["lu-zhai"],
            direction_ids=[direction_id],
            style_id="ink-whitespace",
            aspect_ratio="portrait",
            count_per_direction=2,
            provider="demo",
            model="demo-renderer",
            unit_cost=0,
            actor=self.actor,
        )
        self.store.start_batch(batch["id"], actor=self.actor)
        path = Path(self.temp_dir.name) / "candidate.svg"
        path.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1536" viewBox="0 0 1024 1536"><rect width="1024" height="1536" fill="#eee8dc"/><circle cx="530" cy="420" r="140" fill="#8ca6a0"/></svg>',
            encoding="utf-8",
        )
        first = self.store.claim_next_task(batch["id"])
        inspection = self.reviewed_inspection(path, first)
        first_record = {
            "id": "a" * 32,
            "url": "/generated/first.svg",
            "prompt": "first prompt",
            "created_at": "2026-07-17T00:00:00+00:00",
        }
        registered = self.store.register_production_image(
            first_record, first, inspection
        )
        self.assertEqual(registered["status"], "review_ready")
        self.store.complete_task(
            first["id"], first["attempt_id"],
            output_image_id=first_record["id"], actual_cost=0, duration_ms=10,
        )

        second = self.store.claim_next_task(batch["id"])
        second_record = {
            "id": "b" * 32,
            "url": "/generated/second.svg",
            "prompt": "second prompt",
            "created_at": "2026-07-17T00:00:01+00:00",
        }
        duplicate = self.store.register_production_image(
            second_record, second, inspection
        )
        self.assertEqual(duplicate["status"], "qc_blocked")
        self.assertEqual(duplicate["qc"]["duplicate_of"], first_record["id"])
        self.assertIn("near_duplicate", duplicate["qc"]["hard_failures"])
        self.store.complete_task(
            second["id"], second["attempt_id"],
            output_image_id=second_record["id"], actual_cost=0, duration_ms=10,
        )

        with self.assertRaises(WorkflowError) as blocked:
            self.store.decide_image(
                second_record["id"], "selected",
                reason_tags=["构图最佳"], actor=self.actor,
            )
        self.assertEqual(blocked.exception.code, "QC_OVERRIDE_REQUIRED")
        overridden = self.store.override_qc(
            second_record["id"], "pass",
            reason="人工核对后确认两张主体动作不同，可保留。",
            actor=self.actor,
        )
        self.assertEqual(overridden["status"], "review_ready")
        selected = self.store.decide_image(
            second_record["id"], "selected",
            reason_tags=["构图最佳", "诗意准确"],
            note="保留为返工母图。", actor=self.actor,
        )
        self.assertEqual(selected["status"], "selected")
        order = self.store.create_rework_order(
            second_record["id"],
            preserve=["主体位置", "月光层次"],
            change=["减少背景树木"],
            avoid=["新增文字"],
            note="收敛背景。", actor=self.actor,
        )
        self.assertEqual(order["status"], "draft")
        self.assertEqual(order["change"], ["减少背景树木"])
        actions = {event["action"] for event in self.store.audit_events(limit=50)}
        self.assertIn("image.qc_overridden", actions)
        self.assertIn("image.selected", actions)
        self.assertIn("rework.created", actions)

    def test_dual_final_approval_and_non_overwriting_manifest_export(self):
        image, source_path = self.final_candidate()
        content_result = self.store.finalize_image(
            image["id"],
            reviewer_type="content",
            decision="approved",
            reason="诗意与正文核对无误。",
            actor={"id": "content-final", "role": "content_editor"},
        )
        self.assertFalse(content_result["locked"])
        self.assertIsNone(content_result["final_asset"])

        with self.assertRaises(WorkflowError) as role_error:
            self.store.finalize_image(
                image["id"],
                reviewer_type="art",
                decision="approved",
                actor={"id": "wrong-role", "role": "content_editor"},
            )
        self.assertEqual(role_error.exception.code, "FINAL_ROLE_REQUIRED")

        art_result = self.store.finalize_image(
            image["id"],
            reviewer_type="art",
            decision="approved",
            reason="构图、光色与系列规范一致。",
            actor={"id": "art-final", "role": "art_director"},
        )
        self.assertTrue(art_result["locked"])
        asset = art_result["final_asset"]
        self.assertEqual(asset["version"], 1)
        self.assertEqual(asset["checksum"], inspect_image(source_path, "portrait")["checksum"])
        self.assertEqual(self.store.list_poems(status="approved")["total"], 1)

        estimate = self.store.export_estimate(DEFAULT_PROJECT_ID)
        self.assertTrue(estimate["can_export"])
        self.assertEqual(estimate["asset_count"], 1)
        export_root = Path(self.temp_dir.name) / "exports"
        first = self.store.create_export_package(
            DEFAULT_PROJECT_ID,
            export_root,
            actor={"id": "producer-final", "role": "producer"},
        )
        second = self.store.create_export_package(
            DEFAULT_PROJECT_ID,
            export_root,
            actor={"id": "producer-final", "role": "producer"},
        )
        self.assertEqual(first["status"], "completed")
        self.assertNotEqual(first["output_path"], second["output_path"])
        self.assertTrue(Path(first["manifest_path"]).is_file())
        self.assertTrue(Path(second["manifest_path"]).is_file())
        manifest = json.loads(
            Path(first["manifest_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["asset_count"], 1)
        manifest_asset = manifest["assets"][0]
        self.assertEqual(manifest_asset["poem"]["id"], "shan-ju-qiu-ming")
        self.assertEqual(manifest_asset["source"]["image_id"], image["id"])
        self.assertEqual(manifest_asset["source"]["style"]["id"], "ink-whitespace")
        self.assertEqual(manifest_asset["source"]["style"]["version"], 1)
        self.assertTrue(manifest_asset["source"]["style"]["version_id"])
        self.assertEqual(
            manifest_asset["source"]["prompt_template_version"],
            "demo-six-segment-v3",
        )
        self.assertEqual(len(manifest_asset["source"]["prompt_hash"]), 64)
        self.assertEqual(
            manifest_asset["source"]["prompt_segments"]["instruction"]["id"],
            "instruction_global_v1",
        )
        self.assertEqual(manifest_asset["approvals"]["content"]["actor_id"], "content-final")
        self.assertEqual(manifest_asset["approvals"]["art"]["actor_id"], "art-final")
        exported_file = Path(first["output_path"]) / manifest_asset["file"]["path"]
        self.assertTrue(exported_file.is_file())
        self.assertEqual(
            hashlib.sha256(exported_file.read_bytes()).hexdigest(),
            manifest_asset["file"]["checksum_sha256"],
        )
        self.assertEqual(self.store.list_poems(status="exported")["total"], 1)

    def test_historical_score_blocks_final_asset_until_explicit_qc_override(self):
        image, _ = self.final_candidate("shan-ju-qiu-ming", "e" * 32)
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT id, scores_json FROM qc_results WHERE image_id = ?",
                (image["id"],),
            ).fetchone()
            scores = json.loads(row[1])
            scores["historical_plausibility"] = 55
            connection.execute(
                "UPDATE qc_results SET scores_json = ? WHERE id = ?",
                (json.dumps(scores, ensure_ascii=False), row[0]),
            )
        self.store.finalize_image(
            image["id"],
            reviewer_type="content",
            decision="approved",
            reason="内容无误，但历史细节需覆盖留痕。",
            actor={"id": "content-final", "role": "content_editor"},
        )
        with self.assertRaises(WorkflowError) as blocked:
            self.store.finalize_image(
                image["id"],
                reviewer_type="art",
                decision="approved",
                reason="美术终审。",
                actor={"id": "art-final", "role": "art_director"},
            )
        self.assertEqual(blocked.exception.code, "HISTORICAL_QC_BLOCKS_FINALIZATION")
        self.store.override_qc(
            image["id"],
            "pass",
            reason="内容编辑核对史料后确认该处可接受。",
            actor={"id": "producer-final", "role": "producer"},
        )
        result = self.store.finalize_image(
            image["id"],
            reviewer_type="art",
            decision="approved",
            reason="美术终审通过。",
            actor={"id": "art-final", "role": "art_director"},
        )
        self.assertTrue(result["locked"])


if __name__ == "__main__":
    unittest.main()
