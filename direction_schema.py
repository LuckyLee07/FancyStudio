"""DirectionProposal v1 contract and three-direction diversity gate.

One generation run is treated as an atomic set of narrative, atmospheric and
symbolic proposals. Shape repair is deterministic and runs at most once; it
never invents missing visual intent or evidence.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from itertools import combinations
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "direction-proposal/v1"
GENERATOR_VERSION = "local-direction-planner/v2"
SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "direction-proposal.schema.json"

DIRECTION_TYPES = ("narrative", "atmospheric", "symbolic")
NARRATIVE_MODES = ("narrative", "atmosphere", "symbolism")
SUBJECT_MODES = ("human_focus", "environment_focus", "object_focus")
SHOT_SCALES = ("wide", "medium", "close")
TYPE_NARRATIVE_MODE = {
    "narrative": "narrative",
    "atmospheric": "atmosphere",
    "symbolic": "symbolism",
}

TEXT_FIELDS = (
    "title",
    "visual_thesis",
    "subject",
    "scene",
    "shot",
    "foreground",
    "midground",
    "background",
    "action",
    "composition",
    "lighting",
    "palette",
    "whitespace",
    "text_safe_area",
    "risk_note",
    "art_director_note",
)
LIST_FIELDS = ("preserve", "avoid", "locked_fields")
REQUIRED_FIELDS = (
    "type",
    "title",
    "visual_thesis",
    "subject",
    "subject_mode",
    "scene",
    "shot",
    "shot_scale",
    "narrative_mode",
    "foreground",
    "midground",
    "background",
    "action",
    "composition",
    "lighting",
    "palette",
    "whitespace",
    "preserve",
    "avoid",
    "text_safe_area",
    "risk_note",
    "interpretation_layers",
    "art_director_note",
    "locked_fields",
)
ALLOWED_FIELDS = set(REQUIRED_FIELDS)
EDITABLE_FIELDS = ALLOWED_FIELDS - {"type", "locked_fields"}
LAYER_FIELDS = {
    "poem_facts": ("claim", "evidence_quote"),
    "reasonable_inferences": ("claim", "basis"),
    "creative_choices": ("claim", "purpose"),
}
DIVERSITY_AXES = ("subject_mode", "shot_scale", "narrative_mode")


def schema_document() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _issue(path: str, code: str, message: str) -> dict[str, str]:
    return {"path": path, "code": code, "message": message}


def _normalized(value: Any) -> str:
    return re.sub(r"[\W_]+", "", str(value or "").lower(), flags=re.UNICODE)


def validate_direction_proposal(
    payload: Any,
    *,
    source_text: str = "",
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if not isinstance(payload, dict):
        return [_issue("$", "TYPE_OBJECT_REQUIRED", "DirectionProposal 必须是 JSON 对象。")]

    for field in sorted(set(payload) - ALLOWED_FIELDS):
        issues.append(_issue(f"$.{field}", "UNKNOWN_FIELD", "字段不在 DirectionProposal v1 中。"))
    for field in REQUIRED_FIELDS:
        if field not in payload:
            issues.append(_issue(f"$.{field}", "REQUIRED_FIELD_MISSING", "缺少必填字段。"))

    if payload.get("type") not in DIRECTION_TYPES:
        issues.append(_issue("$.type", "INVALID_DIRECTION_TYPE", "方向类型必须是叙事、意境或象征。"))
    if payload.get("subject_mode") not in SUBJECT_MODES:
        issues.append(_issue("$.subject_mode", "INVALID_SUBJECT_MODE", "主体模式枚举无效。"))
    if payload.get("shot_scale") not in SHOT_SCALES:
        issues.append(_issue("$.shot_scale", "INVALID_SHOT_SCALE", "景别尺度枚举无效。"))
    if payload.get("narrative_mode") not in NARRATIVE_MODES:
        issues.append(_issue("$.narrative_mode", "INVALID_NARRATIVE_MODE", "视觉叙事模式枚举无效。"))
    expected_mode = TYPE_NARRATIVE_MODE.get(payload.get("type"))
    if expected_mode and payload.get("narrative_mode") != expected_mode:
        issues.append(_issue("$.narrative_mode", "TYPE_MODE_MISMATCH", "方向类型与视觉叙事模式不一致。"))

    for field in TEXT_FIELDS:
        if field not in payload:
            continue
        value = payload[field]
        if not isinstance(value, str):
            issues.append(_issue(f"$.{field}", "STRING_REQUIRED", "字段必须是字符串。"))
            continue
        if field != "art_director_note" and not value.strip():
            issues.append(_issue(f"$.{field}", "NON_EMPTY_REQUIRED", "字段不能为空。"))
        if len(value) > (2000 if field in {"composition", "art_director_note"} else 1000):
            issues.append(_issue(f"$.{field}", "STRING_TOO_LONG", "字段超过长度限制。"))

    for field in LIST_FIELDS:
        if field not in payload:
            continue
        value = payload[field]
        if not isinstance(value, list):
            issues.append(_issue(f"$.{field}", "ARRAY_REQUIRED", "字段必须是数组。"))
            continue
        if field in {"preserve", "avoid"} and not value:
            issues.append(_issue(f"$.{field}", "ARRAY_TOO_SHORT", "数组至少需要一项。"))
        if len(value) > 30:
            issues.append(_issue(f"$.{field}", "ARRAY_TOO_LONG", "数组最多 30 项。"))
        seen: set[str] = set()
        for index, item in enumerate(value):
            path = f"$.{field}[{index}]"
            if not isinstance(item, str) or not item.strip():
                issues.append(_issue(path, "NON_EMPTY_STRING_REQUIRED", "数组项必须是非空字符串。"))
                continue
            if len(item) > 500:
                issues.append(_issue(path, "STRING_TOO_LONG", "数组项超过长度限制。"))
            if item in seen:
                issues.append(_issue(path, "DUPLICATE_ITEM", "数组项不能重复。"))
            seen.add(item)
            if field == "locked_fields" and item not in EDITABLE_FIELDS:
                issues.append(_issue(path, "INVALID_LOCK_FIELD", "锁定字段不存在或不可锁定。"))

    layers = payload.get("interpretation_layers")
    if layers is not None:
        if not isinstance(layers, dict):
            issues.append(_issue("$.interpretation_layers", "OBJECT_REQUIRED", "解释分层必须是对象。"))
        else:
            for key in sorted(set(layers) - set(LAYER_FIELDS)):
                issues.append(_issue(f"$.interpretation_layers.{key}", "UNKNOWN_FIELD", "解释分层包含未知类别。"))
            for layer, fields in LAYER_FIELDS.items():
                items = layers.get(layer)
                layer_path = f"$.interpretation_layers.{layer}"
                if not isinstance(items, list) or not items:
                    issues.append(_issue(layer_path, "LAYER_ITEMS_REQUIRED", "每个解释层至少需要一项。"))
                    continue
                if len(items) > 20:
                    issues.append(_issue(layer_path, "ARRAY_TOO_LONG", "每个解释层最多 20 项。"))
                for index, item in enumerate(items[:20]):
                    path = f"{layer_path}[{index}]"
                    if not isinstance(item, dict):
                        issues.append(_issue(path, "OBJECT_REQUIRED", "解释条目必须是对象。"))
                        continue
                    for key in sorted(set(item) - set(fields)):
                        issues.append(_issue(f"{path}.{key}", "UNKNOWN_FIELD", "解释条目包含未知字段。"))
                    for field in fields:
                        value = item.get(field)
                        if not isinstance(value, str) or not value.strip():
                            issues.append(_issue(f"{path}.{field}", "NON_EMPTY_REQUIRED", "解释条目字段不能为空。"))
                        elif len(value) > 1000:
                            issues.append(_issue(f"{path}.{field}", "STRING_TOO_LONG", "解释条目超过长度限制。"))
                    if layer == "poem_facts":
                        quote = item.get("evidence_quote")
                        if isinstance(quote, str) and source_text and quote not in source_text:
                            issues.append(_issue(f"{path}.evidence_quote", "QUOTE_NOT_IN_SOURCE", "诗文事实引用无法在当前 ContentVersion 中定位。"))
    return issues


def diversity_report(proposals: Any) -> dict[str, Any]:
    if not isinstance(proposals, list):
        return {"valid": False, "pairs": [], "minimum_axis_differences": 0}
    pairs = []
    minimum = len(DIVERSITY_AXES)
    for left, right in combinations(proposals, 2):
        left_type = left.get("type") if isinstance(left, dict) else "unknown"
        right_type = right.get("type") if isinstance(right, dict) else "unknown"
        differences = [
            axis
            for axis in DIVERSITY_AXES
            if isinstance(left, dict)
            and isinstance(right, dict)
            and left.get(axis) != right.get(axis)
        ]
        minimum = min(minimum, len(differences))
        pairs.append(
            {
                "pair": [left_type, right_type],
                "different_axes": differences,
                "difference_count": len(differences),
                "valid": len(differences) >= 2,
            }
        )
    if not pairs:
        minimum = 0
    return {
        "valid": len(proposals) == 3 and bool(pairs) and all(item["valid"] for item in pairs),
        "pairs": pairs,
        "minimum_axis_differences": minimum,
    }


def validate_direction_set(
    proposals: Any,
    *,
    source_text: str = "",
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    issues: list[dict[str, str]] = []
    if not isinstance(proposals, list):
        return (
            [_issue("$", "ARRAY_REQUIRED", "三方向输出必须是 JSON 数组。")],
            diversity_report(proposals),
        )
    if len(proposals) != 3:
        issues.append(_issue("$", "EXACTLY_THREE_REQUIRED", "每首诗必须原子生成恰好三个方向。"))
    for index, proposal in enumerate(proposals[:3]):
        for item in validate_direction_proposal(proposal, source_text=source_text):
            issues.append({**item, "path": f"$[{index}]{item['path'][1:]}"})

    types = [item.get("type") for item in proposals if isinstance(item, dict)]
    if len(types) != len(set(types)):
        issues.append(_issue("$", "DUPLICATE_DIRECTION_TYPE", "三个方向的类型不能重复。"))
    if set(types) != set(DIRECTION_TYPES):
        issues.append(_issue("$", "DIRECTION_TYPE_SET_MISMATCH", "方向集合必须同时包含叙事、意境和象征。"))

    titles = [_normalized(item.get("title")) for item in proposals if isinstance(item, dict)]
    if "" in titles or len(titles) != len(set(titles)):
        issues.append(_issue("$", "DUPLICATE_DIRECTION_TITLE", "三个方向必须使用不同标题。"))
    compositions = [
        _normalized(item.get("composition")) for item in proposals if isinstance(item, dict)
    ]
    if "" in compositions or len(compositions) != len(set(compositions)):
        issues.append(_issue("$", "DUPLICATE_COMPOSITION", "三个方向不能复用同一构图。"))

    diversity = diversity_report(proposals)
    for pair in diversity["pairs"]:
        if not pair["valid"]:
            issues.append(
                _issue(
                    "$",
                    "DIRECTION_DIVERSITY_INSUFFICIENT",
                    f"{pair['pair'][0]} 与 {pair['pair'][1]} 在主体、景别、视觉叙事三轴中至少需要两项不同。",
                )
            )
    if not any(
        isinstance(item, dict) and item.get("subject_mode") == "environment_focus"
        for item in proposals
    ):
        issues.append(_issue("$", "HUMAN_REDUCTION_REQUIRED", "至少一个方向必须弱化人物、以环境为主体。"))
    unsafe_markers = {"none", "no", "无", "不留", "无安全区"}
    if not any(
        isinstance(item, dict)
        and _normalized(item.get("text_safe_area")) not in unsafe_markers
        and _normalized(item.get("text_safe_area"))
        for item in proposals
    ):
        issues.append(_issue("$", "TEXT_SAFE_AREA_REQUIRED", "至少一个方向必须提供明确文字安全区。"))
    return issues, diversity


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


def repair_direction_proposal(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    repaired = {key: deepcopy(value) for key, value in payload.items() if key in ALLOWED_FIELDS}
    for field in TEXT_FIELDS:
        if isinstance(repaired.get(field), str):
            limit = 2000 if field in {"composition", "art_director_note"} else 1000
            repaired[field] = repaired[field].strip()[:limit]
    repaired.setdefault("art_director_note", "")
    repaired.setdefault("locked_fields", [])
    for field in LIST_FIELDS:
        if field in repaired:
            repaired[field] = _unique_clean_strings(repaired[field])
    layers = repaired.get("interpretation_layers")
    if isinstance(layers, dict):
        normalized_layers: dict[str, list[dict[str, str]]] = {}
        for layer, fields in LAYER_FIELDS.items():
            normalized_items = []
            items = layers.get(layer)
            if not isinstance(items, list):
                continue
            for item in items[:20]:
                if not isinstance(item, dict):
                    continue
                normalized_items.append(
                    {
                        field: item.get(field, "").strip()[:1000]
                        if isinstance(item.get(field), str)
                        else item.get(field)
                        for field in fields
                    }
                )
            normalized_layers[layer] = normalized_items
        repaired["interpretation_layers"] = normalized_layers
    return repaired


def validate_with_single_repair(
    proposals: Any,
    *,
    source_text: str = "",
) -> tuple[Any, dict[str, Any]]:
    initial_issues, initial_diversity = validate_direction_set(
        proposals,
        source_text=source_text,
    )
    if not initial_issues:
        return proposals, {
            "schema_version": SCHEMA_VERSION,
            "valid": True,
            "repair_attempts": 0,
            "initial_issues": [],
            "final_issues": [],
            "diversity": initial_diversity,
        }
    repaired = (
        [repair_direction_proposal(item) for item in proposals]
        if isinstance(proposals, list)
        else proposals
    )
    final_issues, final_diversity = validate_direction_set(
        repaired,
        source_text=source_text,
    )
    return repaired, {
        "schema_version": SCHEMA_VERSION,
        "valid": not final_issues,
        "repair_attempts": 1,
        "initial_issues": initial_issues,
        "final_issues": final_issues,
        "diversity": final_diversity,
    }
