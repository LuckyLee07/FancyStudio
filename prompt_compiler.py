"""Deterministic, provider-aware prompt compilation for production tasks."""

from __future__ import annotations

import hashlib
import json
from typing import Any


TEMPLATE_VERSIONS = {
    "demo": "demo-six-segment-v3",
    "openai": "openai-six-segment-v3",
}

RATIO_SPECS = {
    "portrait": {"label": "2:3 portrait", "width": 1024, "height": 1536},
    "square": {"label": "1:1 square", "width": 1024, "height": 1024},
    "landscape": {"label": "3:2 landscape", "width": 1536, "height": 1024},
}


class PromptCompileError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _join(value: Any, fallback: str = "未指定") -> str:
    items = _clean_list(value)
    return "；".join(items) if items else fallback


def _required_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise PromptCompileError("PROMPT_SEGMENT_MISSING", f"Prompt 缺少 {key} 段。")
    return value


def build_segments(payload: dict[str, Any]) -> dict[str, Any]:
    poem = _required_dict(payload, "poem")
    requirement = _required_dict(payload, "requirement")
    direction = _required_dict(payload, "direction")
    style = _required_dict(payload, "style")
    instruction = _required_dict(payload, "instruction")
    requirement_content = _required_dict(requirement, "content")
    direction_content = _required_dict(direction, "content")
    instruction_content = _required_dict(instruction, "content")
    aspect_ratio = str(payload.get("aspect_ratio") or "")
    if aspect_ratio not in RATIO_SPECS:
        raise PromptCompileError("PROMPT_OUTPUT_SPEC_MISSING", "Prompt 缺少有效输出比例。")
    if not _clean_list(poem.get("lines")):
        raise PromptCompileError("PROMPT_CONTENT_MISSING", "Prompt 缺少诗词正文。")
    if not str(style.get("prompt_fragment") or "").strip():
        raise PromptCompileError("PROMPT_STYLE_MISSING", "Prompt 缺少已发布风格片段。")
    if not str(instruction_content.get("visual_goal") or "").strip():
        raise PromptCompileError("PROMPT_INSTRUCTION_MISSING", "Prompt 缺少全局视觉目标。")
    art_bible = style.get("art_bible")
    if not isinstance(art_bible, dict) or not isinstance(
        art_bible.get("content"), dict
    ):
        raise PromptCompileError(
            "PROMPT_ART_BIBLE_MISSING", "Prompt 缺少风格版本绑定的 Art Bible。"
        )
    for field in (
        "visual_thesis",
        "scene",
        "composition",
        "text_safe_area",
        "interpretation_layers",
    ):
        if not direction_content.get(field):
            raise PromptCompileError(
                "PROMPT_DIRECTION_CONTRACT_MISSING",
                f"已批准方向缺少生产字段 {field}。",
            )

    segments: dict[str, Any] = {
        "content": {
            "poem_id": poem.get("id"),
            "content_version_id": poem.get("content_version_id"),
            "content_version": poem.get("content_version"),
            "title": poem.get("title"),
            "author": poem.get("author"),
            "dynasty": poem.get("dynasty"),
            "lines": _clean_list(poem.get("lines")),
            "theme": poem.get("theme"),
            "mood": poem.get("mood"),
        },
        "requirement": {
            "id": requirement.get("id"),
            "version": requirement.get("version"),
            "theme": requirement_content.get("theme"),
            "mood": requirement_content.get("mood"),
            "time_and_place": requirement_content.get("time_and_place"),
            "subject": requirement_content.get("subject"),
            "core_imagery": _clean_list(requirement_content.get("core_imagery")),
            "must_have": _clean_list(requirement_content.get("must_have")),
            "avoid": _clean_list(requirement_content.get("avoid")),
            "historical_risks": _clean_list(
                requirement_content.get("historical_risks")
            ),
        },
        "direction": {
            "id": direction.get("id"),
            "version": direction.get("version"),
            "type": direction.get("type"),
            "schema_version": direction.get("schema_version"),
            "generation_run_id": direction.get("generation_run_id"),
            "title": direction_content.get("title"),
            "visual_thesis": direction_content.get("visual_thesis"),
            "subject": direction_content.get("subject"),
            "subject_mode": direction_content.get("subject_mode"),
            "scene": direction_content.get("scene"),
            "shot": direction_content.get("shot"),
            "shot_scale": direction_content.get("shot_scale"),
            "narrative_mode": direction_content.get("narrative_mode"),
            "foreground": direction_content.get("foreground"),
            "midground": direction_content.get("midground"),
            "background": direction_content.get("background"),
            "action": direction_content.get("action"),
            "composition": direction_content.get("composition"),
            "lighting": direction_content.get("lighting"),
            "palette": direction_content.get("palette"),
            "whitespace": direction_content.get("whitespace"),
            "text_safe_area": direction_content.get("text_safe_area"),
            "preserve": _clean_list(direction_content.get("preserve")),
            "avoid": _clean_list(direction_content.get("avoid")),
            "interpretation_layers": direction_content.get("interpretation_layers"),
        },
        "style": {
            "id": style.get("id"),
            "version_id": style.get("version_id"),
            "version": style.get("version"),
            "semantic_version": style.get("semantic_version"),
            "schema_version": style.get("schema_version"),
            "name": style.get("name"),
            "prompt_fragment": style.get("prompt_fragment"),
            "palette": _clean_list(style.get("palette")),
            "visual_traits": style.get("visual_traits") or {},
            "character_design": style.get("character_design") or {},
            "avoid": _clean_list(style.get("avoid")),
            "risks": _clean_list(style.get("risks")),
            "art_bible": {
                "id": art_bible.get("id"),
                "version": art_bible.get("version"),
                "semantic_version": art_bible.get("semantic_version"),
                "schema_version": art_bible.get("schema_version"),
                "content": art_bible.get("content") or {},
            },
        },
        "output": {
            "aspect_ratio": aspect_ratio,
            **RATIO_SPECS[aspect_ratio],
            "layout_safe_area": "保留可用于诗文排版的安全留白，但图内不得生成文字",
            "prohibitions": ["文字", "字母", "题款", "水印", "标志", "边框外溢"],
        },
        "instruction": {
            "id": instruction.get("id"),
            "version": instruction.get("version"),
            "name": instruction.get("name"),
            "audience": instruction_content.get("audience"),
            "visual_goal": instruction_content.get("visual_goal"),
            "composition_rules": _clean_list(
                instruction_content.get("composition_rules")
            ),
            "historical_rules": _clean_list(
                instruction_content.get("historical_rules")
            ),
            "global_avoid": _clean_list(instruction_content.get("global_avoid")),
        },
    }
    rework = payload.get("rework")
    if isinstance(rework, dict):
        segments["rework"] = {
            "order_id": rework.get("order_id"),
            "parent_image_id": rework.get("parent_image_id"),
            "preserve": _clean_list(rework.get("preserve")),
            "change": _clean_list(rework.get("change")),
            "avoid": _clean_list(rework.get("avoid")),
            "note": str(rework.get("note") or "").strip(),
        }
    return segments


def _render_text(segments: dict[str, Any], provider: str) -> str:
    content = segments["content"]
    requirement = segments["requirement"]
    direction = segments["direction"]
    style = segments["style"]
    output = segments["output"]
    instruction = segments["instruction"]
    layers = direction.get("interpretation_layers") or {}
    fact_claims = [
        str(item.get("claim") or "").strip()
        for item in layers.get("poem_facts", [])
        if isinstance(item, dict) and str(item.get("claim") or "").strip()
    ]
    inference_claims = [
        str(item.get("claim") or "").strip()
        for item in layers.get("reasonable_inferences", [])
        if isinstance(item, dict) and str(item.get("claim") or "").strip()
    ]
    creative_claims = [
        str(item.get("claim") or "").strip()
        for item in layers.get("creative_choices", [])
        if isinstance(item, dict) and str(item.get("claim") or "").strip()
    ]
    art_bible = style.get("art_bible") or {}
    art_rules = art_bible.get("content") or {}
    traits = style.get("visual_traits") or {}
    character = style.get("character_design") or {}
    style_version_label = style.get("semantic_version") or f"v{style.get('version')}"
    blocks = [
        (
            "[01 CONTENT / 诗词正文]\n"
            f"《{content['title']}》· {content['dynasty']} · {content['author']}\n"
            f"正文：{' / '.join(content['lines'])}\n"
            f"题材：{content.get('theme') or '未分类'}；情绪：{content.get('mood') or '待确认'}"
        ),
        (
            "[02 REQUIREMENT / 内容需求]\n"
            f"时空：{requirement.get('time_and_place') or '依据原诗审慎表达'}\n"
            f"主体：{requirement.get('subject') or '依据原诗核心意象'}\n"
            f"核心意象：{_join(requirement.get('core_imagery'))}\n"
            f"必须出现：{_join(requirement.get('must_have'))}\n"
            f"禁止出现：{_join(requirement.get('avoid'))}\n"
            f"历史风险：{_join(requirement.get('historical_risks'))}"
        ),
        (
            "[03 DIRECTION / 已批准画面方向]\n"
            f"类型：{direction.get('type') or '未指定'}；标题：{direction.get('title') or '未命名方向'}\n"
            f"视觉命题：{direction.get('visual_thesis') or '未指定'}\n"
            f"场景：{direction.get('scene') or '未指定'}；构图：{direction.get('composition') or '未指定'}\n"
            f"主体与景别：{direction.get('subject') or '未指定'}；{direction.get('shot') or '未指定'}\n"
            f"前中后景：{direction.get('foreground') or '—'} / {direction.get('midground') or '—'} / {direction.get('background') or '—'}\n"
            f"动作：{direction.get('action') or '静态诗意'}；光线：{direction.get('lighting') or '自然光'}\n"
            f"色彩：{direction.get('palette') or '遵循风格包'}；留白：{direction.get('whitespace') or '保留排版安全区'}\n"
            f"文字安全区：{direction.get('text_safe_area') or '未指定'}\n"
            f"诗文事实：{_join(fact_claims)}；合理演绎：{_join(inference_claims)}；创意表达：{_join(creative_claims)}\n"
            f"保持：{_join(direction.get('preserve'))}；方向禁用：{_join(direction.get('avoid'))}"
        ),
        (
            "[04 STYLE / 冻结风格版本]\n"
            f"{style.get('name') or style.get('id')} {style_version_label}\n"
            f"{style.get('prompt_fragment')}\n"
            f"色板：{_join(style.get('palette'))}\n"
            f"线条：{traits.get('line') or '未指定'}；材质：{traits.get('texture') or '未指定'}；"
            f"光线：{traits.get('lighting') or '未指定'}\n"
            f"人物比例：{character.get('proportion') or '未指定'}；表情：{character.get('expression') or '未指定'}；"
            f"服饰：{character.get('costume') or '未指定'}\n"
            f"风格禁用：{_join(style.get('avoid'))}；已知风险：{_join(style.get('risks'))}"
        ),
        (
            "[05 OUTPUT / 输出规格]\n"
            f"{output['label']}，目标 {output['width']}×{output['height']}。\n"
            f"{output['layout_safe_area']}。\n"
            f"禁止：{_join(output['prohibitions'])}。"
        ),
        (
            "[06 GLOBAL / 全局规范]\n"
            f"受众：{instruction.get('audience') or '内容出版团队'}\n"
            f"视觉目标：{instruction.get('visual_goal')}\n"
            f"构图原则：{_join(instruction.get('composition_rules'))}\n"
            f"历史原则：{_join(instruction.get('historical_rules'))}\n"
            f"全局禁用：{_join(instruction.get('global_avoid'))}\n"
            f"Art Bible {art_bible.get('semantic_version') or '未记录'} · "
            f"色彩：{_join(art_rules.get('palette_rules'))}\n"
            f"空间：{_join(art_rules.get('spatial_rules'))}\n"
            f"文字禁令：{_join(art_rules.get('text_prohibitions'))}\n"
            f"历史边界：{_join(art_rules.get('historical_boundaries'))}"
        ),
    ]
    if "rework" in segments:
        rework = segments["rework"]
        blocks.append(
            "[07 REWORK / 结构化返工]\n"
            f"父图：{rework.get('parent_image_id')}\n"
            f"必须保持：{_join(rework.get('preserve'))}\n"
            f"只修改：{_join(rework.get('change'))}\n"
            f"禁止：{_join(rework.get('avoid'))}\n"
            f"备注：{rework.get('note') or '无'}"
        )
    prefix = (
        "Create one original, publication-ready editorial illustration. "
        "Treat every numbered block as a hard production contract."
        if provider == "openai"
        else "Deterministic demo rendering contract; preserve all source references."
    )
    return f"{prefix}\n\n" + "\n\n".join(blocks)


def compile_generation_prompt(
    payload: dict[str, Any],
    provider: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PromptCompileError("INVALID_PROMPT_PAYLOAD", "Prompt 输入必须是对象。")
    provider = str(provider).strip().lower()
    if provider not in TEMPLATE_VERSIONS:
        raise PromptCompileError("UNSUPPORTED_PROMPT_PROVIDER", "没有对应的 Provider Prompt 模板。")
    segments = build_segments(payload)
    template_version = TEMPLATE_VERSIONS[provider]
    canonical = json.dumps(
        {"template_version": template_version, "segments": segments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    text = _render_text(segments, provider)
    return {
        "template_version": template_version,
        "provider": provider,
        "hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "text": text,
        "segments": segments,
        "source_refs": {
            "content_version_id": segments["content"].get("content_version_id"),
            "instruction_version_id": segments["instruction"].get("id"),
            "requirement_id": segments["requirement"].get("id"),
            "direction_id": segments["direction"].get("id"),
            "direction_schema_version": segments["direction"].get("schema_version"),
            "direction_generation_run_id": segments["direction"].get("generation_run_id"),
            "style_version_id": segments["style"].get("version_id"),
            "style_schema_version": segments["style"].get("schema_version"),
            "art_bible_version_id": (segments["style"].get("art_bible") or {}).get("id"),
        },
    }
