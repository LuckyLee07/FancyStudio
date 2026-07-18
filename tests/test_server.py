import base64
import contextlib
import http.client
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

import server


class TangPoemStudioTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        data_dir = Path(self.temp_dir.name) / "data"
        generated_dir = data_dir / "generated"
        generated_dir.mkdir(parents=True)
        shutil.copy(server.ROOT / "data" / "poems.json", data_dir / "poems.json")
        shutil.copy(server.ROOT / "data" / "styles.json", data_dir / "styles.json")
        shutil.copy(
            server.ROOT / "data" / "art_bible.json", data_dir / "art_bible.json"
        )
        shutil.copy(
            server.ROOT / "data" / "benchmark_poems.json",
            data_dir / "benchmark_poems.json",
        )
        shutil.copy(
            server.ROOT / "data" / "qc_policy.json",
            data_dir / "qc_policy.json",
        )

        server.DATA_DIR = data_dir
        server.GENERATED_DIR = generated_dir
        server.STATE_FILE = data_dir / "state.json"
        server.POEMS_FILE = data_dir / "poems.json"
        server.STYLES_FILE = data_dir / "styles.json"
        server.SOP_STORE = None
        server.PROVIDER_CIRCUITS.reset()
        with server.BATCH_WORKERS_LOCK:
            server.BATCH_WORKERS.clear()
        os.environ["AI_PROVIDER"] = "demo"
        server.STORE = server.StudioStore()
        server.seed_demo_gallery()

        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.StudioHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        with server.BATCH_WORKERS_LOCK:
            workers = list(server.BATCH_WORKERS.values())
        for worker in workers:
            worker.join(timeout=2)
        self.temp_dir.cleanup()
        server.PROVIDER_CIRCUITS.reset()
        os.environ.pop("AI_PROVIDER", None)

    def request_json(self, path, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def request_json_error(self, path, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=5)
        error = context.exception
        return error.code, json.loads(error.read().decode("utf-8"))

    def test_health_and_bootstrap(self):
        status, health = self.request_json("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])
        self.assertEqual(health["provider"], "demo")
        self.assertEqual(health["provider_status"]["status"], "ready")
        self.assertEqual(health["provider_status"]["concurrency"], 2)

        status, bootstrap = self.request_json("/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(len(bootstrap["poems"]), 12)
        self.assertEqual(len(bootstrap["styles"]), 6)
        self.assertEqual(len(bootstrap["projects"]), 1)
        self.assertEqual(bootstrap["projects"][0]["id"], server.DEFAULT_PROJECT_ID)
        self.assertEqual(len(bootstrap["images"]), 9)

        head_request = urllib.request.Request(self.base_url + "/", method="HEAD")
        with urllib.request.urlopen(head_request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "text/html")

    def test_requirement_schema_cache_and_run_trace_endpoints(self):
        status, schema = self.request_json("/api/schemas/requirement-card")
        self.assertEqual(status, 200)
        self.assertEqual(schema["title"], "RequirementCard v1")
        self.assertIn("confidence", schema["required"])

        actor = {"id": "editor-api", "role": "content_editor"}
        status, generated = self.request_json(
            "/api/requirements/generate",
            method="POST",
            payload={
                "project_id": server.SOP_DEFAULT_PROJECT_ID,
                "poem_ids": ["jing-ye-si"],
                "actor": actor,
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(generated["succeeded"], 1)
        self.assertEqual(len(generated["results"][0]["input_hash"]), 64)

        status, runs = self.request_json(
            "/api/requirement-generation-runs?poem_id=jing-ye-si&status=succeeded"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(runs["items"]), 1)
        self.assertEqual(runs["items"][0]["schema_version"], "requirement-card/v1")
        self.assertTrue(runs["items"][0]["validation"]["valid"])

        status, detail = self.request_json("/api/poems/jing-ye-si")
        self.assertEqual(status, 200)
        self.assertEqual(detail["counts"]["requirement_generation_runs"], 1)

        status, bootstrap = self.request_json("/api/sop/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(
            bootstrap["requirement_schema"]["schema_version"],
            "requirement-card/v1",
        )
        self.assertEqual(bootstrap["requirement_generation_failures"], [])

        status, backup_result = self.request_json(
            "/api/backups",
            method="POST",
            payload={"actor": {"id": "admin-api", "role": "system_admin"}},
        )
        self.assertEqual(status, 201)
        self.assertTrue(backup_result["backup"]["valid"])
        backup_name = backup_result["backup"]["name"]
        status, verified = self.request_json(
            f"/api/backups/{backup_name}/verify",
            method="POST",
            payload={"actor": {"id": "admin-api", "role": "system_admin"}},
        )
        self.assertEqual(status, 200)
        self.assertTrue(verified["backup"]["valid"])
        _, backups = self.request_json("/api/backups")
        self.assertEqual(backups["items"][0]["name"], backup_name)

    def test_direction_schema_diversity_and_run_trace_endpoints(self):
        status, schema = self.request_json("/api/schemas/direction-proposal")
        self.assertEqual(status, 200)
        self.assertEqual(schema["title"], "DirectionProposal v1")
        self.assertIn("interpretation_layers", schema["required"])

        store = server.get_sop_store()
        editor = {"id": "editor-direction-api", "role": "content_editor"}
        art = {"id": "art-direction-api", "role": "art_director"}
        requirement = store.generate_requirements(
            server.SOP_DEFAULT_PROJECT_ID,
            ["jing-ye-si"],
            actor=editor,
        )["results"][0]
        store.decide_requirement(requirement["requirement_id"], "approve", actor=editor)

        status, generated = self.request_json(
            "/api/directions/generate",
            method="POST",
            payload={
                "project_id": server.SOP_DEFAULT_PROJECT_ID,
                "poem_ids": ["jing-ye-si"],
                "actor": art,
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(generated["succeeded"], 1)
        result = generated["results"][0]
        self.assertEqual(len(result["direction_ids"]), 3)
        self.assertGreaterEqual(result["diversity"]["minimum_axis_differences"], 2)

        status, runs = self.request_json(
            "/api/direction-generation-runs?poem_id=jing-ye-si&status=succeeded"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(runs["items"]), 1)
        self.assertEqual(runs["items"][0]["schema_version"], "direction-proposal/v1")
        self.assertTrue(runs["items"][0]["validation"]["diversity"]["valid"])

        status, detail = self.request_json("/api/poems/jing-ye-si")
        self.assertEqual(status, 200)
        self.assertEqual(detail["counts"]["direction_generation_runs"], 1)

        status, bootstrap = self.request_json("/api/sop/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(
            bootstrap["direction_schema"]["schema_version"],
            "direction-proposal/v1",
        )
        self.assertEqual(bootstrap["direction_generation_failures"], [])

    def test_request_ids_structured_logs_atomic_writes_and_path_safety(self):
        request = urllib.request.Request(
            self.base_url + "/api/reports/production?days=invalid",
            headers={"X-Request-ID": "trace-test-001"},
        )
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=5)
        error = context.exception
        payload = json.loads(error.read().decode("utf-8"))
        self.assertEqual(error.headers["X-Request-ID"], "trace-test-001")
        self.assertEqual(payload["request_id"], "trace-test-001")

        output = Path(self.temp_dir.name) / "atomic" / "asset.bin"
        server.atomic_write_bytes(output, b"first")
        server.atomic_write_bytes(output, b"second")
        self.assertEqual(output.read_bytes(), b"second")
        self.assertEqual(list(output.parent.glob(".*.tmp")), [])

        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            server.structured_log(
                "task.test",
                request_id="trace-test-001",
                batch_id="batch_safe",
                task_id="task_safe",
            )
        record = json.loads(stream.getvalue())
        self.assertEqual(record["event"], "task.test")
        self.assertEqual(record["request_id"], "trace-test-001")

        breaker = server.ProviderCircuitBreaker(threshold=2, cooldown_seconds=1)
        self.assertEqual(breaker.status("openai")["state"], "closed")
        self.assertEqual(breaker.record_failure("openai", "RATE_LIMITED")["state"], "closed")
        opened = breaker.record_failure("openai", "NETWORK_ERROR")
        self.assertEqual(opened["state"], "open")
        self.assertGreaterEqual(opened["retry_after_seconds"], 1)
        breaker.record_success("openai")
        self.assertEqual(breaker.status("openai")["state"], "closed")

        connection = http.client.HTTPConnection(
            "127.0.0.1", self.httpd.server_port, timeout=5
        )
        connection.request("GET", "/exports/../../server.py")
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 404)
        connection.close()

    def test_instruction_style_and_provider_operational_apis(self):
        status, bootstrap = self.request_json("/api/sop/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(len(bootstrap["instruction_versions"]), 1)
        self.assertEqual(len(bootstrap["style_packs"]), 6)
        self.assertEqual(len(bootstrap["style_benchmark_poems"]), 12)
        self.assertEqual(bootstrap["art_bible"]["semantic_version"], "1.0.0")
        self.assertEqual(bootstrap["provider_status"]["provider"], "demo")
        self.assertEqual(
            bootstrap["provider_status"]["visual_qc"]["status"],
            "synthetic_demo",
        )
        self.assertEqual(bootstrap["qc_policy"]["content"]["semantic_version"], "1.0.0")
        self.assertEqual(bootstrap["qc_calibration"]["target_count"], 100)
        self.assertIn("anomalies", bootstrap["production_report"])

        status, review_schema = self.request_json("/api/schemas/review-result")
        self.assertEqual(status, 200)
        self.assertEqual(review_schema["title"], "ReviewResult v1")
        status, policy_schema = self.request_json("/api/schemas/qc-policy")
        self.assertEqual(status, 200)
        self.assertEqual(policy_schema["title"], "QCPolicy v1")
        status, policies = self.request_json("/api/qc-policies")
        self.assertEqual(status, 200)
        self.assertEqual(len(policies["items"]), 1)

        status, report = self.request_json("/api/reports/production?days=14")
        self.assertEqual(status, 200)
        self.assertEqual(report["days"], 14)
        self.assertEqual(len(report["daily"]), 14)

        status, instruction_result = self.request_json(
            "/api/instructions",
            method="POST",
            payload={
                "project_id": "tang-300-production",
                "name": "API 创作规范 v2",
                "content": {
                    "audience": "教育出版团队",
                    "visual_goal": "准确、统一、可排版",
                    "composition_rules": ["主体清楚"],
                    "historical_rules": ["唐代语境"],
                    "global_avoid": ["文字", "水印"],
                },
                "actor": {"id": "content-api", "role": "content_editor"},
            },
        )
        self.assertEqual(status, 201)
        instruction_id = instruction_result["instruction"]["id"]
        status, published = self.request_json(
            f"/api/instructions/{instruction_id}/publish",
            method="POST",
            payload={"actor": {"id": "producer-api", "role": "producer"}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(published["instruction"]["status"], "published")

        status, disposable = self.request_json(
            "/api/instructions",
            method="POST",
            payload={
                "project_id": "tang-300-production",
                "name": "API 待作废草稿",
                "content": instruction_result["instruction"]["content"],
                "actor": {"id": "content-api", "role": "content_editor"},
            },
        )
        self.assertEqual(status, 201)
        status, retired = self.request_json(
            f"/api/instructions/{disposable['instruction']['id']}/retire",
            method="POST",
            payload={
                "reason": "API 版本治理测试",
                "actor": {"id": "content-api", "role": "content_editor"},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(retired["instruction"]["status"], "retired")

        status, style_result = self.request_json(
            "/api/style-packs",
            method="POST",
            payload={
                "project_id": "tang-300-production",
                "style_id": "ink-whitespace",
                "name": "水墨留白 API v2",
                "short_name": "水墨 v2",
                "description": "测试不可变风格版本",
                "semantic_version": "1.1.0",
                "release_notes": "API 风格发布门禁测试。",
                "art_bible_version_id": bootstrap["art_bible"]["id"],
                "prompt_fragment": "minimal ink wash with print safe tones",
                "palette": ["#F0EDE5", "#292E2C"],
                "settings": {
                    "background": "#F0EDE5",
                    "foreground": "#292E2C",
                    "accent": "#767B78",
                    "paper": "cool",
                },
                "applicable_topics": ["山水"],
                "visual_traits": {
                    "line": "干湿并用的墨线",
                    "texture": "冷调纸纹",
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
                "avoid": ["现代器物"],
                "risks": ["画面过空"],
                "positive_examples": ["主体虽小但焦点明确"],
                "negative_examples": ["所有诗都使用孤舟背影"],
                "actor": {"id": "art-api", "role": "art_director"},
            },
        )
        self.assertEqual(status, 201)
        style_version_id = style_result["style"]["id"]
        with self.assertRaises(urllib.error.HTTPError) as style_gate:
            self.request_json(
                f"/api/style-packs/{style_version_id}/publish",
                method="POST",
                payload={"actor": {"id": "art-api", "role": "art_director"}},
            )
        self.assertEqual(style_gate.exception.code, 409)
        style_published = json.loads(style_gate.exception.read().decode("utf-8"))
        self.assertEqual(style_published["code"], "STYLE_BENCHMARK_REQUIRED")

        status, art_schema = self.request_json("/api/schemas/art-bible")
        self.assertEqual(status, 200)
        self.assertIn("benchmark_policy", art_schema["required"])
        status, style_schema = self.request_json("/api/schemas/style-pack")
        self.assertEqual(status, 200)
        self.assertIn("semantic_version", style_schema["required"])
        status, benchmark_poems = self.request_json("/api/style-benchmark-poems")
        self.assertEqual(status, 200)
        self.assertEqual(len(benchmark_poems["items"]), 12)

        status, provider = self.request_json("/api/provider-status")
        self.assertEqual(status, 200)
        self.assertTrue(provider["capabilities"]["generation"])
        self.assertFalse(provider["capabilities"]["image_edit"])

    def test_exception_center_api_returns_actionable_owned_items(self):
        store = server.get_sop_store()
        editor = {"id": "exception-editor", "role": "content_editor"}
        art = {"id": "exception-art", "role": "art_director"}
        producer = {"id": "exception-producer", "role": "producer"}
        requirement_result = store.generate_requirements(
            server.SOP_DEFAULT_PROJECT_ID, ["jiang-xue"], actor=editor
        )
        requirement_id = requirement_result["results"][0]["requirement_id"]
        store.decide_requirement(requirement_id, "approve", actor=editor)
        direction_result = store.generate_directions(
            server.SOP_DEFAULT_PROJECT_ID, ["jiang-xue"], actor=art
        )
        direction_id = direction_result["results"][0]["direction_ids"][0]
        store.decide_direction(direction_id, "approve", actor=art)
        batch = store.create_batch(
            server.SOP_DEFAULT_PROJECT_ID,
            ["jiang-xue"],
            direction_ids=[direction_id],
            style_id="ink-whitespace",
            aspect_ratio="portrait",
            count_per_direction=1,
            provider="demo",
            model="demo-renderer",
            unit_cost=0,
            actor=producer,
        )
        store.start_batch(batch["id"], actor=producer)
        task = store.claim_next_task(batch["id"])
        store.fail_task(
            task["id"],
            task["attempt_id"],
            error_code="INVALID_REQUEST",
            error_message="测试参数错误 /Users/test/private.json sk-live-secret123",
            retryable=False,
            duration_ms=5,
        )

        query = urllib.parse.urlencode(
            {
                "kind": "failed_tasks",
                "responsible_role": "ai_operator",
                "q": "江雪",
            }
        )
        status, payload = self.request_json(f"/api/exceptions?{query}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["target_id"], task["id"])
        self.assertEqual(payload["items"][0]["responsible_role"], "ai_operator")
        self.assertTrue(payload["items"][0]["suggested_action"])
        self.assertNotIn("/Users/", payload["items"][0]["reason"])
        self.assertNotIn("sk-live", payload["items"][0]["reason"])
        self.assertIn("by_role", payload["summary"])

        with self.assertRaises(urllib.error.HTTPError) as invalid:
            self.request_json("/api/exceptions?kind=not-a-kind")
        self.assertEqual(invalid.exception.code, 400)
        error = json.loads(invalid.exception.read().decode("utf-8"))
        self.assertEqual(error["code"], "INVALID_BLOCKER_KIND")

    def test_sop_api_requirement_direction_and_audit_flow(self):
        status, bootstrap = self.request_json("/api/sop/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(bootstrap["summary"]["total_poems"], 12)
        self.assertEqual(
            bootstrap["summary"]["status_counts"]["requirement_draft"], 12
        )

        status, generated = self.request_json(
            "/api/requirements/generate",
            method="POST",
            payload={
                "project_id": server.SOP_DEFAULT_PROJECT_ID,
                "poem_ids": ["jing-ye-si"],
                "actor": {"id": "editor-api", "role": "content_editor"},
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(generated["succeeded"], 1)
        requirement_id = generated["results"][0]["requirement_id"]

        status, approved = self.request_json(
            f"/api/requirements/{requirement_id}/approve",
            method="POST",
            payload={
                "actor": {"id": "editor-api", "role": "content_editor"}
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(approved["requirement"]["status"], "approved")

        status, generated_directions = self.request_json(
            "/api/directions/generate",
            method="POST",
            payload={
                "project_id": server.SOP_DEFAULT_PROJECT_ID,
                "poem_ids": ["jing-ye-si"],
                "actor": {"id": "art-api", "role": "art_director"},
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(generated_directions["succeeded"], 1)
        self.assertEqual(len(generated_directions["results"][0]["direction_ids"]), 3)

        direction_id = generated_directions["results"][0]["direction_ids"][0]
        status, direction_approved = self.request_json(
            f"/api/directions/{direction_id}/approve",
            method="POST",
            payload={"actor": {"id": "art-api", "role": "art_director"}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(direction_approved["direction"]["status"], "approved")

        status, summary = self.request_json(
            f"/api/projects/{server.SOP_DEFAULT_PROJECT_ID}/summary"
        )
        self.assertEqual(status, 200)
        self.assertEqual(summary["todos"]["ready_for_production"], 1)

        status, poem_detail = self.request_json("/api/poems/jing-ye-si")
        self.assertEqual(status, 200)
        self.assertEqual(poem_detail["poem"]["title"], "静夜思")
        self.assertEqual(poem_detail["counts"]["requirements"], 1)
        self.assertEqual(poem_detail["counts"]["directions"], 3)

        status, audit = self.request_json("/api/audit-events?limit=20")
        self.assertEqual(status, 200)
        actions = {item["action"] for item in audit["items"]}
        self.assertIn("requirement.approved", actions)
        self.assertIn("direction.approved", actions)

        status, revised = self.request_json(
            f"/api/directions/{direction_id}/revise",
            method="POST",
            payload={
                "content": {"title": "API 修订方向"},
                "actor": {"id": "art-api", "role": "art_director"},
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(revised["direction"]["version"], 2)
        self.assertEqual(revised["direction"]["content"]["title"], "API 修订方向")
        status, copied = self.request_json(
            f"/api/directions/{revised['direction']['id']}/copy",
            method="POST",
            payload={"actor": {"id": "art-api", "role": "art_director"}},
        )
        self.assertEqual(status, 201)
        self.assertEqual(copied["direction"]["version"], 3)
        status, disabled = self.request_json(
            f"/api/directions/{copied['direction']['id']}/disable",
            method="POST",
            payload={
                "reason": "API 测试停用",
                "actor": {"id": "art-api", "role": "art_director"},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(disabled["direction"]["status"], "disabled")

        _, bulk_generated = self.request_json(
            "/api/requirements/generate",
            method="POST",
            payload={
                "project_id": server.SOP_DEFAULT_PROJECT_ID,
                "poem_ids": ["lu-zhai", "chun-xiao"],
                "actor": {"id": "editor-api", "role": "content_editor"},
            },
        )
        bulk_ids = [item["requirement_id"] for item in bulk_generated["results"]]
        status, bulk_decision = self.request_json(
            "/api/requirements/bulk-decision",
            method="POST",
            payload={
                "requirement_ids": bulk_ids,
                "decision": "approve",
                "actor": {"id": "editor-api", "role": "content_editor"},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(bulk_decision["succeeded"], 2)
        self.assertEqual(bulk_decision["failed"], 0)

        status, bulk_directions = self.request_json(
            "/api/directions/generate",
            method="POST",
            payload={
                "project_id": server.SOP_DEFAULT_PROJECT_ID,
                "poem_ids": ["lu-zhai", "chun-xiao"],
                "actor": {"id": "art-api", "role": "art_director"},
            },
        )
        self.assertEqual(status, 201)
        direction_ids = [
            item["direction_ids"][0] for item in bulk_directions["results"]
        ]
        status, bulk_direction_decision = self.request_json(
            "/api/directions/bulk-decision",
            method="POST",
            payload={
                "direction_ids": direction_ids,
                "decision": "approve",
                "actor": {"id": "art-api", "role": "art_director"},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(bulk_direction_decision["succeeded"], 2)
        self.assertEqual(bulk_direction_decision["failed"], 0)

    def test_sop_poem_import_preview_and_commit(self):
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
                "source": {
                    "source_type": "public_domain",
                    "citation": "项目自有公版整理，依据公共领域底本校勘",
                    "license": "Public Domain",
                    "verification_status": "verified",
                    "verified_at": "2026-07-18",
                },
                "theme": "山水",
                "mood": "壮阔、明亮",
                "imagery": ["香炉峰", "瀑布", "银河"],
            }
        ]
        path = f"/api/projects/{server.SOP_DEFAULT_PROJECT_ID}/poems/import"
        status, preview = self.request_json(
            path,
            method="POST",
            payload={
                "content": json.dumps(
                    {"schema_version": "poem-import/v1", "records": records},
                    ensure_ascii=False,
                ),
                "format": "json",
                "commit": False,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(preview["can_commit"])
        self.assertEqual(preview["counts"]["new"], 1)

        status, committed = self.request_json(
            path,
            method="POST",
            payload={
                "content": json.dumps(
                    {"schema_version": "poem-import/v1", "records": records},
                    ensure_ascii=False,
                ),
                "format": "json",
                "commit": True,
                "actor": {"id": "importer-api", "role": "content_editor"},
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(committed["imported"], 1)

        status, poems = self.request_json(
            "/api/poems?q=" + urllib.parse.quote("望庐山瀑布")
        )
        self.assertEqual(status, 200)
        self.assertEqual(poems["total"], 1)
        self.assertEqual(poems["items"][0]["status"], "content_review")

        status, approved = self.request_json(
            "/api/poems/wang-lu-shan-pu-bu/content/approve",
            method="POST",
            payload={"actor": {"id": "editor-api", "role": "content_editor"}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(approved["poem"]["status"], "requirement_draft")

        status, schema = self.request_json("/api/schemas/poem-import")
        self.assertEqual(status, 200)
        self.assertEqual(schema["title"], "PoemImportDocument v1")

        template_request = urllib.request.Request(
            self.base_url + "/api/templates/poem-import?format=csv"
        )
        with urllib.request.urlopen(template_request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("attachment", response.headers["Content-Disposition"])
            self.assertTrue(response.read().decode("utf-8-sig").startswith("id,title"))

        status, report = self.request_json("/api/reports/data-quality")
        self.assertEqual(status, 200)
        self.assertEqual(report["target_poem_count"], 300)
        self.assertEqual(report["total_poems"], 13)
        self.assertIn("coverage", report)

    def test_source_verification_endpoint_unlocks_content_approval(self):
        record = {
            "id": "test-source-gate-poem",
            "title": "来源门禁测试诗",
            "author": "王之涣",
            "dynasty": "唐",
            "lines": ["白日依山尽", "黄河入海流", "欲穷千里目", "更上一层楼"],
            "theme": "登临",
            "mood": "开阔",
            "imagery": ["白日", "黄河", "高楼"],
            "source": {
                "source_type": "unknown",
                "citation": "待复核底本",
                "license": "needs-review",
                "verification_status": "unverified",
            },
        }
        path = f"/api/projects/{server.SOP_DEFAULT_PROJECT_ID}/poems/import"
        status, _ = self.request_json(
            path,
            method="POST",
            payload={
                "records": [record],
                "commit": True,
                "actor": {"id": "editor-api", "role": "content_editor"},
            },
        )
        self.assertEqual(status, 201)

        status, blocked = self.request_json_error(
            "/api/poems/test-source-gate-poem/content/approve",
            method="POST",
            payload={"actor": {"id": "editor-api", "role": "content_editor"}},
        )
        self.assertEqual(status, 409)
        self.assertEqual(blocked["code"], "SOURCE_VERIFICATION_REQUIRED")

        status, updated = self.request_json(
            "/api/poems/test-source-gate-poem/source",
            method="POST",
            payload={
                "source": {
                    "source_type": "public_domain",
                    "citation": "公共领域底本，内容编辑完成逐句复核",
                    "license": "Public Domain",
                    "verification_status": "verified",
                    "verified_at": "2026-07-18",
                },
                "actor": {"id": "editor-api", "role": "content_editor"},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["source"]["version"], 2)

        status, approved = self.request_json(
            "/api/poems/test-source-gate-poem/content/approve",
            method="POST",
            payload={"actor": {"id": "editor-api", "role": "content_editor"}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(approved["poem"]["status"], "requirement_draft")

    def test_content_revision_and_bulk_approval_api(self):
        status, revised = self.request_json(
            "/api/poems/chun-xiao/content/revisions",
            method="POST",
            payload={
                "content": {
                    "title": "春晓",
                    "author": "孟浩然",
                    "dynasty": "唐",
                    "lines": ["春眠不觉晓", "处处闻啼鸟", "夜来风雨声", "花落知多少"],
                    "theme": "惜春与晨景",
                    "mood": "清新、惋惜",
                    "imagery": ["春眠", "啼鸟", "风雨", "落花"],
                    "notes": "内容编辑完成分行与意象复核。",
                    "change_summary": "补充惜春语义与复核记录",
                },
                "actor": {"id": "editor-api", "role": "content_editor"},
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(revised["content_version"]["version"], 2)
        self.assertEqual(revised["poem"]["status"], "content_review")

        status, premature = self.request_json(
            "/api/requirements/generate",
            method="POST",
            payload={
                "project_id": server.SOP_DEFAULT_PROJECT_ID,
                "poem_ids": ["chun-xiao"],
                "actor": {"id": "editor-api", "role": "content_editor"},
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(premature["results"][0]["code"], "APPROVED_CONTENT_REQUIRED")

        status, bulk = self.request_json(
            "/api/poems/content/bulk-approve",
            method="POST",
            payload={
                "poem_ids": ["chun-xiao", "jing-ye-si"],
                "actor": {"id": "editor-api", "role": "content_editor"},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(bulk["succeeded"], 1)
        self.assertEqual(bulk["failed"], 1)

        status, detail = self.request_json("/api/poems/chun-xiao")
        self.assertEqual(status, 200)
        self.assertEqual(detail["content_versions"][0]["status"], "approved")
        self.assertTrue(detail["content_versions"][0]["source_version_id"])

        status, forbidden = self.request_json_error(
            "/api/poems/shan-xing/content/revisions",
            method="POST",
            payload={
                "content": {
                    "title": "山行",
                    "author": "杜牧",
                    "dynasty": "唐",
                    "lines": ["远上寒山石径斜", "白云生处有人家"],
                    "theme": "山林秋色",
                    "mood": "明丽",
                    "imagery": ["寒山", "白云"],
                    "notes": "测试",
                    "change_summary": "越权修订测试",
                },
                "actor": {"id": "art-api", "role": "art_director"},
            },
        )
        self.assertEqual(status, 403)
        self.assertEqual(forbidden["code"], "CONTENT_ROLE_REQUIRED")

    def test_batch_api_runs_persistent_tasks_and_writes_review_candidates(self):
        actor = {"id": "producer-api", "role": "producer"}
        _, generated = self.request_json(
            "/api/requirements/generate",
            method="POST",
            payload={
                "project_id": server.SOP_DEFAULT_PROJECT_ID,
                "poem_ids": ["jing-ye-si"],
                "actor": actor,
            },
        )
        requirement_id = generated["results"][0]["requirement_id"]
        self.request_json(
            f"/api/requirements/{requirement_id}/approve",
            method="POST",
            payload={"actor": actor},
        )
        _, directions = self.request_json(
            "/api/directions/generate",
            method="POST",
            payload={
                "project_id": server.SOP_DEFAULT_PROJECT_ID,
                "poem_ids": ["jing-ye-si"],
                "actor": actor,
            },
        )
        direction_id = directions["results"][0]["direction_ids"][0]
        self.request_json(
            f"/api/directions/{direction_id}/approve",
            method="POST",
            payload={"actor": actor},
        )

        settings = {
            "project_id": server.SOP_DEFAULT_PROJECT_ID,
            "poem_ids": ["jing-ye-si"],
            "direction_ids": [direction_id],
            "style_id": "moonlit-blue-green",
            "aspect_ratio": "portrait",
            "count_per_direction": 2,
            "actor": actor,
        }
        status, estimate = self.request_json(
            "/api/batches/estimate", method="POST", payload=settings
        )
        self.assertEqual(status, 200)
        self.assertEqual(estimate["task_count"], 2)
        self.assertEqual(estimate["estimated_cost"], 0)
        self.assertTrue(estimate["can_start"])

        status, created = self.request_json(
            "/api/batches",
            method="POST",
            payload={**settings, "name": "静夜思首轮双样", "priority": 80},
        )
        self.assertEqual(status, 201)
        batch_id = created["batch"]["id"]
        self.assertEqual(created["batch"]["status"], "draft")

        status, started = self.request_json(
            f"/api/batches/{batch_id}/start",
            method="POST",
            payload={"actor": actor},
        )
        self.assertEqual(status, 202)
        self.assertIn(started["batch"]["status"], {"queued", "running"})

        completed = None
        tasks = []
        for _ in range(50):
            _, batches = self.request_json("/api/batches")
            completed = next(item for item in batches["items"] if item["id"] == batch_id)
            _, task_payload = self.request_json(
                "/api/tasks?limit=1&batch_id=" + urllib.parse.quote(batch_id)
            )
            tasks = task_payload["items"]
            self.assertEqual(task_payload["total"], 2)
            self.assertEqual(task_payload["limit"], 1)
            self.assertIn("has_next", task_payload)
            if completed["status"] in {"completed", "partial_failed", "failed"}:
                break
            time.sleep(0.1)

        self.assertIsNotNone(completed)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["progress"], 100)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "succeeded")
        self.assertEqual(tasks[0]["attempt_count"], 1)
        _, second_task_page = self.request_json(
            "/api/tasks?limit=1&offset=1&batch_id=" + urllib.parse.quote(batch_id)
        )
        self.assertEqual(len(second_task_page["items"]), 1)
        self.assertTrue(second_task_page["has_previous"])
        self.assertFalse(second_task_page["has_next"])
        all_tasks = [*tasks, *second_task_page["items"]]

        _, images_payload = self.request_json("/api/images")
        images = [
            item
            for item in images_payload["images"]
            if item.get("sop_batch_id") == batch_id
        ]
        self.assertEqual(len(images), 2)
        self.assertEqual(
            {item["sop_task_id"] for item in images},
            {item["id"] for item in all_tasks},
        )

        _, poem_payload = self.request_json(
            "/api/poems?q=" + urllib.parse.quote("静夜思")
        )
        self.assertEqual(poem_payload["items"][0]["status"], "candidate_review")

        status, review_queue = self.request_json(
            "/api/review-queue?include_blocked=true"
        )
        self.assertEqual(status, 200)
        candidates = review_queue["groups"][0]["candidates"]
        self.assertEqual(len(candidates), 2)
        candidate = next(
            item for item in candidates if item["status"] == "review_ready"
        )
        self.assertEqual(candidate["qc"]["status"], "pass")
        self.assertIn(candidate["qc"]["decision"], {"candidate", "recommended"})
        self.assertEqual(candidate["qc"]["reviewer_kind"], "synthetic_demo")
        self.assertEqual(len(candidate["qc"]["scores"]), 8)
        self.assertEqual(candidate["prompt_template_version"], "demo-six-segment-v3")
        self.assertEqual(len(candidate["prompt_hash"]), 64)
        self.assertIn("instruction", candidate["prompt_segments"])
        self.assertIn("style", candidate["prompt_segments"])
        status, calibration = self.request_json(
            f"/api/images/{candidate['id']}/qc-calibration",
            method="POST",
            payload={
                "human_decision": "candidate",
                "human_scores": {"poem_relevance": 84},
                "reason_tags": ["诗意准确"],
                "note": "API 校准测试",
                "actor": {"id": "producer-api", "role": "producer"},
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(calibration["sample"]["predicted_decision"], candidate["qc"]["decision"])
        status, calibration_report = self.request_json("/api/qc-calibration")
        self.assertEqual(status, 200)
        self.assertEqual(calibration_report["sample_count"], 1)
        status, decision = self.request_json(
            f"/api/images/{candidate['id']}/decision",
            method="POST",
            payload={
                "decision": "selected",
                "reason_tags": ["诗意准确", "构图最佳"],
                "note": "API 审片测试",
                "actor": {"id": "art-api", "role": "art_director"},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(decision["image"]["status"], "selected")
        status, rework = self.request_json(
            f"/api/images/{candidate['id']}/rework",
            method="POST",
            payload={
                "preserve": ["主体位置"],
                "change": ["减少背景元素"],
                "avoid": ["新增文字"],
                "actor": {"id": "art-api", "role": "art_director"},
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(rework["rework_order"]["source_image_id"], candidate["id"])
        order_id = rework["rework_order"]["id"]
        completed_order = None
        for _ in range(50):
            _, orders = self.request_json("/api/rework-orders")
            completed_order = next(
                item for item in orders["items"] if item["id"] == order_id
            )
            if completed_order["status"] in {"completed", "failed", "budget_blocked"}:
                break
            time.sleep(0.1)
        self.assertEqual(completed_order["status"], "completed")
        self.assertTrue(completed_order["output_image_id"])
        _, review_after_rework = self.request_json(
            "/api/review-queue?include_blocked=true"
        )
        all_candidates = [
            item
            for group in review_after_rework["groups"]
            for item in group["candidates"]
        ]
        child = next(
            item
            for item in all_candidates
            if item["id"] == completed_order["output_image_id"]
        )
        self.assertEqual(child["parent_image_id"], candidate["id"])
        self.assertEqual(child["generation"], 2)

        _, final_candidate = self.request_json(
            f"/api/images/{candidate['id']}/decision",
            method="POST",
            payload={
                "decision": "final_candidate",
                "reason_tags": ["诗意准确"],
                "actor": {"id": "art-api", "role": "art_director"},
            },
        )
        self.assertEqual(final_candidate["image"]["status"], "final_candidate")
        _, content_final = self.request_json(
            f"/api/images/{candidate['id']}/finalize",
            method="POST",
            payload={
                "reviewer_type": "content",
                "decision": "approved",
                "reason": "正文与诗意核对无误",
                "actor": {"id": "content-final-api", "role": "content_editor"},
            },
        )
        self.assertFalse(content_final["locked"])
        _, art_final = self.request_json(
            f"/api/images/{candidate['id']}/finalize",
            method="POST",
            payload={
                "reviewer_type": "art",
                "decision": "approved",
                "reason": "构图与风格基线通过",
                "actor": {"id": "art-final-api", "role": "art_director"},
            },
        )
        self.assertTrue(art_final["locked"])
        self.assertEqual(art_final["final_asset"]["version"], 1)
        _, final_assets = self.request_json("/api/final-assets")
        self.assertEqual(len(final_assets["items"]), 1)

        _, export_estimate = self.request_json(
            "/api/exports/estimate",
            method="POST",
            payload={"project_id": server.SOP_DEFAULT_PROJECT_ID},
        )
        self.assertTrue(export_estimate["can_export"])
        self.assertEqual(export_estimate["asset_count"], 1)
        status, exported = self.request_json(
            "/api/exports",
            method="POST",
            payload={
                "project_id": server.SOP_DEFAULT_PROJECT_ID,
                "actor": {"id": "producer-api", "role": "producer"},
            },
        )
        self.assertEqual(status, 201)
        package = exported["package"]
        self.assertEqual(package["status"], "completed")
        with urllib.request.urlopen(
            self.base_url + package["manifest_url"], timeout=5
        ) as response:
            manifest = json.loads(response.read().decode("utf-8"))
        self.assertEqual(manifest["asset_count"], 1)
        self.assertEqual(manifest["assets"][0]["source"]["style"]["version"], 1)
        self.assertEqual(
            manifest["assets"][0]["source"]["prompt_template_version"],
            "demo-six-segment-v3",
        )
        self.assertEqual(
            len(manifest["assets"][0]["source"]["prompt_hash"]),
            64,
        )
        self.assertEqual(
            manifest["assets"][0]["source"]["prompt_segments"]["instruction"]["id"],
            "instruction_global_v1",
        )
        self.assertEqual(
            manifest["assets"][0]["approvals"]["content"]["actor_id"],
            "content-final-api",
        )

    def test_budget_api_hard_stops_batch_before_provider_call(self):
        actor = {"id": "admin-api", "role": "system_admin"}
        store = server.get_sop_store()
        generated = store.generate_requirements(
            server.SOP_DEFAULT_PROJECT_ID, ["jiang-xue"], actor=actor
        )
        requirement_id = generated["results"][0]["requirement_id"]
        store.decide_requirement(requirement_id, "approve", actor=actor)
        directions = store.generate_directions(
            server.SOP_DEFAULT_PROJECT_ID, ["jiang-xue"], actor=actor
        )
        direction_id = directions["results"][0]["direction_ids"][0]
        store.decide_direction(direction_id, "approve", actor=actor)

        status, updated = self.request_json(
            f"/api/projects/{server.SOP_DEFAULT_PROJECT_ID}/budget",
            method="PATCH",
            payload={"hard_limit": 0.01, "soft_ratio": 0.7, "actor": actor},
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["budget"]["hard_limit"], 0.01)

        settings = {
            "project_id": server.SOP_DEFAULT_PROJECT_ID,
            "poem_ids": ["jiang-xue"],
            "direction_ids": [direction_id],
            "style_id": "ink-whitespace",
            "aspect_ratio": "portrait",
            "count_per_direction": 1,
            "actor": actor,
        }
        with mock.patch.dict(os.environ, {"AI_PROVIDER": "openai"}):
            _, estimate = self.request_json(
                "/api/batches/estimate", method="POST", payload=settings
            )
            self.assertGreater(estimate["estimated_cost"], 0.01)
            self.assertFalse(estimate["can_start"])
            _, created = self.request_json(
                "/api/batches", method="POST", payload=settings
            )
            batch_id = created["batch"]["id"]
            with self.assertRaises(urllib.error.HTTPError) as blocked:
                self.request_json(
                    f"/api/batches/{batch_id}/start",
                    method="POST",
                    payload={"actor": actor},
                )
            self.assertEqual(blocked.exception.code, 409)
            payload = json.loads(blocked.exception.read().decode("utf-8"))
            self.assertEqual(payload["code"], "BUDGET_BLOCKED")
            self.assertEqual(payload["batch"]["status"], "budget_blocked")
            self.assertFalse(any(image.get("sop_batch_id") == batch_id for image in server.STORE.images))

    def test_demo_worker_completes_a_sixty_image_batch(self):
        store = server.get_sop_store()
        actor = {"id": "load-test", "role": "producer"}
        poem_ids = [poem["id"] for poem in server.STORE.poems[:10]]
        generated = store.generate_requirements(
            server.SOP_DEFAULT_PROJECT_ID, poem_ids, actor=actor
        )
        for result in generated["results"]:
            store.decide_requirement(
                result["requirement_id"], "approve", actor=actor
            )
        direction_result = store.generate_directions(
            server.SOP_DEFAULT_PROJECT_ID, poem_ids, actor=actor
        )
        direction_ids = []
        for result in direction_result["results"]:
            direction_ids.extend(result["direction_ids"])
        for direction_id in direction_ids:
            store.decide_direction(direction_id, "approve", actor=actor)

        batch = store.create_batch(
            server.SOP_DEFAULT_PROJECT_ID,
            poem_ids,
            direction_ids=direction_ids,
            name="十首基准诗 · 60 张稳定性批次",
            style_id="light-gongbi",
            aspect_ratio="portrait",
            count_per_direction=2,
            provider="demo",
            model="demo-renderer",
            unit_cost=0,
            actor=actor,
        )
        self.assertEqual(batch["task_count"], 60)
        store.start_batch(batch["id"], actor=actor)
        server.run_batch_worker(batch["id"])

        state = store.execution_state(batch["id"])
        self.assertEqual(state["batch"]["status"], "completed")
        self.assertEqual(state["batch"]["progress"], 100)
        self.assertEqual(state["counts"]["succeeded"], 60)
        tasks = store.tasks(batch_id=batch["id"])
        self.assertTrue(all(task["attempt_count"] == 1 for task in tasks))
        images = [
            image
            for image in server.STORE.images
            if image.get("sop_batch_id") == batch["id"]
        ]
        self.assertEqual(len(images), 60)
        self.assertTrue(
            all((server.GENERATED_DIR / Path(image["url"]).name).is_file() for image in images)
        )

    def test_complete_generation_gallery_and_favorite_flow(self):
        status, result = self.request_json(
            "/api/generate",
            method="POST",
            payload={
                "poem_id": "jing-ye-si",
                "style_id": "moonlit-blue-green",
                "count": 2,
                "aspect_ratio": "portrait",
                "custom_note": "减少人物，保留留白",
            },
        )
        self.assertEqual(status, 202)
        job_id = result["job"]["id"]

        completed_job = None
        for _ in range(30):
            _, jobs_payload = self.request_json("/api/jobs")
            completed_job = next(job for job in jobs_payload["jobs"] if job["id"] == job_id)
            if completed_job["status"] in {"completed", "failed"}:
                break
            time.sleep(0.1)

        self.assertIsNotNone(completed_job)
        self.assertEqual(completed_job["status"], "completed")
        self.assertEqual(completed_job["progress"], 100)
        self.assertEqual(len(completed_job["image_ids"]), 2)

        _, images_payload = self.request_json("/api/images")
        generated = [image for image in images_payload["images"] if image["job_id"] == job_id]
        self.assertEqual(len(generated), 2)
        self.assertTrue(all(image["style_id"] == "moonlit-blue-green" for image in generated))

        image = generated[0]
        with urllib.request.urlopen(self.base_url + image["url"], timeout=5) as response:
            svg = response.read().decode("utf-8")
        self.assertIn("<svg", svg)
        self.assertNotIn("<text", svg)

        status, favorite_result = self.request_json(
            f"/api/images/{image['id']}", method="PATCH", payload={"favorite": True}
        )
        self.assertEqual(status, 200)
        self.assertTrue(favorite_result["image"]["favorite"])

    def test_prompt_contains_poem_style_and_safety_constraints(self):
        poem = server.STORE.poem_by_id["jiang-xue"]
        style = server.STORE.style_by_id["ink-whitespace"]
        prompt = server.build_prompt(poem, style, "more negative space")
        self.assertIn("江雪", prompt)
        self.assertIn(style["prompt_fragment"], prompt)
        self.assertIn("no text", prompt.lower())
        self.assertIn("more negative space", prompt)

    def test_project_convergence_review_and_quality_gate(self):
        status, project_result = self.request_json(
            "/api/projects",
            method="POST",
            payload={
                "name": "王维山水诗基线",
                "purpose": "诗画卡片",
                "poem_ids": ["lu-zhai", "shan-ju-qiu-ming"],
                "style_id": "ink-whitespace",
                "aspect_ratio": "portrait",
            },
        )
        self.assertEqual(status, 201)
        project = project_result["project"]

        with self.assertRaises(urllib.error.HTTPError) as invalid_convergence:
            self.request_json(
                "/api/generate",
                method="POST",
                payload={
                    "project_id": project["id"],
                    "poem_id": "lu-zhai",
                    "style_id": "ink-whitespace",
                    "generation_mode": "converge",
                    "count": 1,
                    "aspect_ratio": "portrait",
                    "custom_note": "只增加林间光线",
                },
            )
        self.assertEqual(invalid_convergence.exception.code, 400)

        _, explore_result = self.request_json(
            "/api/generate",
            method="POST",
            payload={
                "project_id": project["id"],
                "poem_id": "lu-zhai",
                "style_id": "ink-whitespace",
                "generation_mode": "explore",
                "count": 1,
                "aspect_ratio": "portrait",
                "custom_note": "探索林间返照",
            },
        )
        explore_job_id = explore_result["job"]["id"]
        parent = None
        for _ in range(30):
            _, images_payload = self.request_json("/api/images")
            parent = next(
                (item for item in images_payload["images"] if item["job_id"] == explore_job_id),
                None,
            )
            if parent:
                break
            time.sleep(0.1)
        self.assertIsNotNone(parent)
        self.assertEqual(parent["project_id"], project["id"])
        self.assertEqual(parent["decision"], "candidate")

        _, selected_result = self.request_json(
            f"/api/images/{parent['id']}",
            method="PATCH",
            payload={
                "decision": "selected",
                "feedback_tags": ["增加留白", "统一色彩"],
                "review_note": "保留返照，只简化背景",
            },
        )
        self.assertEqual(selected_result["image"]["decision"], "selected")
        self.assertTrue(selected_result["image"]["favorite"])

        _, converge_result = self.request_json(
            "/api/generate",
            method="POST",
            payload={
                "project_id": project["id"],
                "poem_id": "lu-zhai",
                "style_id": "ink-whitespace",
                "generation_mode": "converge",
                "parent_image_id": parent["id"],
                "preserve": ["构图层级", "色彩关系"],
                "count": 1,
                "aspect_ratio": "portrait",
                "custom_note": "只简化背景，增强返照",
            },
        )
        self.assertEqual(converge_result["job"]["parent_image_id"], parent["id"])

        with self.assertRaises(urllib.error.HTTPError) as premature_final:
            self.request_json(
                f"/api/images/{parent['id']}",
                method="PATCH",
                payload={"decision": "final"},
            )
        self.assertEqual(premature_final.exception.code, 409)

        qc = {key: True for key in server.QC_KEYS}
        _, qc_result = self.request_json(
            f"/api/images/{parent['id']}", method="PATCH", payload={"qc": qc}
        )
        self.assertTrue(all(qc_result["image"]["qc"].values()))
        _, final_result = self.request_json(
            f"/api/images/{parent['id']}",
            method="PATCH",
            payload={"decision": "final"},
        )
        self.assertEqual(final_result["image"]["decision"], "final")

    def test_openai_adapter_saves_base64_image_response(self):
        image_bytes = b"test-png-binary"
        api_payload = json.dumps(
            {"data": [{"b64_json": base64.b64encode(image_bytes).decode("ascii")}]}
        ).encode("utf-8")

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return api_payload

        output = Path(self.temp_dir.name) / "adapter-output.png"
        with mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "test-key", "OPENAI_IMAGE_MODEL": "gpt-image-2"},
        ), mock.patch("server.urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
            server.generate_openai_image(output, "a quiet Tang landscape", "portrait")

        self.assertEqual(output.read_bytes(), image_bytes)
        request = urlopen.call_args.args[0]
        sent_payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(sent_payload["model"], "gpt-image-2")
        self.assertEqual(sent_payload["size"], "1024x1536")

    def test_openai_edit_adapter_sends_parent_image_as_multipart(self):
        image_bytes = b"edited-png-binary"
        api_payload = json.dumps(
            {"data": [{"b64_json": base64.b64encode(image_bytes).decode("ascii")}]}
        ).encode("utf-8")

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return api_payload

        parent = Path(self.temp_dir.name) / "parent.png"
        parent.write_bytes(b"parent-image-bytes")
        output = Path(self.temp_dir.name) / "edited-output.png"
        with mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "test-key", "OPENAI_IMAGE_MODEL": "gpt-image-2"},
        ), mock.patch("server.urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
            server.edit_openai_image(
                output,
                "Keep the composition; change only the moon size",
                "portrait",
                parent,
            )

        self.assertEqual(output.read_bytes(), image_bytes)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/images/edits")
        self.assertIn("multipart/form-data; boundary=", request.headers["Content-type"])
        self.assertIn(b'name="image[]"', request.data)
        self.assertIn(b'filename="parent.png"', request.data)
        self.assertIn(b"gpt-image-2", request.data)
        self.assertIn(b"change only the moon size", request.data)
        self.assertIn(b"parent-image-bytes", request.data)


if __name__ == "__main__":
    unittest.main()
