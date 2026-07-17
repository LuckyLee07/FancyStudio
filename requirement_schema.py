"""RequirementCard v1 contract, normalization and one-pass repair.

The production store deliberately validates structured objects rather than
trying to recover fields from prose. Repair is limited to deterministic shape
normalization; missing semantic evidence remains a hard failure.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "requirement-card/v1"
GENERATOR_VERSION = "local-requirement-planner/v2"
SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "requirement-card.schema.json"

TEXT_FIELDS = (
    "theme",
    "mood",
    "time_and_place",
    "subject",
    "composition",
    "editor_note",
)
LIST_FIELDS = (
    "core_imagery",
    "must_have",
    "avoid",
    "historical_risks",
    "uncertainties",
    "locked_fields",
)
REQUIRED_FIELDS = (
    "theme",
    "mood",
    "time_and_place",
    "subject",
    "core_imagery",
    "composition",
    "must_have",
    "avoid",
    "historical_risks",
    "uncertainties",
    "evidence",
    "confidence",
    "editor_note",
    "locked_fields",
)
ALLOWED_FIELDS = set(REQUIRED_FIELDS)
CONFIDENCE_FIELDS = (
    "time_and_place",
    "subject",
    "composition",
    "historical_risks",
)
EVIDENCE_SUPPORT_FIELDS = {
    "theme",
    "mood",
    "time_and_place",
    "subject",
    "core_imagery",
    "composition",
    "must_have",
    "avoid",
    "historical_risks",
    "uncertainties",
}
EDITABLE_FIELDS = ALLOWED_FIELDS - {"confidence", "evidence", "locked_fields"}


def schema_document() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _issue(path: str, code: str, message: str) -> dict[str, str]:
    return {"path": path, "code": code, "message": message}


def confidence_level(score: float) -> str:
    if score < 0.6:
        return "low"
    if score < 0.8:
        return "medium"
    return "high"


def validate_requirement_card(
    payload: Any,
    *,
    source_text: str = "",
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if not isinstance(payload, dict):
        return [_issue("$", "TYPE_OBJECT_REQUIRED", "RequirementCard 必须是 JSON 对象。")]

    for field in sorted(set(payload) - ALLOWED_FIELDS):
        issues.append(_issue(f"$.{field}", "UNKNOWN_FIELD", "字段不在 RequirementCard v1 中。"))
    for field in REQUIRED_FIELDS:
        if field not in payload:
            issues.append(_issue(f"$.{field}", "REQUIRED_FIELD_MISSING", "缺少必填字段。"))

    for field in TEXT_FIELDS:
        if field not in payload:
            continue
        value = payload[field]
        if not isinstance(value, str):
            issues.append(_issue(f"$.{field}", "STRING_REQUIRED", "字段必须是字符串。"))
            continue
        if field != "editor_note" and not value.strip():
            issues.append(_issue(f"$.{field}", "NON_EMPTY_REQUIRED", "字段不能为空。"))
        if len(value) > (2000 if field in {"composition", "editor_note"} else 500):
            issues.append(_issue(f"$.{field}", "STRING_TOO_LONG", "字段超过长度限制。"))

    minimum_items = {
        "core_imagery": 1,
        "must_have": 1,
        "avoid": 1,
        "historical_risks": 0,
        "uncertainties": 0,
        "locked_fields": 0,
    }
    for field in LIST_FIELDS:
        if field not in payload:
            continue
        value = payload[field]
        if not isinstance(value, list):
            issues.append(_issue(f"$.{field}", "ARRAY_REQUIRED", "字段必须是数组。"))
            continue
        if len(value) < minimum_items[field]:
            issues.append(_issue(f"$.{field}", "ARRAY_TOO_SHORT", "数组缺少必要条目。"))
        if len(value) > 30:
            issues.append(_issue(f"$.{field}", "ARRAY_TOO_LONG", "数组最多 30 项。"))
        seen: set[str] = set()
        for index, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                issues.append(_issue(f"$.{field}[{index}]", "NON_EMPTY_STRING_REQUIRED", "数组项必须是非空字符串。"))
                continue
            if len(item) > 500:
                issues.append(_issue(f"$.{field}[{index}]", "STRING_TOO_LONG", "数组项超过长度限制。"))
            if item in seen:
                issues.append(_issue(f"$.{field}[{index}]", "DUPLICATE_ITEM", "数组项不能重复。"))
            seen.add(item)
            if field == "locked_fields" and item not in EDITABLE_FIELDS:
                issues.append(_issue(f"$.{field}[{index}]", "INVALID_LOCK_FIELD", "锁定字段不允许编辑或不存在。"))

    evidence = payload.get("evidence")
    if evidence is not None:
        if not isinstance(evidence, list):
            issues.append(_issue("$.evidence", "ARRAY_REQUIRED", "evidence 必须是数组。"))
        elif not evidence:
            issues.append(_issue("$.evidence", "EVIDENCE_REQUIRED", "至少需要一条引用依据。"))
        else:
            if len(evidence) > 20:
                issues.append(_issue("$.evidence", "ARRAY_TOO_LONG", "evidence 最多 20 项。"))
            for index, item in enumerate(evidence[:20]):
                path = f"$.evidence[{index}]"
                if not isinstance(item, dict):
                    issues.append(_issue(path, "OBJECT_REQUIRED", "证据项必须是对象。"))
                    continue
                unknown = set(item) - {"source", "quote", "supports"}
                for field in sorted(unknown):
                    issues.append(_issue(f"{path}.{field}", "UNKNOWN_FIELD", "证据项包含未知字段。"))
                source = item.get("source")
                quote = item.get("quote")
                supports = item.get("supports")
                if source not in {"original_poem", "content_notes", "human_reference"}:
                    issues.append(_issue(f"{path}.source", "INVALID_EVIDENCE_SOURCE", "证据来源枚举无效。"))
                if not isinstance(quote, str) or not quote.strip():
                    issues.append(_issue(f"{path}.quote", "EVIDENCE_QUOTE_REQUIRED", "证据必须包含非空引用。"))
                elif source == "original_poem" and source_text and quote not in source_text:
                    issues.append(_issue(f"{path}.quote", "QUOTE_NOT_IN_SOURCE", "原诗引用无法在当前 ContentVersion 中定位。"))
                if not isinstance(supports, list) or not supports:
                    issues.append(_issue(f"{path}.supports", "EVIDENCE_SUPPORT_REQUIRED", "证据必须声明支持的字段。"))
                else:
                    seen_supports: set[str] = set()
                    for support_index, support in enumerate(supports):
                        if support not in EVIDENCE_SUPPORT_FIELDS:
                            issues.append(_issue(f"{path}.supports[{support_index}]", "INVALID_SUPPORT_FIELD", "证据支持字段无效。"))
                        if support in seen_supports:
                            issues.append(_issue(f"{path}.supports[{support_index}]", "DUPLICATE_ITEM", "证据支持字段不能重复。"))
                        seen_supports.add(support)

    confidence = payload.get("confidence")
    low_confidence_fields: list[str] = []
    if confidence is not None:
        if not isinstance(confidence, dict):
            issues.append(_issue("$.confidence", "OBJECT_REQUIRED", "confidence 必须是对象。"))
        else:
            for field in sorted(set(confidence) - set(CONFIDENCE_FIELDS)):
                issues.append(_issue(f"$.confidence.{field}", "UNKNOWN_FIELD", "置信度字段不在契约中。"))
            for field in CONFIDENCE_FIELDS:
                item = confidence.get(field)
                path = f"$.confidence.{field}"
                if not isinstance(item, dict):
                    issues.append(_issue(path, "CONFIDENCE_REQUIRED", "缺少字段置信度说明。"))
                    continue
                unknown = set(item) - {"score", "level", "basis", "requires_review"}
                for key in sorted(unknown):
                    issues.append(_issue(f"{path}.{key}", "UNKNOWN_FIELD", "置信度项包含未知字段。"))
                score = item.get("score")
                if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= float(score) <= 1:
                    issues.append(_issue(f"{path}.score", "INVALID_CONFIDENCE_SCORE", "score 必须是 0–1 数值。"))
                    continue
                expected_level = confidence_level(float(score))
                if item.get("level") != expected_level:
                    issues.append(_issue(f"{path}.level", "CONFIDENCE_LEVEL_MISMATCH", "level 与 score 区间不一致。"))
                if not isinstance(item.get("basis"), str) or not item.get("basis", "").strip():
                    issues.append(_issue(f"{path}.basis", "CONFIDENCE_BASIS_REQUIRED", "必须说明置信度依据。"))
                if not isinstance(item.get("requires_review"), bool):
                    issues.append(_issue(f"{path}.requires_review", "BOOLEAN_REQUIRED", "requires_review 必须是布尔值。"))
                if expected_level == "low":
                    low_confidence_fields.append(field)
                    if item.get("requires_review") is not True:
                        issues.append(_issue(f"{path}.requires_review", "LOW_CONFIDENCE_REVIEW_REQUIRED", "低置信度字段必须进入人工复核。"))
    uncertainties = payload.get("uncertainties")
    if low_confidence_fields and isinstance(uncertainties, list) and not uncertainties:
        issues.append(_issue("$.uncertainties", "LOW_CONFIDENCE_UNCERTAINTY_REQUIRED", "低置信度字段必须给出不确定点。"))
    return issues


def _unique_clean_strings(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()[:500]
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def repair_requirement_card(payload: Any) -> Any:
    """Perform exactly one deterministic, non-semantic shape repair."""

    if not isinstance(payload, dict):
        return payload
    repaired = {key: deepcopy(value) for key, value in payload.items() if key in ALLOWED_FIELDS}
    for field in TEXT_FIELDS:
        if isinstance(repaired.get(field), str):
            limit = 2000 if field in {"composition", "editor_note"} else 500
            repaired[field] = repaired[field].strip()[:limit]
    repaired.setdefault("editor_note", "")
    repaired.setdefault("locked_fields", [])
    for field in LIST_FIELDS:
        if field in repaired:
            repaired[field] = _unique_clean_strings(repaired[field])

    evidence = repaired.get("evidence")
    if isinstance(evidence, list):
        normalized_evidence = []
        for item in evidence[:20]:
            if not isinstance(item, dict):
                continue
            normalized = {
                "source": item.get("source"),
                "quote": item.get("quote", "").strip()[:2000]
                if isinstance(item.get("quote"), str)
                else item.get("quote"),
                "supports": _unique_clean_strings(item.get("supports")),
            }
            normalized_evidence.append(normalized)
        repaired["evidence"] = normalized_evidence

    confidence = repaired.get("confidence")
    if not isinstance(confidence, dict):
        confidence = {}
    normalized_confidence: dict[str, Any] = {}
    for field in CONFIDENCE_FIELDS:
        item = confidence.get(field)
        item = item if isinstance(item, dict) else {}
        raw_score = item.get("score", 0)
        try:
            score = max(0.0, min(float(raw_score), 1.0))
        except (TypeError, ValueError):
            score = 0.0
        level = confidence_level(score)
        basis = item.get("basis")
        if not isinstance(basis, str) or not basis.strip():
            basis = "生成器未提供可靠依据，必须人工复核"
        normalized_confidence[field] = {
            "score": round(score, 3),
            "level": level,
            "basis": basis.strip()[:500],
            "requires_review": True
            if level == "low"
            else bool(item.get("requires_review", False)),
        }
    repaired["confidence"] = normalized_confidence
    return repaired


def validate_with_single_repair(
    payload: Any,
    *,
    source_text: str = "",
) -> tuple[Any, dict[str, Any]]:
    initial_issues = validate_requirement_card(payload, source_text=source_text)
    if not initial_issues:
        return payload, {
            "schema_version": SCHEMA_VERSION,
            "valid": True,
            "repair_attempts": 0,
            "initial_issues": [],
            "final_issues": [],
        }
    repaired = repair_requirement_card(payload)
    final_issues = validate_requirement_card(repaired, source_text=source_text)
    return repaired, {
        "schema_version": SCHEMA_VERSION,
        "valid": not final_issues,
        "repair_attempts": 1,
        "initial_issues": initial_issues,
        "final_issues": final_issues,
    }
