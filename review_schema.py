"""Contracts and deterministic policy evaluation for visual QC results."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


REVIEW_RESULT_SCHEMA_VERSION = "review-result/v1"
QC_POLICY_SCHEMA_VERSION = "qc-policy/v1"
SCORE_FIELDS = (
    "safety",
    "technical_integrity",
    "poem_relevance",
    "style_match",
    "historical_plausibility",
    "composition",
    "character_quality",
    "series_consistency",
)
DECISIONS = {"rejected", "manual_review", "candidate", "recommended"}
PROBLEM_DIMENSIONS = {*SCORE_FIELDS, "text_noise"}
PROBLEM_SEVERITIES = {"low", "medium", "high", "critical"}
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_CODE = re.compile(r"^[A-Z0-9_]{2,80}$")


class ReviewContractError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or []


def _schema_document(filename: str) -> dict[str, Any]:
    path = Path(__file__).resolve().parent / "schemas" / filename
    return json.loads(path.read_text(encoding="utf-8"))


def review_result_schema_document() -> dict[str, Any]:
    return _schema_document("review-result.schema.json")


def qc_policy_schema_document() -> dict[str, Any]:
    return _schema_document("qc-policy.schema.json")


def _text(value: Any, field: str, errors: list[str], *, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field} 必须是非空字符串")
        return ""
    if len(value.strip()) > limit:
        errors.append(f"{field} 长度不能超过 {limit}")
    return value.strip()


def _score(value: Any, field: str, errors: list[str]) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        errors.append(f"{field} 必须是 0–100 的整数")
        return 0
    return value


def _string_list(
    value: Any,
    field: str,
    errors: list[str],
    *,
    maximum: int,
    item_limit: int,
) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"{field} 必须是数组")
        return []
    if len(value) > maximum:
        errors.append(f"{field} 最多 {maximum} 项")
    result: list[str] = []
    for index, item in enumerate(value[:maximum]):
        result.append(_text(item, f"{field}[{index}]", errors, limit=item_limit))
    return result


def validate_review_result(payload: Any) -> dict[str, Any]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        raise ReviewContractError(
            "REVIEW_RESULT_INVALID",
            "视觉质检结果必须是 JSON 对象。",
            details=["root 必须是 object"],
        )
    allowed = {
        "schema_version",
        "overall_score",
        "scores",
        "has_unexpected_text",
        "hard_fail_reasons",
        "problems",
        "decision",
        "confidence",
        "evidence",
        "reviewer_version",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        errors.append("存在未定义字段：" + "、".join(unknown))
    if payload.get("schema_version") != REVIEW_RESULT_SCHEMA_VERSION:
        errors.append(f"schema_version 必须是 {REVIEW_RESULT_SCHEMA_VERSION}")
    overall_score = _score(payload.get("overall_score"), "overall_score", errors)
    raw_scores = payload.get("scores")
    scores: dict[str, int] = {}
    if not isinstance(raw_scores, dict):
        errors.append("scores 必须是对象")
        raw_scores = {}
    unknown_scores = sorted(set(raw_scores) - set(SCORE_FIELDS))
    if unknown_scores:
        errors.append("scores 存在未定义维度：" + "、".join(unknown_scores))
    for field in SCORE_FIELDS:
        scores[field] = _score(raw_scores.get(field), f"scores.{field}", errors)
    has_unexpected_text = payload.get("has_unexpected_text")
    if not isinstance(has_unexpected_text, bool):
        errors.append("has_unexpected_text 必须是布尔值")
        has_unexpected_text = False
    hard_fail_reasons = _string_list(
        payload.get("hard_fail_reasons"),
        "hard_fail_reasons",
        errors,
        maximum=12,
        item_limit=80,
    )
    problems: list[dict[str, str]] = []
    raw_problems = payload.get("problems")
    if not isinstance(raw_problems, list):
        errors.append("problems 必须是数组")
        raw_problems = []
    if len(raw_problems) > 12:
        errors.append("problems 最多 12 项")
    for index, problem in enumerate(raw_problems[:12]):
        prefix = f"problems[{index}]"
        if not isinstance(problem, dict):
            errors.append(f"{prefix} 必须是对象")
            continue
        expected = {"code", "dimension", "severity", "note", "evidence"}
        if set(problem) != expected:
            errors.append(f"{prefix} 字段必须完整且不能扩展")
        code = _text(problem.get("code"), f"{prefix}.code", errors, limit=80)
        if code and not _CODE.fullmatch(code):
            errors.append(f"{prefix}.code 必须是大写下划线错误码")
        dimension = str(problem.get("dimension") or "")
        if dimension not in PROBLEM_DIMENSIONS:
            errors.append(f"{prefix}.dimension 无效")
        severity = str(problem.get("severity") or "")
        if severity not in PROBLEM_SEVERITIES:
            errors.append(f"{prefix}.severity 无效")
        problems.append(
            {
                "code": code,
                "dimension": dimension,
                "severity": severity,
                "note": _text(problem.get("note"), f"{prefix}.note", errors, limit=300),
                "evidence": _text(
                    problem.get("evidence"),
                    f"{prefix}.evidence",
                    errors,
                    limit=300,
                ),
            }
        )
    decision = str(payload.get("decision") or "")
    if decision not in DECISIONS:
        errors.append("decision 无效")
    confidence = payload.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        errors.append("confidence 必须是 0–1 的数字")
        confidence = 0.0
    confidence = float(confidence)
    if not 0 <= confidence <= 1:
        errors.append("confidence 必须在 0–1 之间")
    raw_evidence = payload.get("evidence")
    evidence: dict[str, list[str]] = {}
    if not isinstance(raw_evidence, dict):
        errors.append("evidence 必须是对象")
        raw_evidence = {}
    evidence_fields = {
        "observed_elements",
        "missing_required_elements",
        "uncertain_elements",
    }
    if set(raw_evidence) != evidence_fields:
        errors.append("evidence 字段必须完整且不能扩展")
    for field in sorted(evidence_fields):
        evidence[field] = _string_list(
            raw_evidence.get(field),
            f"evidence.{field}",
            errors,
            maximum=12,
            item_limit=120,
        )
    reviewer_version = _text(
        payload.get("reviewer_version"),
        "reviewer_version",
        errors,
        limit=100,
    )
    if decision == "rejected" and not hard_fail_reasons and not problems:
        errors.append("rejected 必须给出 hard_fail_reasons 或 problems")
    if errors:
        raise ReviewContractError(
            "REVIEW_RESULT_INVALID",
            "视觉质检结果未通过 ReviewResult v1 校验。",
            details=errors,
        )
    return {
        "schema_version": REVIEW_RESULT_SCHEMA_VERSION,
        "overall_score": overall_score,
        "scores": scores,
        "has_unexpected_text": has_unexpected_text,
        "hard_fail_reasons": list(dict.fromkeys(hard_fail_reasons)),
        "problems": problems,
        "decision": decision,
        "confidence": round(confidence, 4),
        "evidence": evidence,
        "reviewer_version": reviewer_version,
    }


def validate_qc_policy(payload: Any) -> dict[str, Any]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        raise ReviewContractError(
            "QC_POLICY_INVALID",
            "QC 政策必须是 JSON 对象。",
            details=["root 必须是 object"],
        )
    expected = {
        "semantic_version",
        "name",
        "release_notes",
        "weights",
        "thresholds",
        "hard_fail_problem_codes",
    }
    if set(payload) != expected:
        errors.append("QC 政策字段必须完整且不能扩展")
    semantic_version = str(payload.get("semantic_version") or "")
    if not _SEMVER.fullmatch(semantic_version):
        errors.append("semantic_version 必须符合 x.y.z")
    name = _text(payload.get("name"), "name", errors, limit=100)
    release_notes = _text(
        payload.get("release_notes"), "release_notes", errors, limit=500
    )
    raw_weights = payload.get("weights")
    weights: dict[str, float] = {}
    if not isinstance(raw_weights, dict) or set(raw_weights) != set(SCORE_FIELDS):
        errors.append("weights 必须包含且只包含全部评分维度")
        raw_weights = raw_weights if isinstance(raw_weights, dict) else {}
    for field in SCORE_FIELDS:
        value = raw_weights.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
            errors.append(f"weights.{field} 必须在 0–1 之间")
            value = 0
        weights[field] = float(value)
    if abs(sum(weights.values()) - 1) > 0.0001:
        errors.append("weights 权重之和必须等于 1")
    threshold_fields = {
        "reject_below",
        "manual_review_below",
        "recommended_from",
        "poem_relevance_hard_fail_below",
        "historical_hard_fail_below",
        "historical_export_minimum",
        "confidence_manual_review_below",
    }
    raw_thresholds = payload.get("thresholds")
    thresholds: dict[str, int | float] = {}
    if not isinstance(raw_thresholds, dict) or set(raw_thresholds) != threshold_fields:
        errors.append("thresholds 字段必须完整且不能扩展")
        raw_thresholds = raw_thresholds if isinstance(raw_thresholds, dict) else {}
    for field in threshold_fields - {"confidence_manual_review_below"}:
        thresholds[field] = _score(raw_thresholds.get(field), f"thresholds.{field}", errors)
    confidence_threshold = raw_thresholds.get("confidence_manual_review_below")
    if (
        isinstance(confidence_threshold, bool)
        or not isinstance(confidence_threshold, (int, float))
        or not 0 <= float(confidence_threshold) <= 1
    ):
        errors.append("thresholds.confidence_manual_review_below 必须在 0–1 之间")
        confidence_threshold = 0
    thresholds["confidence_manual_review_below"] = float(confidence_threshold)
    if not (
        int(thresholds["reject_below"])
        < int(thresholds["manual_review_below"])
        <= int(thresholds["recommended_from"])
    ):
        errors.append("reject_below < manual_review_below <= recommended_from")
    hard_codes = _string_list(
        payload.get("hard_fail_problem_codes"),
        "hard_fail_problem_codes",
        errors,
        maximum=30,
        item_limit=80,
    )
    if not hard_codes:
        errors.append("hard_fail_problem_codes 至少一项")
    for code in hard_codes:
        if code and not _CODE.fullmatch(code):
            errors.append("hard_fail_problem_codes 必须是大写下划线错误码")
    if errors:
        raise ReviewContractError(
            "QC_POLICY_INVALID",
            "QC 政策未通过校验。",
            details=errors,
        )
    return {
        "semantic_version": semantic_version,
        "name": name,
        "release_notes": release_notes,
        "weights": weights,
        "thresholds": thresholds,
        "hard_fail_problem_codes": list(dict.fromkeys(hard_codes)),
    }


def compose_qc_result(
    local_inspection: dict[str, Any],
    visual_result: dict[str, Any] | None,
    policy: dict[str, Any],
    *,
    policy_version_id: str,
    reviewer_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge proven local checks with model review using deterministic gates."""

    policy = validate_qc_policy(policy)
    result = dict(local_inspection)
    hard_failures = list(dict.fromkeys(result.get("hard_failures") or []))
    warnings = list(dict.fromkeys(result.get("warnings") or []))
    coverage = list(dict.fromkeys(result.get("coverage") or []))
    metadata = dict(reviewer_metadata or {})
    result.update(
        {
            "policy_version_id": policy_version_id,
            "policy_semantic_version": policy["semantic_version"],
            "reviewer_kind": str(metadata.get("reviewer_kind") or "unavailable"),
            "reviewer_model": str(metadata.get("reviewer_model") or ""),
            "input_hash": str(metadata.get("input_hash") or ""),
            "usage": metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {},
            "estimated_cost": max(0.0, float(metadata.get("estimated_cost") or 0)),
        }
    )
    if visual_result is None:
        warning = str(metadata.get("unavailable_code") or "visual_review_unavailable")
        warnings.append(warning)
        result.update(
            {
                "version": f"{result.get('version', 'local')}+visual-unavailable",
                "status": "hard_fail" if hard_failures else "manual_required",
                "score": max(0, min(float(result.get("score") or 0), 100)),
                "hard_failures": hard_failures,
                "warnings": list(dict.fromkeys(warnings)),
                "coverage": coverage,
                "scores": {},
                "problems": [],
                "decision": "rejected" if hard_failures else "manual_review",
                "confidence": 0.0,
                "evidence": {},
                "raw_visual_score": None,
            }
        )
        return result

    visual = validate_review_result(visual_result)
    scores = dict(visual["scores"])
    scores["technical_integrity"] = min(
        scores["technical_integrity"],
        round(max(0, min(float(local_inspection.get("score") or 0), 100))),
    )
    computed_score = round(
        sum(scores[field] * float(policy["weights"][field]) for field in SCORE_FIELDS),
        1,
    )
    hard_failures.extend(visual["hard_fail_reasons"])
    if visual["has_unexpected_text"]:
        hard_failures.append("UNEXPECTED_TEXT")
    if scores["poem_relevance"] < int(
        policy["thresholds"]["poem_relevance_hard_fail_below"]
    ):
        hard_failures.append("POEM_RELEVANCE_CRITICAL")
    if scores["historical_plausibility"] < int(
        policy["thresholds"]["historical_hard_fail_below"]
    ):
        hard_failures.append("HISTORICAL_PLAUSIBILITY_CRITICAL")
    hard_code_set = set(policy["hard_fail_problem_codes"])
    hard_failures.extend(
        problem["code"]
        for problem in visual["problems"]
        if problem["code"] in hard_code_set
        or problem["severity"] == "critical"
    )
    if computed_score < int(policy["thresholds"]["reject_below"]):
        hard_failures.append("OVERALL_SCORE_BELOW_REJECT_THRESHOLD")
    hard_failures = list(dict.fromkeys(hard_failures))
    warnings.extend(
        f"visual_review:{problem['code']}"
        for problem in visual["problems"]
        if problem["code"] not in hard_failures
    )
    manual_required = (
        computed_score < int(policy["thresholds"]["manual_review_below"])
        or visual["confidence"]
        < float(policy["thresholds"]["confidence_manual_review_below"])
        or visual["decision"] in {"manual_review", "rejected"}
    )
    if hard_failures:
        status = "hard_fail"
        decision = "rejected"
    elif manual_required:
        status = "manual_required"
        decision = "manual_review"
    else:
        status = "pass"
        decision = (
            "recommended"
            if computed_score >= int(policy["thresholds"]["recommended_from"])
            else "candidate"
        )
    coverage.extend(
        [
            "L1_vision_safety_text",
            "L2_poem_relevance",
            "L3_historical_plausibility",
            "L4_aesthetic_quality",
            "L5_series_consistency",
        ]
    )
    result.update(
        {
            "version": f"{result.get('version', 'local')}+{visual['reviewer_version']}",
            "status": status,
            "score": computed_score,
            "hard_failures": hard_failures,
            "warnings": list(dict.fromkeys(warnings)),
            "coverage": list(dict.fromkeys(coverage)),
            "scores": scores,
            "problems": visual["problems"],
            "decision": decision,
            "confidence": visual["confidence"],
            "evidence": visual["evidence"],
            "raw_visual_score": visual["overall_score"],
        }
    )
    return result
