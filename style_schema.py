"""Runtime contracts for the global Art Bible and versioned style packs."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


ART_BIBLE_SCHEMA_VERSION = "art-bible/v1"
STYLE_PACK_SCHEMA_VERSION = "style-pack/v1"
ART_BIBLE_SCHEMA_PATH = (
    Path(__file__).resolve().parent / "schemas" / "art-bible.schema.json"
)
STYLE_PACK_SCHEMA_PATH = (
    Path(__file__).resolve().parent / "schemas" / "style-pack.schema.json"
)

ART_BIBLE_LIST_FIELDS = (
    "palette_rules",
    "line_rules",
    "character_proportion_rules",
    "spatial_rules",
    "material_rules",
    "text_prohibitions",
    "historical_boundaries",
)
ART_BIBLE_REQUIRED_FIELDS = (*ART_BIBLE_LIST_FIELDS, "benchmark_policy")
BENCHMARK_POLICY_FIELDS = (
    "benchmark_poem_count",
    "min_poems_per_release",
    "min_samples_per_poem",
    "min_style_match_score",
    "max_off_topic_rate",
)

STYLE_VISUAL_TRAITS = (
    "line",
    "texture",
    "lighting",
    "contrast",
    "saturation",
    "whitespace",
)
STYLE_CHARACTER_FIELDS = ("proportion", "expression", "costume")
STYLE_LIST_FIELDS = (
    "palette",
    "applicable_topics",
    "avoid",
    "risks",
    "positive_examples",
    "negative_examples",
)
STYLE_REQUIRED_FIELDS = (
    "style_id",
    "name",
    "semantic_version",
    "description",
    "prompt_fragment",
    "release_notes",
    "art_bible_version_id",
    "visual_traits",
    "character_design",
    *STYLE_LIST_FIELDS,
    "settings",
)


def art_bible_schema_document() -> dict[str, Any]:
    return json.loads(ART_BIBLE_SCHEMA_PATH.read_text(encoding="utf-8"))


def style_pack_schema_document() -> dict[str, Any]:
    return json.loads(STYLE_PACK_SCHEMA_PATH.read_text(encoding="utf-8"))


def _issue(path: str, code: str, message: str) -> dict[str, str]:
    return {"path": path, "code": code, "message": message}


def _validate_string_list(
    payload: dict[str, Any],
    field: str,
    *,
    maximum: int = 30,
) -> list[dict[str, str]]:
    value = payload.get(field)
    path = f"$.{field}"
    if not isinstance(value, list) or not value:
        return [_issue(path, "NON_EMPTY_ARRAY_REQUIRED", "字段必须是非空数组。")]
    issues: list[dict[str, str]] = []
    if len(value) > maximum:
        issues.append(_issue(path, "ARRAY_TOO_LONG", f"字段最多包含 {maximum} 项。"))
    seen: set[str] = set()
    for index, item in enumerate(value[:maximum]):
        item_path = f"{path}[{index}]"
        if not isinstance(item, str) or not item.strip():
            issues.append(_issue(item_path, "NON_EMPTY_STRING_REQUIRED", "数组项必须是非空字符串。"))
            continue
        normalized = item.strip()
        if len(normalized) > 500:
            issues.append(_issue(item_path, "STRING_TOO_LONG", "数组项超过 500 字符。"))
        if normalized in seen:
            issues.append(_issue(item_path, "DUPLICATE_ITEM", "数组项不能重复。"))
        seen.add(normalized)
    return issues


def validate_art_bible(payload: Any) -> list[dict[str, str]]:
    if not isinstance(payload, dict):
        return [_issue("$", "TYPE_OBJECT_REQUIRED", "Art Bible 必须是 JSON 对象。")]
    issues: list[dict[str, str]] = []
    allowed = set(ART_BIBLE_REQUIRED_FIELDS)
    for field in sorted(set(payload) - allowed):
        issues.append(_issue(f"$.{field}", "UNKNOWN_FIELD", "Art Bible 包含未知字段。"))
    for field in ART_BIBLE_LIST_FIELDS:
        issues.extend(_validate_string_list(payload, field))
    policy = payload.get("benchmark_policy")
    if not isinstance(policy, dict):
        issues.append(_issue("$.benchmark_policy", "OBJECT_REQUIRED", "基准策略必须是对象。"))
        return issues
    for field in sorted(set(policy) - set(BENCHMARK_POLICY_FIELDS)):
        issues.append(_issue(f"$.benchmark_policy.{field}", "UNKNOWN_FIELD", "基准策略包含未知字段。"))
    for field in BENCHMARK_POLICY_FIELDS:
        if field not in policy:
            issues.append(_issue(f"$.benchmark_policy.{field}", "REQUIRED_FIELD_MISSING", "缺少基准策略字段。"))
    integer_limits = {
        "benchmark_poem_count": (12, 100),
        "min_poems_per_release": (5, 100),
        "min_samples_per_poem": (4, 9),
    }
    for field, (minimum, maximum) in integer_limits.items():
        value = policy.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            issues.append(_issue(f"$.benchmark_policy.{field}", "INVALID_INTEGER_RANGE", f"字段必须是 {minimum}–{maximum} 的整数。"))
    score = policy.get("min_style_match_score")
    if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= score <= 100:
        issues.append(_issue("$.benchmark_policy.min_style_match_score", "INVALID_SCORE", "风格匹配分必须在 0–100。"))
    off_topic = policy.get("max_off_topic_rate")
    if not isinstance(off_topic, (int, float)) or isinstance(off_topic, bool) or not 0 <= off_topic <= 1:
        issues.append(_issue("$.benchmark_policy.max_off_topic_rate", "INVALID_RATE", "偏题率必须在 0–1。"))
    if (
        isinstance(policy.get("benchmark_poem_count"), int)
        and isinstance(policy.get("min_poems_per_release"), int)
        and policy["min_poems_per_release"] > policy["benchmark_poem_count"]
    ):
        issues.append(_issue("$.benchmark_policy.min_poems_per_release", "MIN_EXCEEDS_POOL", "发布最少诗数不能超过基准池总数。"))
    return issues


def validate_style_pack(payload: Any) -> list[dict[str, str]]:
    if not isinstance(payload, dict):
        return [_issue("$", "TYPE_OBJECT_REQUIRED", "StylePack 必须是 JSON 对象。")]
    issues: list[dict[str, str]] = []
    allowed = set(STYLE_REQUIRED_FIELDS) | {"short_name"}
    for field in sorted(set(payload) - allowed):
        issues.append(_issue(f"$.{field}", "UNKNOWN_FIELD", "StylePack 包含未知字段。"))
    for field in STYLE_REQUIRED_FIELDS:
        if field not in payload:
            issues.append(_issue(f"$.{field}", "REQUIRED_FIELD_MISSING", "缺少必填字段。"))
    style_id = payload.get("style_id")
    if not isinstance(style_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,79}", style_id):
        issues.append(_issue("$.style_id", "INVALID_STYLE_ID", "风格 ID 仅支持小写字母、数字和连字符。"))
    semantic_version = payload.get("semantic_version")
    if not isinstance(semantic_version, str) or not re.fullmatch(
        r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", semantic_version
    ):
        issues.append(_issue("$.semantic_version", "INVALID_SEMVER", "语义版本必须使用 MAJOR.MINOR.PATCH。"))
    for field in (
        "name",
        "description",
        "prompt_fragment",
        "release_notes",
        "art_bible_version_id",
    ):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            issues.append(_issue(f"$.{field}", "NON_EMPTY_REQUIRED", "字段不能为空。"))
        elif len(value) > (4000 if field == "prompt_fragment" else 1000):
            issues.append(_issue(f"$.{field}", "STRING_TOO_LONG", "字段超过长度限制。"))
    palette = payload.get("palette")
    issues.extend(_validate_string_list(payload, "palette", maximum=12))
    if isinstance(palette, list):
        for index, color in enumerate(palette[:12]):
            if isinstance(color, str) and not re.fullmatch(r"#[0-9a-fA-F]{6}", color.strip()):
                issues.append(_issue(f"$.palette[{index}]", "INVALID_HEX_COLOR", "色板必须使用六位 HEX 色值。"))
    for field in STYLE_LIST_FIELDS[1:]:
        issues.extend(_validate_string_list(payload, field))
    traits = payload.get("visual_traits")
    if not isinstance(traits, dict):
        issues.append(_issue("$.visual_traits", "OBJECT_REQUIRED", "视觉特征必须是对象。"))
    else:
        for field in STYLE_VISUAL_TRAITS:
            value = traits.get(field)
            if not isinstance(value, str) or not value.strip():
                issues.append(_issue(f"$.visual_traits.{field}", "NON_EMPTY_REQUIRED", "视觉特征字段不能为空。"))
    character = payload.get("character_design")
    if not isinstance(character, dict):
        issues.append(_issue("$.character_design", "OBJECT_REQUIRED", "人物规范必须是对象。"))
    else:
        for field in STYLE_CHARACTER_FIELDS:
            value = character.get(field)
            if not isinstance(value, str) or not value.strip():
                issues.append(_issue(f"$.character_design.{field}", "NON_EMPTY_REQUIRED", "人物规范字段不能为空。"))
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        issues.append(_issue("$.settings", "OBJECT_REQUIRED", "风格设置必须是对象。"))
    else:
        for field in ("background", "foreground", "accent", "paper"):
            value = settings.get(field)
            if not isinstance(value, str) or not value.strip():
                issues.append(_issue(f"$.settings.{field}", "NON_EMPTY_REQUIRED", "风格设置字段不能为空。"))
    return issues
