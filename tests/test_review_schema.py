import json
import unittest
from pathlib import Path

from review_schema import (
    QC_POLICY_SCHEMA_VERSION,
    REVIEW_RESULT_SCHEMA_VERSION,
    compose_qc_result,
    qc_policy_schema_document,
    review_result_schema_document,
    validate_qc_policy,
    validate_review_result,
)


ROOT = Path(__file__).resolve().parents[1]


class ReviewSchemaTests(unittest.TestCase):
    def setUp(self):
        self.policy = json.loads(
            (ROOT / "data" / "qc_policy.json").read_text(encoding="utf-8")
        )[0]
        self.local = {
            "version": "local-rules-v2",
            "status": "manual_required",
            "score": 94,
            "hard_failures": [],
            "warnings": [],
            "checks": {"inspection_completed": True},
            "coverage": ["L0_file_integrity"],
        }

    @staticmethod
    def visual(score=88, *, history=None, decision="recommended", confidence=0.9):
        scores = {
            "safety": score,
            "technical_integrity": score,
            "poem_relevance": score,
            "style_match": score,
            "historical_plausibility": history if history is not None else score,
            "composition": score,
            "character_quality": score,
            "series_consistency": score,
        }
        return {
            "schema_version": REVIEW_RESULT_SCHEMA_VERSION,
            "overall_score": score,
            "scores": scores,
            "has_unexpected_text": False,
            "hard_fail_reasons": [],
            "problems": [],
            "decision": decision,
            "confidence": confidence,
            "evidence": {
                "observed_elements": ["山水与月色"],
                "missing_required_elements": [],
                "uncertain_elements": [],
            },
            "reviewer_version": "test-review-v1",
        }

    def test_schema_documents_and_runtime_contracts_stay_aligned(self):
        self.assertEqual(review_result_schema_document()["title"], "ReviewResult v1")
        self.assertEqual(qc_policy_schema_document()["title"], "QCPolicy v1")
        self.assertEqual(
            validate_review_result(self.visual())["schema_version"],
            REVIEW_RESULT_SCHEMA_VERSION,
        )
        self.assertEqual(QC_POLICY_SCHEMA_VERSION, "qc-policy/v1")
        self.assertEqual(validate_qc_policy(self.policy)["semantic_version"], "1.0.0")

    def test_deterministic_policy_routes_recommended_candidate_and_manual(self):
        recommended = compose_qc_result(
            self.local,
            self.visual(90),
            self.policy,
            policy_version_id="qcpolicy_test",
        )
        candidate = compose_qc_result(
            self.local,
            self.visual(80, decision="candidate"),
            self.policy,
            policy_version_id="qcpolicy_test",
        )
        manual = compose_qc_result(
            self.local,
            None,
            self.policy,
            policy_version_id="qcpolicy_test",
            reviewer_metadata={"unavailable_code": "vision_offline"},
        )
        inconsistent_rejection = self.visual(90, decision="rejected")
        inconsistent_rejection["problems"] = [
            {
                "code": "SUBJECT_AMBIGUOUS",
                "dimension": "poem_relevance",
                "severity": "medium",
                "note": "主体指向不够明确",
                "evidence": "画面只有远景，未见明确叙事主体",
            }
        ]
        rejected_by_reviewer = compose_qc_result(
            self.local,
            inconsistent_rejection,
            self.policy,
            policy_version_id="qcpolicy_test",
        )
        self.assertEqual(recommended["decision"], "recommended")
        self.assertEqual(candidate["decision"], "candidate")
        self.assertEqual(manual["status"], "manual_required")
        self.assertIn("vision_offline", manual["warnings"])
        self.assertEqual(rejected_by_reviewer["decision"], "manual_review")

    def test_local_hard_failure_and_critical_history_can_never_pass(self):
        local = {**self.local, "hard_failures": ["aspect_ratio_mismatch"]}
        local_failure = compose_qc_result(
            local,
            self.visual(95),
            self.policy,
            policy_version_id="qcpolicy_test",
        )
        history_failure = compose_qc_result(
            self.local,
            self.visual(88, history=20),
            self.policy,
            policy_version_id="qcpolicy_test",
        )
        self.assertEqual(local_failure["decision"], "rejected")
        self.assertIn("aspect_ratio_mismatch", local_failure["hard_failures"])
        self.assertEqual(history_failure["decision"], "rejected")
        self.assertIn(
            "HISTORICAL_PLAUSIBILITY_CRITICAL",
            history_failure["hard_failures"],
        )


if __name__ == "__main__":
    unittest.main()
