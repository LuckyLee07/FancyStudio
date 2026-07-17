"""Visual QC adapters with a safe manual-review fallback.

Real images are reviewed through the OpenAI Responses API only when the image
provider is OpenAI and visual QC is enabled. Demo SVGs use an explicitly
synthetic reviewer so the local SOP can be exercised without presenting demo
scores as real model judgement.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from review_schema import (
    REVIEW_RESULT_SCHEMA_VERSION,
    review_result_schema_document,
    validate_review_result,
)


VISUAL_REVIEWER_VERSION = "vision-review-v1"
DEFAULT_VISION_MODEL = "gpt-5.6-luna"


class VisualReviewError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _enabled() -> bool:
    return os.getenv("TANG_VISION_QC_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def visual_reviewer_status(image_provider: str) -> dict[str, Any]:
    enabled = _enabled()
    configured = bool(os.getenv("OPENAI_API_KEY"))
    if image_provider == "demo":
        return {
            "status": "synthetic_demo",
            "enabled": True,
            "configured": True,
            "model": "synthetic-demo-reviewer",
            "reviewer_version": VISUAL_REVIEWER_VERSION,
            "real_visual_review": False,
        }
    return {
        "status": (
            "ready"
            if enabled and configured
            else "disabled"
            if not enabled
            else "configuration_required"
        ),
        "enabled": enabled,
        "configured": configured,
        "model": os.getenv("OPENAI_VISION_QC_MODEL", DEFAULT_VISION_MODEL),
        "reviewer_version": VISUAL_REVIEWER_VERSION,
        "real_visual_review": enabled and configured,
    }


def _compact_context(context: dict[str, Any]) -> dict[str, Any]:
    poem = context.get("poem") if isinstance(context.get("poem"), dict) else {}
    requirement = (
        context.get("requirement")
        if isinstance(context.get("requirement"), dict)
        else {}
    )
    direction = (
        context.get("direction")
        if isinstance(context.get("direction"), dict)
        else {}
    )
    style = context.get("style") if isinstance(context.get("style"), dict) else {}
    art_bible = (
        style.get("art_bible") if isinstance(style.get("art_bible"), dict) else {}
    )
    art_bible_content = (
        art_bible.get("content")
        if isinstance(art_bible.get("content"), dict)
        else art_bible
    )
    return {
        "poem": {
            "title": str(poem.get("title") or "")[:100],
            "author": str(poem.get("author") or "")[:100],
            "dynasty": str(poem.get("dynasty") or "")[:40],
            "lines": [str(item)[:100] for item in (poem.get("lines") or [])[:12]],
        },
        "requirement": {
            field: requirement.get(field)
            for field in (
                "theme",
                "mood",
                "time_and_place",
                "subject",
                "core_imagery",
                "must_have",
                "avoid",
                "historical_risks",
                "uncertainties",
            )
        },
        "direction": {
            field: direction.get(field)
            for field in (
                "type",
                "visual_thesis",
                "subject",
                "scene",
                "shot_scale",
                "narrative_mode",
                "composition",
                "lighting",
                "palette",
                "whitespace",
                "text_safe_area",
                "risk_note",
                "interpretation_layers",
            )
        },
        "style": {
            "style_id": style.get("style_id") or style.get("id"),
            "semantic_version": style.get("semantic_version"),
            "description": style.get("description"),
            "visual_traits": style.get("visual_traits"),
            "character_design": style.get("character_design"),
            "palette": style.get("palette"),
            "avoid": style.get("avoid"),
            "risks": style.get("risks"),
        },
        "art_bible": {
            field: (
                art_bible.get(field)
                if field == "semantic_version"
                else art_bible_content.get(field)
            )
            for field in (
                "semantic_version",
                "palette_rules",
                "line_rules",
                "character_proportions",
                "spatial_rules",
                "material_rules",
                "text_prohibitions",
                "historical_boundaries",
            )
        },
    }


def _input_hash(
    path: Path,
    context: dict[str, Any],
    model: str,
    policy_version_id: str,
) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    digest.update(
        json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    digest.update(model.encode("utf-8"))
    digest.update(policy_version_id.encode("utf-8"))
    digest.update(VISUAL_REVIEWER_VERSION.encode("utf-8"))
    return digest.hexdigest()


def _demo_review(
    path: Path,
    context: dict[str, Any],
    policy_version_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    compact = _compact_context(context)
    digest = _input_hash(path, compact, "synthetic-demo-reviewer", policy_version_id)
    variance = int(digest[:2], 16) % 8
    base = 80 + variance
    imagery = compact["requirement"].get("core_imagery") or []
    observed = ["演示渲染器的抽象山水层次", "与风格色板一致的合成配色"]
    observed.extend(str(item)[:120] for item in imagery[:2])
    result = {
        "schema_version": REVIEW_RESULT_SCHEMA_VERSION,
        "overall_score": base,
        "scores": {
            "safety": 100,
            "technical_integrity": 92,
            "poem_relevance": base,
            "style_match": min(96, base + 5),
            "historical_plausibility": 78,
            "composition": min(94, base + 3),
            "character_quality": 90,
            "series_consistency": min(95, base + 4),
        },
        "has_unexpected_text": False,
        "hard_fail_reasons": [],
        "problems": [],
        "decision": "recommended" if base >= 85 else "candidate",
        "confidence": 0.9,
        "evidence": {
            "observed_elements": observed[:12],
            "missing_required_elements": [],
            "uncertain_elements": ["演示图仅用于验证流程，不代表真实视觉模型判断"],
        },
        "reviewer_version": "synthetic-demo-review-v1",
    }
    return validate_review_result(result), {
        "reviewer_kind": "synthetic_demo",
        "reviewer_model": "synthetic-demo-reviewer",
        "input_hash": digest,
        "usage": {},
        "estimated_cost": 0,
    }


def _review_prompt(context: dict[str, Any]) -> str:
    context_json = json.dumps(
        context,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"""你是唐诗教育出版插图的视觉质检员。请只依据图片中可观察到的证据和给定生产合同评分。

评分维度：安全、技术完整、诗意相关、风格匹配、历史合理、构图、人物质量、系列一致。
必须遵守：
1. 不把 Prompt 中写了某物当成图片中已经出现；observed_elements 只能写看得见的内容。
2. must_have 缺失要写入 missing_required_elements；不确定的服饰、建筑、器物写入 uncertain_elements。
3. 历史合理性只是风险判断，不是学术认证。证据不足时降低 confidence 并选择 manual_review。
4. 发现乱码、字母、水印或 Logo 时 has_unexpected_text=true，并给出 UNEXPECTED_TEXT 或 WATERMARK_OR_LOGO。
5. 严重偏题、明显现代物件、严重人物畸形必须说明可见证据，禁止只给抽象结论。
6. reviewer_version 固定为 openai-vision-review-v1。

生产合同：
{context_json}"""


def _response_text(response: dict[str, Any]) -> str:
    for output in response.get("output") or []:
        if output.get("type") != "message":
            continue
        for content in output.get("content") or []:
            if content.get("type") == "refusal":
                raise VisualReviewError("VISION_REVIEW_REFUSED", "视觉质检模型拒绝了本次检查。")
            if content.get("type") == "output_text" and content.get("text"):
                return str(content["text"])
    raise VisualReviewError("VISION_REVIEW_EMPTY", "视觉质检接口没有返回结构化结果。")


def _openai_review(
    path: Path,
    mime_type: str,
    context: dict[str, Any],
    policy_version_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise VisualReviewError("VISION_REVIEW_NOT_CONFIGURED", "未配置视觉质检服务。")
    if mime_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        raise VisualReviewError("VISION_REVIEW_FORMAT_UNSUPPORTED", "当前图片格式不支持视觉模型质检。")
    payload_bytes = path.read_bytes()
    if len(payload_bytes) > 20_000_000:
        raise VisualReviewError("VISION_REVIEW_FILE_TOO_LARGE", "图片超过视觉质检的本地安全上限。")
    compact = _compact_context(context)
    model = os.getenv("OPENAI_VISION_QC_MODEL", DEFAULT_VISION_MODEL)
    digest = _input_hash(path, compact, model, policy_version_id)
    schema = review_result_schema_document()
    schema.pop("$schema", None)
    schema.pop("$id", None)
    body = {
        "model": model,
        "store": False,
        "safety_identifier": "tang-poem-studio-local-qc",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": _review_prompt(compact)},
                    {
                        "type": "input_image",
                        "image_url": (
                            f"data:{mime_type};base64,"
                            + base64.b64encode(payload_bytes).decode("ascii")
                        ),
                        "detail": "high",
                    },
                ],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "review_result_v1",
                "strict": True,
                "schema": schema,
            }
        },
        "max_output_tokens": 1800,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "tang-poem-studio/vision-qc",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=150) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        code = (
            "VISION_REVIEW_RATE_LIMITED"
            if exc.code == 429
            else "VISION_REVIEW_PROVIDER_UNAVAILABLE"
            if exc.code >= 500
            else "VISION_REVIEW_REQUEST_REJECTED"
        )
        raise VisualReviewError(code, "视觉质检接口暂时不可用。") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise VisualReviewError("VISION_REVIEW_NETWORK_ERROR", "无法连接视觉质检接口。") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise VisualReviewError("VISION_REVIEW_BAD_RESPONSE", "视觉质检接口响应无法解析。") from exc
    try:
        parsed = json.loads(_response_text(response_payload))
        result = validate_review_result(parsed)
    except json.JSONDecodeError as exc:
        raise VisualReviewError("VISION_REVIEW_BAD_RESPONSE", "视觉质检结果不是有效 JSON。") from exc
    usage = response_payload.get("usage") if isinstance(response_payload.get("usage"), dict) else {}
    input_tokens = max(0, int(usage.get("input_tokens") or 0))
    output_tokens = max(0, int(usage.get("output_tokens") or 0))
    try:
        input_rate = max(0.0, float(os.getenv("TANG_VISION_QC_INPUT_COST_PER_M", "0")))
        output_rate = max(0.0, float(os.getenv("TANG_VISION_QC_OUTPUT_COST_PER_M", "0")))
    except ValueError:
        input_rate = output_rate = 0.0
    estimated_cost = input_tokens * input_rate / 1_000_000 + output_tokens * output_rate / 1_000_000
    return result, {
        "reviewer_kind": "openai_vision",
        "reviewer_model": str(response_payload.get("model") or model),
        "input_hash": digest,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": max(0, int(usage.get("total_tokens") or 0)),
        },
        "estimated_cost": round(estimated_cost, 8),
        "response_id": str(response_payload.get("id") or "")[:100],
    }


def review_image(
    path: Path,
    *,
    image_provider: str,
    context: dict[str, Any],
    policy_version_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Return a structured result or safe unavailability metadata."""

    path = Path(path)
    if image_provider == "demo":
        return _demo_review(path, context, policy_version_id)
    if not _enabled():
        return None, {
            "reviewer_kind": "unavailable",
            "unavailable_code": "visual_review_disabled",
        }
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    try:
        return _openai_review(path, mime_type, context, policy_version_id)
    except VisualReviewError as exc:
        return None, {
            "reviewer_kind": "unavailable",
            "reviewer_model": os.getenv("OPENAI_VISION_QC_MODEL", DEFAULT_VISION_MODEL),
            "unavailable_code": exc.code.lower(),
        }
