import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from review_schema import REVIEW_RESULT_SCHEMA_VERSION
from visual_reviewer import review_image


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def _review_payload(score=86):
    return {
        "schema_version": REVIEW_RESULT_SCHEMA_VERSION,
        "overall_score": score,
        "scores": {
            "safety": 96,
            "technical_integrity": 90,
            "poem_relevance": score,
            "style_match": 87,
            "historical_plausibility": 82,
            "composition": 88,
            "character_quality": 90,
            "series_consistency": 85,
        },
        "has_unexpected_text": False,
        "hard_fail_reasons": [],
        "problems": [],
        "decision": "recommended",
        "confidence": 0.88,
        "evidence": {
            "observed_elements": ["月色与山影"],
            "missing_required_elements": [],
            "uncertain_elements": ["远景建筑细节"],
        },
        "reviewer_version": "openai-vision-review-v1",
    }


class VisualReviewerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.image_path = Path(self.temp_dir.name) / "candidate.png"
        self.image_path.write_bytes(b"test-png-payload")
        self.context = {
            "poem": {"title": "静夜思", "lines": ["床前明月光"]},
            "requirement": {"must_have": ["月光"]},
            "direction": {"type": "environment"},
            "style": {"id": "ink-whitespace"},
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_demo_review_is_deterministic_and_explicitly_synthetic(self):
        first, metadata = review_image(
            self.image_path,
            image_provider="demo",
            context=self.context,
            policy_version_id="qcpolicy_test",
        )
        second, _ = review_image(
            self.image_path,
            image_provider="demo",
            context=self.context,
            policy_version_id="qcpolicy_test",
        )
        self.assertEqual(first, second)
        self.assertEqual(metadata["reviewer_kind"], "synthetic_demo")
        self.assertIn("演示图仅用于验证流程", first["evidence"]["uncertain_elements"][0])

    def test_openai_adapter_sends_image_and_strict_structured_output(self):
        provider_response = {
            "id": "resp_test",
            "model": "gpt-5.6-luna",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": json.dumps(_review_payload())}
                    ],
                }
            ],
            "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        }
        captured = {}

        def fake_urlopen(request, timeout):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return _Response(provider_response)

        with mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "test-key", "TANG_VISION_QC_ENABLED": "1"},
        ), mock.patch("visual_reviewer.urllib.request.urlopen", side_effect=fake_urlopen):
            result, metadata = review_image(
                self.image_path,
                image_provider="openai",
                context=self.context,
                policy_version_id="qcpolicy_test",
            )

        self.assertEqual(result["decision"], "recommended")
        self.assertEqual(metadata["reviewer_kind"], "openai_vision")
        self.assertFalse(captured["body"]["store"])
        self.assertTrue(
            captured["body"]["input"][0]["content"][1]["image_url"].startswith(
                "data:image/png;base64,"
            )
        )
        self.assertTrue(captured["body"]["text"]["format"]["strict"])

    def test_disabled_real_reviewer_returns_safe_manual_fallback_metadata(self):
        with mock.patch.dict(os.environ, {"TANG_VISION_QC_ENABLED": "0"}):
            result, metadata = review_image(
                self.image_path,
                image_provider="openai",
                context=self.context,
                policy_version_id="qcpolicy_test",
            )
        self.assertIsNone(result)
        self.assertEqual(metadata["unavailable_code"], "visual_review_disabled")


if __name__ == "__main__":
    unittest.main()
