import base64
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.request
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

        server.DATA_DIR = data_dir
        server.GENERATED_DIR = generated_dir
        server.STATE_FILE = data_dir / "state.json"
        server.POEMS_FILE = data_dir / "poems.json"
        server.STYLES_FILE = data_dir / "styles.json"
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
        self.temp_dir.cleanup()
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

    def test_health_and_bootstrap(self):
        status, health = self.request_json("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])
        self.assertEqual(health["provider"], "demo")

        status, bootstrap = self.request_json("/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(len(bootstrap["poems"]), 10)
        self.assertEqual(len(bootstrap["styles"]), 6)
        self.assertEqual(len(bootstrap["images"]), 9)

        head_request = urllib.request.Request(self.base_url + "/", method="HEAD")
        with urllib.request.urlopen(head_request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "text/html")

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


if __name__ == "__main__":
    unittest.main()
