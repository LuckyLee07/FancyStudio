import tempfile
import unittest
from pathlib import Path

from qc_engine import hamming_distance, inspect_image


class QcEngineTests(unittest.TestCase):
    def test_valid_svg_reports_proven_checks_and_manual_risks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "valid.svg"
            path.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1536"><rect width="1024" height="1536" fill="#f5f0e5"/><path d="M100 300 L900 1200" stroke="#243544"/></svg>',
                encoding="utf-8",
            )
            result = inspect_image(path, "portrait")
        self.assertEqual(result["status"], "soft_risk")
        self.assertEqual(result["width"], 1024)
        self.assertEqual(result["height"], 1536)
        self.assertTrue(result["checks"]["aspect_ratio_match"])
        self.assertTrue(result["checks"]["no_embedded_text"])
        self.assertTrue(result["perceptual_hash"])
        self.assertIn(
            "semantic_historical_and_aesthetic_checks_require_human_review",
            result["warnings"],
        )

    def test_svg_text_and_wrong_ratio_are_hard_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.svg"
            path.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024"><text x="10" y="20">watermark</text></svg>',
                encoding="utf-8",
            )
            result = inspect_image(path, "portrait")
        self.assertEqual(result["status"], "hard_fail")
        self.assertIn("aspect_ratio_mismatch", result["hard_failures"])
        self.assertIn("embedded_text_or_watermark", result["hard_failures"])

    def test_hamming_distance_rejects_invalid_or_distant_hashes(self):
        self.assertEqual(hamming_distance("0000000000000000", "0000000000000001"), 1)
        self.assertEqual(hamming_distance("0" * 16, "f" * 16), 64)
        self.assertGreater(hamming_distance("bad", "different"), 64)


if __name__ == "__main__":
    unittest.main()
