#!/usr/bin/env python3
"""唐诗绘卷 local web server.

Runs with Python's standard library only. In demo mode it renders deterministic
SVG illustrations locally. When OPENAI_API_KEY is present it can submit real
image generation requests to the OpenAI Images API.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from backup_service import create_backup, list_backups, verify_backup
from direction_schema import schema_document as direction_schema_document
from poem_import_schema import (
    PoemImportContractError,
    csv_template_text,
    json_template_document,
    parse_import_document,
    schema_document as poem_import_schema_document,
)
from qc_engine import QC_VERSION, inspect_image
from review_schema import (
    compose_qc_result,
    qc_policy_schema_document,
    review_result_schema_document,
)
from requirement_schema import schema_document as requirement_schema_document
from style_schema import (
    art_bible_schema_document,
    style_pack_schema_document,
)
from visual_reviewer import review_image, visual_reviewer_status
from sop_store import (
    DEFAULT_PROJECT_ID as SOP_DEFAULT_PROJECT_ID,
    SopStore,
    WorkflowError,
)


ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = Path(os.getenv("TANG_STUDIO_PUBLIC_DIR", ROOT / "public")).resolve()
DATA_DIR = Path(os.getenv("TANG_STUDIO_DATA_DIR", ROOT / "data")).resolve()
GENERATED_DIR = DATA_DIR / "generated"
STATE_FILE = DATA_DIR / "state.json"
POEMS_FILE = DATA_DIR / "poems.json"
STYLES_FILE = DATA_DIR / "styles.json"
APP_VERSION = "0.14.0"

DEFAULT_PROJECT_ID = "tang-poems-baseline"
DECISION_VALUES = {"candidate", "selected", "rejected", "final"}
QC_KEYS = {
    "poem_relevance",
    "period_accuracy",
    "series_consistency",
    "visual_integrity",
    "layout_safety",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def atomic_write_text(path: Path, payload: str) -> None:
    atomic_write_bytes(path, payload.encode("utf-8"))


def structured_log(event: str, *, level: str = "info", **fields: Any) -> None:
    if event in {"task.claimed", "task.succeeded"} and os.environ.get(
        "TANG_VERBOSE_TASK_LOGS", ""
    ).lower() not in {"1", "true", "yes"}:
        return
    record = {
        "time": utc_now(),
        "level": level,
        "event": str(event)[:100],
    }
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            record[str(key)[:60]] = value if not isinstance(value, str) else value[:1000]
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")), flush=True)


class StudioStore:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.poems = read_json(POEMS_FILE, [])
        self.styles = read_json(STYLES_FILE, [])
        self.poem_by_id = {item["id"]: item for item in self.poems}
        self.style_by_id = {item["id"]: item for item in self.styles}
        state = read_json(STATE_FILE, {"projects": [], "jobs": [], "images": []})
        self.projects = state.get("projects", [])
        self.jobs = state.get("jobs", [])
        self.images = state.get("images", [])
        if not self.projects:
            self.projects.append(
                {
                    "id": DEFAULT_PROJECT_ID,
                    "name": "唐诗十首 · 视觉基线",
                    "purpose": "诗画卡片",
                    "poem_ids": [item["id"] for item in self.poems],
                    "style_id": "light-gongbi",
                    "aspect_ratio": "portrait",
                    "status": "in_progress",
                    "created_at": utc_now(),
                    "updated_at": utc_now(),
                }
            )
        fallback_project_id = self.projects[0]["id"]
        for image in self.images:
            if not image.get("project_id"):
                image["project_id"] = fallback_project_id
            image_project = next(
                (
                    project
                    for project in self.projects
                    if project["id"] == image.get("project_id")
                ),
                self.projects[0],
            )
            image.setdefault("project_name", image_project["name"])
            image.setdefault("generation_mode", "explore")
            image.setdefault("parent_image_id", None)
            image.setdefault("decision", "selected" if image.get("favorite") else "candidate")
            image.setdefault("feedback_tags", [])
            image.setdefault("review_note", "")
            image.setdefault("qc", {})
        for job in self.jobs:
            if not job.get("project_id"):
                job["project_id"] = fallback_project_id
            job.setdefault("generation_mode", "explore")
            job.setdefault("parent_image_id", None)
        for job in self.jobs:
            if job.get("status") in {"queued", "running"}:
                job["status"] = "failed"
                job["error"] = "本地服务重启，未完成任务已停止，请重新生成。"
                job["finished_at"] = utc_now()
        self.persist()

    def persist(self) -> None:
        with self.lock:
            atomic_write_json(
                STATE_FILE,
                {"projects": self.projects, "jobs": self.jobs, "images": self.images},
            )

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "poems": self.poems,
                "styles": self.styles,
                "projects": self.projects,
                "images": list(reversed(self.images[-200:])),
                "jobs": list(reversed(self.jobs[-30:])),
            }

    def add_project(self, project: dict[str, Any]) -> None:
        with self.lock:
            self.projects.append(project)
            self.persist()

    def update_project(self, project_id: str, **updates: Any) -> dict[str, Any] | None:
        with self.lock:
            project = next(
                (item for item in self.projects if item["id"] == project_id), None
            )
            if not project:
                return None
            project.update(updates)
            project["updated_at"] = utc_now()
            self.persist()
            return project

    def add_job(self, job: dict[str, Any]) -> None:
        with self.lock:
            self.jobs.append(job)
            self.persist()

    def update_job(self, job_id: str, **updates: Any) -> None:
        with self.lock:
            job = next((item for item in self.jobs if item["id"] == job_id), None)
            if not job:
                return
            job.update(updates)
            self.persist()

    def add_image(self, image: dict[str, Any]) -> None:
        with self.lock:
            self.images.append(image)
            self.persist()

    def update_image(self, image_id: str, **updates: Any) -> dict[str, Any] | None:
        with self.lock:
            image = next((item for item in self.images if item["id"] == image_id), None)
            if not image:
                return None
            image.update(updates)
            image["updated_at"] = utc_now()
            self.persist()
            return image


STORE: StudioStore | None = None
SOP_STORE: SopStore | None = None
BATCH_WORKERS: dict[str, threading.Thread] = {}
BATCH_WORKERS_LOCK = threading.RLock()


def _batch_concurrency() -> int:
    try:
        return max(1, min(int(os.getenv("TANG_BATCH_CONCURRENCY", "2")), 8))
    except ValueError:
        return 2


BATCH_TASK_SEMAPHORE = threading.Semaphore(_batch_concurrency())


def get_sop_store() -> SopStore:
    """Return a workflow store bound to the active data directory.

    Tests replace DATA_DIR at runtime, so the binding is refreshed whenever the
    desired database path changes.
    """

    global SOP_STORE
    database_path = (DATA_DIR / "studio.db").resolve()
    if SOP_STORE is None or SOP_STORE.database_path != database_path:
        SOP_STORE = SopStore(database_path, POEMS_FILE, STYLES_FILE)
    return SOP_STORE


def provider_name() -> str:
    requested = os.getenv("AI_PROVIDER", "auto").strip().lower()
    if requested == "auto":
        return "openai" if os.getenv("OPENAI_API_KEY") else "demo"
    return requested


class ProviderCircuitBreaker:
    def __init__(self, threshold: int = 5, cooldown_seconds: int = 60) -> None:
        self.threshold = max(2, int(threshold))
        self.cooldown_seconds = max(1, int(cooldown_seconds))
        self.lock = threading.RLock()
        self.states: dict[str, dict[str, Any]] = {}

    def status(self, provider: str) -> dict[str, Any]:
        provider = str(provider).strip().lower()
        with self.lock:
            state = self.states.setdefault(
                provider,
                {"failures": 0, "open_until": 0.0, "last_error_code": ""},
            )
            now = time.monotonic()
            if state["open_until"] and state["open_until"] <= now:
                state.update(failures=0, open_until=0.0, last_error_code="")
            remaining = max(0, round(state["open_until"] - now))
            return {
                "state": "open" if remaining > 0 else "closed",
                "failure_count": int(state["failures"]),
                "threshold": self.threshold,
                "cooldown_seconds": self.cooldown_seconds,
                "retry_after_seconds": remaining,
                "last_error_code": state["last_error_code"],
            }

    def record_success(self, provider: str) -> None:
        with self.lock:
            self.states[str(provider).strip().lower()] = {
                "failures": 0,
                "open_until": 0.0,
                "last_error_code": "",
            }

    def record_failure(self, provider: str, error_code: str) -> dict[str, Any]:
        provider = str(provider).strip().lower()
        with self.lock:
            state = self.states.setdefault(
                provider,
                {"failures": 0, "open_until": 0.0, "last_error_code": ""},
            )
            state["failures"] += 1
            state["last_error_code"] = str(error_code)[:80]
            if state["failures"] >= self.threshold:
                state["open_until"] = time.monotonic() + self.cooldown_seconds
        return self.status(provider)

    def reset(self, provider: str | None = None) -> None:
        with self.lock:
            if provider is None:
                self.states.clear()
            else:
                self.states.pop(str(provider).strip().lower(), None)


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(os.getenv(name, str(default))), maximum))
    except ValueError:
        return default


PROVIDER_CIRCUITS = ProviderCircuitBreaker(
    threshold=_env_int("TANG_PROVIDER_CIRCUIT_THRESHOLD", 5, 2, 20),
    cooldown_seconds=_env_int("TANG_PROVIDER_CIRCUIT_COOLDOWN", 60, 5, 3600),
)


def provider_runtime_status() -> dict[str, Any]:
    """Expose safe operational settings without leaking provider credentials."""

    provider = provider_name()
    configured = provider == "demo" or bool(os.getenv("OPENAI_API_KEY"))
    circuit = PROVIDER_CIRCUITS.status(provider)
    return {
        "provider": provider,
        "model": (
            "demo-renderer"
            if provider == "demo"
            else os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2")
        ),
        "status": (
            "circuit_open"
            if circuit["state"] == "open"
            else "ready"
            if configured
            else "configuration_required"
        ),
        "configured": configured,
        "live_generation": provider == "openai" and configured,
        "concurrency": _batch_concurrency(),
        "timeouts_seconds": {
            "generation": 240,
            "edit": 300,
            "download": 120,
        },
        "max_attempts": 3,
        "circuit": circuit,
        "visual_qc": visual_reviewer_status(provider),
        "capabilities": {
            "generation": provider in {"demo", "openai"},
            "image_edit": provider == "openai",
            "aspect_ratios": ["portrait", "square", "landscape"],
            "max_images_per_direction": 4,
        },
    }


def present_export_package(package: dict[str, Any]) -> dict[str, Any]:
    result = dict(package)
    if result.get("status") == "completed":
        result["manifest_url"] = f"/exports/{result['name']}/manifest.json"
    else:
        result["manifest_url"] = None
    return result


def build_prompt(
    poem: dict[str, Any],
    style: dict[str, Any],
    custom_note: str,
    workflow: dict[str, Any] | None = None,
) -> str:
    lines = "，".join(poem["lines"])
    constraints = "、".join(poem.get("avoid", []))
    note = custom_note.strip()
    note_block = f"\nAdditional art direction: {note}" if note else ""
    workflow = workflow or {}
    mode = workflow.get("generation_mode", "explore")
    if mode == "converge":
        preserve = "、".join(workflow.get("preserve", [])) or "整体风格与主体关系"
        workflow_block = (
            "\nIteration mode: converge from the approved parent candidate. "
            f"Preserve: {preserve}. Change only the explicitly requested areas."
        )
    else:
        directions = workflow.get("exploration_directions") or [
            "诗意远景",
            "主体叙事",
            "意象留白",
            "空间层次",
        ]
        workflow_block = (
            "\nExploration mode: propose a clearly differentiated art-direction route. "
            f"Possible route: {directions[workflow.get('sample_index', 0) % len(directions)]}."
        )
    project_block = (
        f"\nSeries brief: {workflow.get('project_name', '唐诗插图系列')}，"
        f"intended for {workflow.get('purpose', '诗画内容发布')}。"
    )
    return (
        "Create an original editorial illustration inspired by a classical Tang poem.\n"
        f"Poem: {poem['title']} by {poem['author']} ({lines}).\n"
        f"Visual thesis: {poem['visual_brief']}\n"
        f"Mood: {poem['mood']}. Key imagery: {', '.join(poem['imagery'])}.\n"
        f"Style: {style['prompt_fragment']}.\n"
        "Composition: a polished 4:5 book illustration with a strong focal hierarchy, "
        "period-plausible Tang-dynasty setting, and useful breathing room.\n"
        f"Avoid: {constraints}; anachronisms; no text; no generated letters; calligraphy; captions; "
        "logos; watermarks; recognizable living-artist or protected studio styles."
        f"{project_block}{workflow_block}"
        f"{note_block}"
    )


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _mix(first: str, second: str, ratio: float) -> str:
    a = _hex_to_rgb(first)
    b = _hex_to_rgb(second)
    rgb = tuple(round(x * (1 - ratio) + y * ratio) for x, y in zip(a, b))
    return "#" + "".join(f"{item:02x}" for item in rgb)


def _scene_variant(poem_id: str) -> str:
    if poem_id in {"jing-ye-si", "feng-qiao-ye-bo"}:
        return "moon"
    if poem_id in {"jiang-xue", "zhong-nan-wang-yu-xue"}:
        return "snow"
    if poem_id in {"lu-zhai", "shan-ju-qiu-ming", "chun-xiao"}:
        return "forest"
    if poem_id in {"deng-guan-que-lou", "song-meng-hao-ran"}:
        return "river"
    return "desert"


def render_demo_svg(
    output: Path,
    poem: dict[str, Any],
    style: dict[str, Any],
    sample_index: int,
    aspect_ratio: str = "portrait",
) -> None:
    """Render a stylized local fallback, deliberately labelled as demo artwork."""
    width, height = {
        "portrait": (1024, 1536),
        "square": (1024, 1024),
        "landscape": (1536, 1024),
    }.get(aspect_ratio, (1024, 1536))
    scale_x = width / 1024
    scale_y = height / 1280
    bg = style["background"]
    fg = style["foreground"]
    accent = style["accent"]
    mid = style["palette"][1]
    pale = _mix(bg, "#ffffff", 0.32)
    dark = _mix(fg, "#000000", 0.18)
    seed = int(hashlib.sha256(f"{poem['id']}:{style['id']}:{sample_index}".encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    variant = _scene_variant(poem["id"])
    texture_id = f"paper-{seed}"
    blur_id = f"blur-{seed}"

    # Varied mountain silhouettes retain a shared style palette.
    points_back = " ".join(
        f"{x},{720 + rng.randint(-130, 100)}" for x in range(-40, 1120, 130)
    )
    points_front = " ".join(
        f"{x},{900 + rng.randint(-120, 90)}" for x in range(-40, 1120, 115)
    )
    scene = []

    if variant == "moon":
        moon_x = 710 if sample_index % 2 == 0 else 300
        scene.extend(
            [
                f'<circle cx="{moon_x}" cy="270" r="116" fill="{pale}" opacity=".94"/>',
                f'<circle cx="{moon_x - 26}" cy="244" r="5" fill="{mid}" opacity=".25"/>',
                f'<path d="M0 870 Q260 780 520 865 T1024 820 V1280 H0Z" fill="{dark}" opacity=".78"/>',
                f'<path d="M0 970 Q300 870 620 960 T1024 930 V1280 H0Z" fill="{fg}" opacity=".88"/>',
                f'<path d="M110 1020 C330 965 520 986 930 926" stroke="{accent}" stroke-width="8" opacity=".72" fill="none"/>',
                f'<path d="M565 880 q38 -92 76 0 v150 h-76z" fill="{dark}"/>',
                f'<circle cx="603" cy="858" r="25" fill="{dark}"/>',
            ]
        )
    elif variant == "snow":
        scene.extend(
            [
                f'<polygon points="-40,1280 {points_back} 1120,1280" fill="{mid}" opacity=".28"/>',
                f'<polygon points="-40,1280 {points_front} 1120,1280" fill="{fg}" opacity=".42"/>',
                f'<path d="M0 1030 Q280 980 520 1040 T1024 1005 V1280 H0Z" fill="{pale}"/>',
                f'<path d="M410 1040 q80 -28 160 0 q-48 18 -112 18z" fill="{dark}"/>',
                f'<path d="M486 1018 v-62" stroke="{dark}" stroke-width="9"/><path d="M462 965 q25 -38 50 0" fill="{dark}"/>',
            ]
        )
        for _ in range(52):
            x, y, r = rng.randint(20, 1004), rng.randint(60, 960), rng.randint(2, 7)
            scene.append(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{pale}" opacity=".{rng.randint(35, 85)}"/>')
    elif variant == "forest":
        scene.append(f'<rect width="1024" height="1280" fill="{_mix(bg, mid, .28)}"/>')
        for index in range(14):
            x = index * 82 + rng.randint(-30, 20)
            tree_color = fg if index % 3 else mid
            opacity = 0.26 + (index % 5) * 0.11
            scene.append(
                f'<path d="M{x} 1280 C{x+20} 920 {x-25} 500 {x+40} -40" stroke="{tree_color}" stroke-width="{22 + index % 4 * 7}" opacity="{opacity:.2f}" fill="none"/>'
            )
        scene.extend(
            [
                f'<path d="M-50 1020 Q210 900 490 1010 T1080 930 V1280 H-50Z" fill="{dark}" opacity=".72"/>',
                f'<path d="M0 1085 C280 980 690 1110 1024 965" stroke="{pale}" stroke-width="22" opacity=".86" fill="none"/>',
                f'<path d="M650 70 L380 980" stroke="{pale}" stroke-width="130" opacity=".17" filter="url(#{blur_id})"/>',
            ]
        )
    elif variant == "river":
        scene.extend(
            [
                f'<circle cx="760" cy="310" r="134" fill="{accent}" opacity=".82"/>',
                f'<polygon points="-40,1280 {points_back} 1120,1280" fill="{mid}" opacity=".52"/>',
                f'<path d="M0 820 Q310 760 530 850 T1024 780 V1280 H0Z" fill="{_mix(bg, mid, .45)}"/>',
                f'<path d="M0 940 Q280 850 560 950 T1024 880 V1280 H0Z" fill="{fg}" opacity=".72"/>',
                f'<path d="M0 895 C330 940 610 870 1024 920" stroke="{pale}" stroke-width="13" opacity=".66" fill="none"/>',
                f'<path d="M610 850 q72 -23 144 0 q-38 18 -106 18z" fill="{dark}"/>',
                f'<path d="M675 848 v-108 l70 90z" fill="{pale}" opacity=".78"/>',
            ]
        )
    else:
        scene.extend(
            [
                f'<circle cx="240" cy="260" r="138" fill="{accent}" opacity=".78"/>',
                f'<path d="M0 820 Q250 700 530 835 T1024 780 V1280 H0Z" fill="{mid}" opacity=".64"/>',
                f'<path d="M0 980 Q310 820 620 960 T1024 900 V1280 H0Z" fill="{fg}" opacity=".82"/>',
                f'<path d="M700 850 q60 -75 120 0 v190 h-120z" fill="{dark}"/>',
                f'<path d="M728 860 q28 -44 56 0" stroke="{accent}" stroke-width="9" fill="none"/>',
            ]
        )

    # Decorative style receives a print-like frame; ink style stays minimal.
    frame = ""
    if style["id"] == "new-chinese-decorative":
        frame = f'<rect x="42" y="42" width="940" height="1196" rx="10" fill="none" stroke="{accent}" stroke-width="7" opacity=".78"/><circle cx="90" cy="90" r="17" fill="{accent}"/>'
    elif style["id"] == "childrens-book":
        frame = f'<path d="M82 1120 Q220 1060 340 1140 T610 1130 T930 1090" stroke="{accent}" stroke-width="18" stroke-linecap="round" fill="none" opacity=".72"/>'

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <defs>
    <filter id="{texture_id}"><feTurbulence type="fractalNoise" baseFrequency=".55" numOctaves="3" seed="{seed % 97}"/><feColorMatrix values="0 0 0 0 0.5  0 0 0 0 0.45  0 0 0 0 0.35  0 0 0 .08 0"/></filter>
    <filter id="{blur_id}"><feGaussianBlur stdDeviation="34"/></filter>
    <linearGradient id="wash-{seed}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{pale}"/><stop offset="1" stop-color="{bg}"/></linearGradient>
  </defs>
  <g transform="scale({scale_x:.6f} {scale_y:.6f})">
    <rect width="1024" height="1280" fill="url(#wash-{seed})"/>
    {''.join(scene)}
    {frame}
    <rect width="1024" height="1280" filter="url(#{texture_id})" opacity=".72"/>
  </g>
</svg>'''
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(output, svg)


def generate_openai_image(
    output: Path,
    prompt: str,
    aspect_ratio: str,
) -> None:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("未设置 OPENAI_API_KEY，无法使用真实图像生成。")
    size_by_ratio = {
        "portrait": "1024x1536",
        "square": "1024x1024",
        "landscape": "1536x1024",
    }
    payload = {
        "model": os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2"),
        "prompt": prompt,
        "size": size_by_ratio.get(aspect_ratio, "1024x1536"),
        "quality": os.getenv("OPENAI_IMAGE_QUALITY", "medium"),
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/images/generations",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": f"tang-poem-studio/{APP_VERSION}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(body).get("error", {}).get("message", body)
        except json.JSONDecodeError:
            message = body
        raise RuntimeError(f"OpenAI 图像接口返回 {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接图像接口：{exc.reason}") from exc

    data = result.get("data") or []
    if not data:
        raise RuntimeError("图像接口没有返回图片数据。")
    item = data[0]
    output.parent.mkdir(parents=True, exist_ok=True)
    if item.get("b64_json"):
        payload = base64.b64decode(item["b64_json"])
        if len(payload) > 50_000_000:
            raise RuntimeError("图像接口返回的文件超过 50 MB 安全上限。")
        atomic_write_bytes(output, payload)
        return
    if item.get("url"):
        with urllib.request.urlopen(item["url"], timeout=120) as response:
            payload = response.read(50_000_001)
        if len(payload) > 50_000_000:
            raise RuntimeError("图像接口返回的文件超过 50 MB 安全上限。")
        atomic_write_bytes(output, payload)
        return
    raise RuntimeError("图像接口响应中缺少 b64_json 或 url。")


def _encode_multipart(
    fields: dict[str, str], file_field: str, file_path: Path
) -> tuple[bytes, str]:
    boundary = f"----TangPoemStudio{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{file_path.name}"\r\n'
            ).encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            file_path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(chunks), boundary


def edit_openai_image(
    output: Path,
    prompt: str,
    aspect_ratio: str,
    parent_path: Path,
) -> None:
    """Create a high-fidelity edit using the selected parent candidate."""
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("未设置 OPENAI_API_KEY，无法使用真实图像编辑。")
    if parent_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise RuntimeError("真实收敛迭代需要 PNG、JPEG 或 WebP 父候选，请先选择 AI 样图或真实生成图。")
    size_by_ratio = {
        "portrait": "1024x1536",
        "square": "1024x1024",
        "landscape": "1536x1024",
    }
    body, boundary = _encode_multipart(
        {
            "model": os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2"),
            "prompt": prompt,
            "size": size_by_ratio.get(aspect_ratio, "1024x1536"),
            "quality": os.getenv("OPENAI_IMAGE_QUALITY", "medium"),
            "output_format": "png",
        },
        "image[]",
        parent_path,
    )
    request = urllib.request.Request(
        "https://api.openai.com/v1/images/edits",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": f"tang-poem-studio/{APP_VERSION}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(body_text).get("error", {}).get("message", body_text)
        except json.JSONDecodeError:
            message = body_text
        raise RuntimeError(f"OpenAI 图像编辑接口返回 {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接图像编辑接口：{exc.reason}") from exc
    data = result.get("data") or []
    if not data or not data[0].get("b64_json"):
        raise RuntimeError("图像编辑接口没有返回可保存的图片数据。")
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = base64.b64decode(data[0]["b64_json"])
    if len(payload) > 50_000_000:
        raise RuntimeError("图像接口返回的文件超过 50 MB 安全上限。")
    atomic_write_bytes(output, payload)


def local_image_path(image: dict[str, Any]) -> Path:
    url = str(image.get("url", ""))
    if url.startswith("/generated/"):
        return GENERATED_DIR / Path(url).name
    if url.startswith("/samples/"):
        return PUBLIC_DIR / "samples" / Path(url).name
    raise RuntimeError("父候选不是可读取的本地图片。")


def create_image_record(
    job_id: str,
    poem: dict[str, Any],
    style: dict[str, Any],
    index: int,
    provider: str,
    aspect_ratio: str,
    custom_note: str,
    project: dict[str, Any],
    generation_mode: str = "explore",
    parent_image_id: str | None = None,
    preserve: list[str] | None = None,
    compiled_prompt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    image_id = uuid.uuid4().hex
    extension = "svg" if provider == "demo" else "png"
    filename = f"{image_id}.{extension}"
    output = GENERATED_DIR / filename
    workflow = {
        "project_name": project["name"],
        "purpose": project.get("purpose", "诗画内容发布"),
        "generation_mode": generation_mode,
        "parent_image_id": parent_image_id,
        "preserve": preserve or [],
        "sample_index": index,
    }
    prompt = str((compiled_prompt or {}).get("text") or "")
    if not prompt:
        prompt = build_prompt(poem, style, custom_note, workflow)
    if provider == "demo":
        render_demo_svg(output, poem, style, index, aspect_ratio)
    elif provider == "openai":
        if generation_mode == "converge" and parent_image_id:
            assert STORE is not None
            parent = next(
                (item for item in STORE.images if item["id"] == parent_image_id),
                None,
            )
            if not parent:
                raise RuntimeError("父候选不存在，无法执行收敛迭代。")
            edit_openai_image(output, prompt, aspect_ratio, local_image_path(parent))
        else:
            generate_openai_image(output, prompt, aspect_ratio)
    else:
        raise RuntimeError(f"不支持的 AI_PROVIDER：{provider}")
    return {
        "id": image_id,
        "job_id": job_id,
        "poem_id": poem["id"],
        "poem_title": poem["title"],
        "author": poem["author"],
        "style_id": style["id"],
        "style_name": style["name"],
        "project_id": project["id"],
        "project_name": project["name"],
        "provider": provider,
        "url": f"/generated/{filename}",
        "aspect_ratio": aspect_ratio,
        "prompt": prompt,
        "prompt_hash": str((compiled_prompt or {}).get("hash") or ""),
        "prompt_template_version": str(
            (compiled_prompt or {}).get("template_version") or ""
        ),
        "generation_mode": generation_mode,
        "parent_image_id": parent_image_id,
        "decision": "candidate",
        "feedback_tags": [],
        "review_note": "",
        "qc": {},
        "favorite": False,
        "created_at": utc_now(),
    }


def run_generation_job(job_id: str) -> None:
    assert STORE is not None
    with STORE.lock:
        job = next(item for item in STORE.jobs if item["id"] == job_id)
        poem = STORE.poem_by_id[job["poem_id"]]
        style = STORE.style_by_id[job["style_id"]]
        provider = job["provider"]
        count = job["count"]
        ratio = job["aspect_ratio"]
        note = job.get("custom_note", "")
        project = next(
            item for item in STORE.projects if item["id"] == job["project_id"]
        )
        generation_mode = job.get("generation_mode", "explore")
        parent_image_id = job.get("parent_image_id")
        preserve = job.get("preserve", [])
    STORE.update_job(job_id, status="running", started_at=utc_now(), progress=0)
    image_ids: list[str] = []
    try:
        for index in range(count):
            if provider == "demo":
                time.sleep(0.35)
            record = create_image_record(
                job_id,
                poem,
                style,
                index + int(time.time()),
                provider,
                ratio,
                note,
                project,
                generation_mode,
                parent_image_id,
                preserve,
            )
            STORE.add_image(record)
            image_ids.append(record["id"])
            STORE.update_job(
                job_id,
                progress=round((index + 1) / count * 100),
                image_ids=image_ids,
            )
        STORE.update_job(
            job_id,
            status="completed",
            progress=100,
            image_ids=image_ids,
            finished_at=utc_now(),
        )
    except Exception as exc:  # surfaced to the UI with a recoverable failed job
        STORE.update_job(
            job_id,
            status="failed",
            error=str(exc),
            image_ids=image_ids,
            finished_at=utc_now(),
        )


def estimated_unit_cost(provider: str) -> float:
    if provider == "demo":
        return 0.0
    try:
        return max(0.0, float(os.getenv("TANG_IMAGE_ESTIMATED_COST", "0.06")))
    except ValueError:
        return 0.06


def _batch_task_poem(task: dict[str, Any]) -> dict[str, Any]:
    prompt = task.get("prompt") or {}
    poem = dict(prompt.get("poem") or {})
    requirement = (prompt.get("requirement") or {}).get("content") or {}
    direction = (prompt.get("direction") or {}).get("content") or {}
    poem["lines"] = poem.get("lines") or task.get("lines") or []
    poem["theme"] = poem.get("theme") or task.get("theme", "")
    poem["mood"] = poem.get("mood") or task.get("mood", "")
    poem["imagery"] = requirement.get("core_imagery") or []
    poem["avoid"] = list(
        dict.fromkeys(
            [
                *(requirement.get("avoid") or []),
                *(direction.get("avoid") or []),
            ]
        )
    )
    poem["visual_brief"] = "；".join(
        item
        for item in (
            direction.get("subject"),
            direction.get("shot"),
            direction.get("foreground"),
            direction.get("midground"),
            direction.get("background"),
            direction.get("action"),
            direction.get("lighting"),
            direction.get("palette"),
            direction.get("whitespace"),
        )
        if item
    )
    return poem


def _batch_task_note(task: dict[str, Any]) -> str:
    prompt = task.get("prompt") or {}
    direction = (prompt.get("direction") or {}).get("content") or {}
    rework = prompt.get("rework") or {}
    preserve = "、".join(direction.get("preserve") or [])
    avoid = "、".join(direction.get("avoid") or [])
    pieces = [
        f"画面方向：{direction.get('title', '')}",
        f"主体：{direction.get('subject', '')}",
        f"景别：{direction.get('shot', '')}",
        f"前景：{direction.get('foreground', '')}",
        f"中景：{direction.get('midground', '')}",
        f"背景：{direction.get('background', '')}",
        f"光线：{direction.get('lighting', '')}",
        f"色彩：{direction.get('palette', '')}",
        f"留白：{direction.get('whitespace', '')}",
        f"必须保持：{preserve}",
        f"禁止：{avoid}",
    ]
    if rework:
        pieces.extend(
            [
                f"返工保持：{'、'.join(rework.get('preserve') or [])}",
                f"返工修改：{'、'.join(rework.get('change') or [])}",
                f"返工禁止：{'、'.join(rework.get('avoid') or [])}",
                f"返工备注：{rework.get('note', '')}",
            ]
        )
    return "；".join(piece for piece in pieces if not piece.endswith("："))


def _classify_batch_error(exc: Exception) -> tuple[str, bool]:
    message = str(exc)
    lowered = message.lower()
    if "429" in lowered or "rate" in lowered:
        return "RATE_LIMITED", True
    if any(code in lowered for code in ("500", "502", "503", "504")):
        return "PROVIDER_UNAVAILABLE", True
    if any(term in lowered for term in ("timeout", "超时", "无法连接", "temporar")):
        return "NETWORK_ERROR", True
    if "content" in lowered and "policy" in lowered:
        return "CONTENT_BLOCKED", False
    return "GENERATION_FAILED", False


def register_image_qc(
    store: SopStore,
    record: dict[str, Any],
    task: dict[str, Any],
) -> dict[str, Any]:
    try:
        path = local_image_path(record)
        local_inspection = inspect_image(path, task["aspect_ratio"])
    except Exception as exc:
        path = None
        local_inspection = {
            "version": QC_VERSION,
            "status": "manual_required",
            "score": 0,
            "hard_failures": [],
            "warnings": ["qc_engine_unavailable"],
            "checks": {"inspection_completed": False},
            "coverage": [],
            "file_path": "",
            "mime_type": "",
            "checksum": "",
            "perceptual_hash": "",
            "file_size": 0,
            "width": 0,
            "height": 0,
        }
        structured_log(
            "qc.local_inspection_unavailable",
            level="warning",
            task_id=task.get("id"),
            error_type=type(exc).__name__,
        )
    policy_record = store.published_qc_policy(
        str(task.get("project_id") or SOP_DEFAULT_PROJECT_ID)
    )
    prompt = task.get("prompt") if isinstance(task.get("prompt"), dict) else {}
    direction = prompt.get("direction") or {}
    context = {
        "poem": prompt.get("poem") or {},
        "requirement": (prompt.get("requirement") or {}).get("content") or {},
        "direction": {
            "type": direction.get("type"),
            **(direction.get("content") or {}),
        },
        "style": prompt.get("style") or {},
    }
    if local_inspection.get("hard_failures") or path is None:
        visual_result = None
        reviewer_metadata = {
            "reviewer_kind": "skipped",
            "unavailable_code": (
                "visual_review_skipped_local_hard_fail"
                if local_inspection.get("hard_failures")
                else "visual_review_skipped_local_inspection_unavailable"
            ),
        }
    else:
        visual_result, reviewer_metadata = review_image(
            path,
            image_provider=str(task.get("provider") or "demo"),
            context=context,
            policy_version_id=policy_record["id"],
        )
    inspection = compose_qc_result(
        local_inspection,
        visual_result,
        policy_record["content"],
        policy_version_id=policy_record["id"],
        reviewer_metadata=reviewer_metadata,
    )
    if visual_result is None and not local_inspection.get("hard_failures"):
        structured_log(
            "qc.visual_review_unavailable",
            level="warning",
            task_id=task.get("id"),
            provider=task.get("provider"),
            reason=reviewer_metadata.get("unavailable_code"),
        )
    production_image = store.register_production_image(record, task, inspection)
    qc = production_image.get("qc") or {}
    if STORE is not None:
        STORE.update_image(
            record["id"],
            production_status=production_image["status"],
            qc_status=qc.get("status"),
            qc_score=qc.get("score"),
            qc_hard_failures=qc.get("hard_failures", []),
            qc_warnings=qc.get("warnings", []),
        )
    return production_image


def run_batch_worker(batch_id: str) -> None:
    assert STORE is not None
    store = get_sop_store()
    structured_log("batch.worker_started", batch_id=batch_id)
    try:
        while True:
            state = store.execution_state(batch_id)
            if state["batch"]["status"] not in {"queued", "running"}:
                return
            circuit = PROVIDER_CIRCUITS.status(state["batch"]["provider"])
            if circuit["state"] == "open":
                paused = store.pause_provider_batches(
                    state["batch"]["provider"],
                    reason=(
                        "Provider 连续失败触发熔断；"
                        f"约 {circuit['retry_after_seconds']} 秒后可人工恢复。"
                    ),
                    actor={"id": "provider-circuit", "role": "system"},
                )
                structured_log(
                    "provider.circuit_paused_batches",
                    level="warning",
                    provider=state["batch"]["provider"],
                    batch_id=batch_id,
                    paused_count=len(paused),
                    retry_after_seconds=circuit["retry_after_seconds"],
                )
                return
            task = store.claim_next_task(batch_id)
            if task is None:
                state = store.execution_state(batch_id)
                if state["batch"]["status"] not in {"queued", "running"}:
                    return
                if state["counts"].get("retry_waiting", 0):
                    time.sleep(0.25)
                    continue
                return
            started = time.monotonic()
            structured_log(
                "task.claimed",
                batch_id=batch_id,
                task_id=task["id"],
                attempt_id=task["attempt_id"],
                provider=task["provider"],
            )
            try:
                with BATCH_TASK_SEMAPHORE:
                    existing = next(
                        (
                            image
                            for image in STORE.images
                            if image.get("sop_task_id") == task["id"]
                        ),
                        None,
                    )
                    if existing:
                        production_image = register_image_qc(store, existing, task)
                        store.complete_task(
                            task["id"],
                            task["attempt_id"],
                            output_image_id=existing["id"],
                            actual_cost=0,
                            duration_ms=round((time.monotonic() - started) * 1000),
                            response={
                                "recovered_existing_image": True,
                                "production_status": production_image["status"],
                            },
                        )
                        structured_log(
                            "task.succeeded",
                            batch_id=batch_id,
                            task_id=task["id"],
                            attempt_id=task["attempt_id"],
                            image_id=existing["id"],
                            recovered=True,
                        )
                        PROVIDER_CIRCUITS.record_success(task["provider"])
                        continue
                    style = (task.get("prompt") or {}).get("style")
                    if not isinstance(style, dict):
                        style = STORE.style_by_id.get(task["style_id"])
                    if not style:
                        raise RuntimeError("批次绑定的风格版本不存在。")
                    project = store.project(task["project_id"])
                    poem = _batch_task_poem(task)
                    rework = (task.get("prompt") or {}).get("rework") or {}
                    record = create_image_record(
                        task["id"],
                        poem,
                        style,
                        task["sample_index"] + int(time.time()),
                        task["provider"],
                        task["aspect_ratio"],
                        _batch_task_note(task),
                        {
                            "id": project["id"],
                            "name": project["name"],
                            "purpose": project["purpose"],
                        },
                        "converge" if rework else "explore",
                        rework.get("parent_image_id"),
                        rework.get("preserve") or [],
                        compiled_prompt=(task.get("prompt") or {}).get("compiled"),
                    )
                    record.update(
                        {
                            "sop_batch_id": batch_id,
                            "sop_task_id": task["id"],
                            "direction_id": task["direction_id"],
                            "idempotency_key": task["idempotency_key"],
                            "rework_order_id": task.get("rework_order_id"),
                        }
                    )
                    STORE.add_image(record)
                    production_image = register_image_qc(store, record, task)
                    actual_cost = (
                        0.0
                        if task["provider"] == "demo"
                        else float(task["batch_settings"].get("unit_cost", 0))
                    )
                    store.complete_task(
                        task["id"],
                        task["attempt_id"],
                        output_image_id=record["id"],
                        actual_cost=actual_cost,
                        duration_ms=round((time.monotonic() - started) * 1000),
                        response={
                            "image_id": record["id"],
                            "url": record["url"],
                            "production_status": production_image["status"],
                            "qc_result_id": (production_image.get("qc") or {}).get("id"),
                        },
                    )
                    structured_log(
                        "task.succeeded",
                        batch_id=batch_id,
                        task_id=task["id"],
                        attempt_id=task["attempt_id"],
                        image_id=record["id"],
                        duration_ms=round((time.monotonic() - started) * 1000),
                    )
                    PROVIDER_CIRCUITS.record_success(task["provider"])
            except Exception as exc:
                error_code, retryable = _classify_batch_error(exc)
                store.fail_task(
                    task["id"],
                    task["attempt_id"],
                    error_code=error_code,
                    error_message=str(exc),
                    retryable=retryable,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
                structured_log(
                    "task.failed",
                    level="error" if not retryable else "warning",
                    batch_id=batch_id,
                    task_id=task["id"],
                    attempt_id=task["attempt_id"],
                    error_code=error_code,
                    retryable=retryable,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
                if retryable:
                    circuit = PROVIDER_CIRCUITS.record_failure(
                        task["provider"], error_code
                    )
                    if circuit["state"] == "open":
                        paused = store.pause_provider_batches(
                            task["provider"],
                            reason=(
                                "Provider 连续失败触发熔断；"
                                f"约 {circuit['retry_after_seconds']} 秒后可人工恢复。"
                            ),
                            actor={"id": "provider-circuit", "role": "system"},
                        )
                        structured_log(
                            "provider.circuit_opened",
                            level="error",
                            provider=task["provider"],
                            batch_id=batch_id,
                            paused_count=len(paused),
                            error_code=error_code,
                            retry_after_seconds=circuit["retry_after_seconds"],
                        )
    finally:
        with BATCH_WORKERS_LOCK:
            current = BATCH_WORKERS.get(batch_id)
            if current is threading.current_thread():
                BATCH_WORKERS.pop(batch_id, None)
        structured_log("batch.worker_stopped", batch_id=batch_id)


def ensure_batch_worker(batch_id: str) -> None:
    with BATCH_WORKERS_LOCK:
        current = BATCH_WORKERS.get(batch_id)
        if current and current.is_alive():
            return
        worker = threading.Thread(
            target=run_batch_worker,
            args=(batch_id,),
            daemon=True,
            name=f"batch-{batch_id[-8:]}",
        )
        BATCH_WORKERS[batch_id] = worker
        worker.start()


def seed_demo_gallery() -> None:
    assert STORE is not None
    if STORE.images:
        return
    pairings = [
        ("jing-ye-si", "light-gongbi"),
        ("jiang-xue", "ink-whitespace"),
        ("chun-xiao", "childrens-book"),
        ("deng-guan-que-lou", "new-chinese-decorative"),
        ("shan-ju-qiu-ming", "warm-natural"),
        ("feng-qiao-ye-bo", "moonlit-blue-green"),
    ]
    for index, (poem_id, style_id) in enumerate(pairings):
        record = create_image_record(
            "seed",
            STORE.poem_by_id[poem_id],
            STORE.style_by_id[style_id],
            index,
            "demo",
            "portrait",
            "",
            STORE.projects[0],
        )
        record["is_seed"] = True
        STORE.add_image(record)
    sample_assets = [
        ("jing-ye-si", "light-gongbi", "jing-ye-si-light-gongbi.png"),
        ("jiang-xue", "ink-whitespace", "jiang-xue-ink-whitespace.png"),
        ("chun-xiao", "childrens-book", "chun-xiao-childrens-book.png"),
    ]
    for poem_id, style_id, filename in sample_assets:
        path = PUBLIC_DIR / "samples" / filename
        if not path.is_file():
            continue
        poem = STORE.poem_by_id[poem_id]
        style = STORE.style_by_id[style_id]
        image_id = hashlib.sha256(f"sample:{filename}".encode()).hexdigest()[:32]
        STORE.add_image(
            {
                "id": image_id,
                "job_id": "sample",
                "poem_id": poem_id,
                "poem_title": poem["title"],
                "author": poem["author"],
                "style_id": style_id,
                "style_name": style["name"],
                "project_id": STORE.projects[0]["id"],
                "project_name": STORE.projects[0]["name"],
                "provider": "sample",
                "url": f"/samples/{filename}",
                "aspect_ratio": "portrait",
                "prompt": build_prompt(
                    poem,
                    style,
                    "",
                    {
                        "project_name": STORE.projects[0]["name"],
                        "purpose": STORE.projects[0]["purpose"],
                    },
                ),
                "generation_mode": "explore",
                "parent_image_id": None,
                "decision": "candidate",
                "feedback_tags": [],
                "review_note": "",
                "qc": {},
                "favorite": False,
                "is_seed": True,
                "created_at": utc_now(),
            }
        )


class StudioHandler(BaseHTTPRequestHandler):
    server_version = f"TangPoemStudio/{APP_VERSION}"

    def _request_id(self) -> str:
        current = getattr(self, "request_id", "")
        if current:
            return current
        incoming = self.headers.get("X-Request-ID", "") if self.headers else ""
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", incoming):
            incoming = f"req_{uuid.uuid4().hex}"
        self.request_id = incoming
        return incoming

    def log_message(self, format: str, *args: Any) -> None:
        path = urlparse(getattr(self, "path", "")).path
        context: dict[str, Any] = {}
        for field, pattern in (
            ("batch_id", r"(batch_[a-f0-9]{32})"),
            ("task_id", r"(task_[a-f0-9]{32})"),
            ("image_id", r"/api/images/([a-f0-9]{32})"),
        ):
            match = re.search(pattern, path)
            if match:
                context[field] = match.group(1)
        structured_log(
            "http.request",
            request_id=self._request_id(),
            method=getattr(self, "command", ""),
            path=path,
            status=args[1] if len(args) > 1 else None,
            message=(format % args),
            **context,
        )

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        request_id = self._request_id()
        if int(status) >= 400 and isinstance(payload, dict) and "request_id" not in payload:
            payload = {**payload, "request_id": request_id}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Request-ID", request_id)
        self.end_headers()
        self.wfile.write(body)

    def _send_workflow_error(self, exc: WorkflowError) -> None:
        self._send_json(
            {
                "code": exc.code,
                "message": str(exc),
                "request_id": self._request_id(),
            },
            exc.status,
        )

    def _send_download(
        self,
        payload: bytes,
        *,
        content_type: str,
        filename: str,
    ) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Request-ID", self._request_id())
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 2_000_000:
            raise ValueError("请求内容为空或过大。")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("请求不是有效 JSON。") from exc

    def _serve_file(self, path: Path, cache: bool = True, head_only: bool = False) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "public, max-age=86400" if cache else "no-cache")
        self.send_header("X-Request-ID", self._request_id())
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def do_HEAD(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self._serve_file(PUBLIC_DIR / "index.html", cache=False, head_only=True)
            return
        if path.startswith("/generated/"):
            name = Path(path).name
            if not re.fullmatch(r"[a-f0-9]{32}\.(?:png|jpg|jpeg|webp|svg)", name):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._serve_file(GENERATED_DIR / name, head_only=True)
            return
        candidate = (PUBLIC_DIR / path.lstrip("/")).resolve()
        try:
            candidate.relative_to(PUBLIC_DIR)
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._serve_file(candidate, cache=False, head_only=True)

    def do_GET(self) -> None:  # noqa: N802
        assert STORE is not None
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/api/health":
            workflow_health = get_sop_store().health()
            provider_status = provider_runtime_status()
            self._send_json(
                {
                    "ok": workflow_health["status"] == "ok",
                    "version": APP_VERSION,
                    "provider": provider_name(),
                    "live_generation": provider_name() == "openai",
                    "time": utc_now(),
                    "workflow": workflow_health,
                    "provider_status": provider_status,
                }
            )
            return
        if path == "/api/bootstrap":
            data = STORE.snapshot()
            data["config"] = {
                "version": APP_VERSION,
                "provider": provider_name(),
                "live_generation": provider_name() == "openai",
                "model": os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2"),
                "provider_status": provider_runtime_status(),
            }
            self._send_json(data)
            return
        if path == "/api/sop/bootstrap":
            try:
                project_id = query.get("project_id", [SOP_DEFAULT_PROJECT_ID])[0]
                payload = get_sop_store().snapshot(project_id)
                payload["backups"] = list_backups(DATA_DIR / "backups")
                payload["provider_status"] = provider_runtime_status()
                self._send_json(payload)
            except WorkflowError as exc:
                self._send_workflow_error(exc)
            return
        if path == "/api/instructions":
            project_id = query.get("project_id", [SOP_DEFAULT_PROJECT_ID])[0]
            self._send_json({"items": get_sop_store().instructions(project_id)})
            return
        if path == "/api/schemas/art-bible":
            self._send_json(art_bible_schema_document())
            return
        if path == "/api/schemas/style-pack":
            self._send_json(style_pack_schema_document())
            return
        if path == "/api/art-bibles":
            project_id = query.get("project_id", [SOP_DEFAULT_PROJECT_ID])[0]
            self._send_json(
                {"items": get_sop_store().art_bible_versions(project_id)}
            )
            return
        if path == "/api/style-benchmark-poems":
            project_id = query.get("project_id", [SOP_DEFAULT_PROJECT_ID])[0]
            self._send_json(
                {"items": get_sop_store().benchmark_poems(project_id)}
            )
            return
        if path == "/api/style-benchmark-runs":
            project_id = query.get("project_id", [SOP_DEFAULT_PROJECT_ID])[0]
            self._send_json(
                {
                    "items": get_sop_store().style_benchmark_runs(
                        project_id,
                        style_version_id=query.get("style_version_id", [None])[0],
                    )
                }
            )
            return
        if path == "/api/style-packs":
            project_id = query.get("project_id", [SOP_DEFAULT_PROJECT_ID])[0]
            try:
                self._send_json(
                    {
                        "items": get_sop_store().style_pack_versions(
                            project_id,
                            status=query.get("status", [None])[0],
                        )
                    }
                )
            except WorkflowError as exc:
                self._send_workflow_error(exc)
            return
        if path == "/api/provider-status":
            self._send_json(provider_runtime_status())
            return
        summary_match = re.fullmatch(r"/api/projects/([a-z0-9-]{3,80})/summary", path)
        if summary_match:
            try:
                self._send_json(get_sop_store().summary(summary_match.group(1)))
            except WorkflowError as exc:
                self._send_workflow_error(exc)
            return
        if path == "/api/reports/production":
            try:
                self._send_json(
                    get_sop_store().production_report(
                        query.get("project_id", [SOP_DEFAULT_PROJECT_ID])[0],
                        days=int(query.get("days", ["7"])[0]),
                    )
                )
            except (WorkflowError, ValueError) as exc:
                if isinstance(exc, WorkflowError):
                    self._send_workflow_error(exc)
                else:
                    self._send_json(
                        {"code": "INVALID_REPORT_RANGE", "message": "日报范围无效。"},
                        HTTPStatus.BAD_REQUEST,
                    )
            return
        if path == "/api/reports/data-quality":
            try:
                self._send_json(
                    get_sop_store().data_quality_report(
                        query.get("project_id", [SOP_DEFAULT_PROJECT_ID])[0]
                    )
                )
            except WorkflowError as exc:
                self._send_workflow_error(exc)
            return
        if path == "/api/poems":
            try:
                result = get_sop_store().list_poems(
                    query.get("project_id", [SOP_DEFAULT_PROJECT_ID])[0],
                    status=query.get("status", [None])[0],
                    query=query.get("q", [""])[0],
                    limit=int(query.get("limit", ["100"])[0]),
                    offset=int(query.get("offset", ["0"])[0]),
                )
                self._send_json(result)
            except (WorkflowError, ValueError) as exc:
                if isinstance(exc, WorkflowError):
                    self._send_workflow_error(exc)
                else:
                    self._send_json(
                        {"code": "INVALID_PAGINATION", "message": "分页参数无效。"},
                        HTTPStatus.BAD_REQUEST,
                    )
            return
        poem_detail_match = re.fullmatch(r"/api/poems/([a-z0-9-]{3,80})", path)
        if poem_detail_match:
            try:
                self._send_json(get_sop_store().poem_detail(poem_detail_match.group(1)))
            except WorkflowError as exc:
                self._send_workflow_error(exc)
            return
        if path == "/api/requirements":
            self._send_json(
                {
                    "items": get_sop_store().requirements(
                        query.get("project_id", [SOP_DEFAULT_PROJECT_ID])[0]
                    )
                }
            )
            return
        if path == "/api/schemas/requirement-card":
            self._send_json(requirement_schema_document())
            return
        if path == "/api/schemas/direction-proposal":
            self._send_json(direction_schema_document())
            return
        if path == "/api/schemas/review-result":
            self._send_json(review_result_schema_document())
            return
        if path == "/api/schemas/qc-policy":
            self._send_json(qc_policy_schema_document())
            return
        if path == "/api/schemas/poem-import":
            self._send_json(poem_import_schema_document())
            return
        if path == "/api/templates/poem-import":
            format_name = query.get("format", ["json"])[0].lower()
            if format_name == "csv":
                self._send_download(
                    csv_template_text().encode("utf-8-sig"),
                    content_type="text/csv; charset=utf-8",
                    filename="poem-import-template.csv",
                )
            elif format_name == "json":
                self._send_download(
                    json.dumps(
                        json_template_document(), ensure_ascii=False, indent=2
                    ).encode("utf-8"),
                    content_type="application/json; charset=utf-8",
                    filename="poem-import-template.json",
                )
            else:
                self._send_json(
                    {
                        "code": "IMPORT_FORMAT_UNSUPPORTED",
                        "message": "模板格式只支持 json 或 csv。",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
            return
        if path == "/api/qc-policies":
            project_id = query.get("project_id", [SOP_DEFAULT_PROJECT_ID])[0]
            self._send_json(
                {"items": get_sop_store().qc_policy_versions(project_id)}
            )
            return
        if path == "/api/qc-calibration":
            project_id = query.get("project_id", [SOP_DEFAULT_PROJECT_ID])[0]
            self._send_json(get_sop_store().qc_calibration_report(project_id))
            return
        if path == "/api/requirement-generation-runs":
            try:
                unresolved = query.get("unresolved", [""])[0].lower()
                self._send_json(
                    {
                        "items": get_sop_store().requirement_generation_runs(
                            query.get("project_id", [SOP_DEFAULT_PROJECT_ID])[0],
                            poem_id=query.get("poem_id", [None])[0],
                            status=query.get("status", [None])[0],
                            unresolved_only=unresolved in {"1", "true", "yes"},
                            limit=int(query.get("limit", ["200"])[0]),
                        )
                    }
                )
            except (WorkflowError, ValueError) as exc:
                if isinstance(exc, WorkflowError):
                    self._send_workflow_error(exc)
                else:
                    self._send_json(
                        {
                            "code": "INVALID_PAGINATION",
                            "message": "需求生成运行记录分页参数无效。",
                        },
                        HTTPStatus.BAD_REQUEST,
                    )
            return
        if path == "/api/direction-generation-runs":
            try:
                unresolved = query.get("unresolved", [""])[0].lower()
                self._send_json(
                    {
                        "items": get_sop_store().direction_generation_runs(
                            query.get("project_id", [SOP_DEFAULT_PROJECT_ID])[0],
                            poem_id=query.get("poem_id", [None])[0],
                            status=query.get("status", [None])[0],
                            unresolved_only=unresolved in {"1", "true", "yes"},
                            limit=int(query.get("limit", ["200"])[0]),
                        )
                    }
                )
            except (WorkflowError, ValueError) as exc:
                if isinstance(exc, WorkflowError):
                    self._send_workflow_error(exc)
                else:
                    self._send_json(
                        {
                            "code": "INVALID_PAGINATION",
                            "message": "方向生成运行记录分页参数无效。",
                        },
                        HTTPStatus.BAD_REQUEST,
                    )
            return
        if path == "/api/directions":
            self._send_json(
                {
                    "items": get_sop_store().directions(
                        query.get("project_id", [SOP_DEFAULT_PROJECT_ID])[0]
                    )
                }
            )
            return
        if path == "/api/audit-events":
            self._send_json(
                {
                    "items": get_sop_store().audit_events(
                        target_type=query.get("target_type", [None])[0],
                        target_id=query.get("target_id", [None])[0],
                        limit=int(query.get("limit", ["100"])[0]),
                    )
                }
            )
            return
        if path == "/api/batches":
            project_id = query.get("project_id", [SOP_DEFAULT_PROJECT_ID])[0]
            self._send_json({"items": get_sop_store().batches(project_id)})
            return
        if path == "/api/tasks":
            try:
                self._send_json(
                    get_sop_store().task_page(
                        project_id=query.get(
                            "project_id", [SOP_DEFAULT_PROJECT_ID]
                        )[0],
                        batch_id=query.get("batch_id", [None])[0],
                        status=query.get("status", [None])[0],
                        poem_id=query.get("poem_id", [None])[0],
                        error_code=query.get("error_code", [None])[0],
                        q=query.get("q", [""])[0],
                        limit=int(query.get("limit", ["50"])[0]),
                        offset=int(query.get("offset", ["0"])[0]),
                    )
                )
            except (WorkflowError, ValueError) as exc:
                if isinstance(exc, WorkflowError):
                    self._send_workflow_error(exc)
                else:
                    self._send_json(
                        {"code": "INVALID_PAGINATION", "message": "分页参数无效。"},
                        HTTPStatus.BAD_REQUEST,
                    )
            return
        if path == "/api/review-queue":
            project_id = query.get("project_id", [SOP_DEFAULT_PROJECT_ID])[0]
            include_blocked = query.get("include_blocked", ["false"])[0].lower() in {
                "1",
                "true",
                "yes",
            }
            self._send_json(
                get_sop_store().review_queue(
                    project_id,
                    include_blocked=include_blocked,
                )
            )
            return
        if path == "/api/rework-orders":
            project_id = query.get("project_id", [SOP_DEFAULT_PROJECT_ID])[0]
            self._send_json({"items": get_sop_store().rework_orders(project_id)})
            return
        if path == "/api/final-assets":
            project_id = query.get("project_id", [SOP_DEFAULT_PROJECT_ID])[0]
            self._send_json(
                {
                    "items": get_sop_store().final_assets(
                        project_id,
                        query=query.get("q", [""])[0],
                        current_only=query.get("current_only", ["true"])[0].lower()
                        not in {"0", "false", "no"},
                        limit=int(query.get("limit", ["500"])[0]),
                    )
                }
            )
            return
        if path == "/api/exports":
            project_id = query.get("project_id", [SOP_DEFAULT_PROJECT_ID])[0]
            self._send_json(
                {
                    "items": [
                        present_export_package(package)
                        for package in get_sop_store().export_packages(project_id)
                    ]
                }
            )
            return
        if path == "/api/backups":
            self._send_json({"items": list_backups(DATA_DIR / "backups")})
            return
        if path == "/api/images":
            self._send_json({"images": list(reversed(STORE.images[-200:]))})
            return
        if path == "/api/jobs":
            self._send_json({"jobs": list(reversed(STORE.jobs[-30:]))})
            return
        if path.startswith("/generated/"):
            name = Path(path).name
            if not re.fullmatch(r"[a-f0-9]{32}\.(?:png|jpg|jpeg|webp|svg)", name):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._serve_file(GENERATED_DIR / name)
            return
        if path.startswith("/exports/"):
            export_root = (DATA_DIR / "exports").resolve()
            candidate = (export_root / path.removeprefix("/exports/")).resolve()
            try:
                candidate.relative_to(export_root)
            except ValueError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._serve_file(candidate, cache=False)
            return
        if path == "/":
            self._serve_file(PUBLIC_DIR / "index.html", cache=False)
            return
        candidate = (PUBLIC_DIR / path.lstrip("/")).resolve()
        try:
            candidate.relative_to(PUBLIC_DIR)
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if candidate.is_file():
            self._serve_file(candidate, cache=False)
        else:
            # SPA fallback.
            self._serve_file(PUBLIC_DIR / "index.html", cache=False)

    def do_POST(self) -> None:  # noqa: N802
        assert STORE is not None
        path = urlparse(self.path).path
        if path == "/api/instructions":
            try:
                body = self._read_json()
                instruction = get_sop_store().create_instruction_version(
                    str(body.get("project_id") or SOP_DEFAULT_PROJECT_ID),
                    name=str(body.get("name") or ""),
                    content=body.get("content"),
                    actor=body.get("actor"),
                )
                self._send_json({"instruction": instruction}, HTTPStatus.CREATED)
            except WorkflowError as exc:
                self._send_workflow_error(exc)
            return
        instruction_publish = re.fullmatch(
            r"/api/instructions/(instruction_[a-f0-9]{32})/publish", path
        )
        if instruction_publish:
            try:
                body = self._read_json()
                instruction = get_sop_store().publish_instruction_version(
                    instruction_publish.group(1), actor=body.get("actor")
                )
                self._send_json({"instruction": instruction})
            except WorkflowError as exc:
                self._send_workflow_error(exc)
            return
        instruction_retire = re.fullmatch(
            r"/api/instructions/(instruction_[a-f0-9]{32})/retire", path
        )
        if instruction_retire:
            try:
                body = self._read_json()
                instruction = get_sop_store().retire_instruction_draft(
                    instruction_retire.group(1),
                    reason=str(body.get("reason") or ""),
                    actor=body.get("actor"),
                )
                self._send_json({"instruction": instruction})
            except WorkflowError as exc:
                self._send_workflow_error(exc)
            return
        if path == "/api/style-packs":
            try:
                body = self._read_json()
                style = get_sop_store().create_style_pack_version(
                    str(body.get("project_id") or SOP_DEFAULT_PROJECT_ID),
                    style_id=str(body.get("style_id") or ""),
                    name=str(body.get("name") or ""),
                    short_name=str(body.get("short_name") or ""),
                    description=str(body.get("description") or ""),
                    semantic_version=str(body.get("semantic_version") or ""),
                    release_notes=str(body.get("release_notes") or ""),
                    art_bible_version_id=str(
                        body.get("art_bible_version_id") or ""
                    ),
                    prompt_fragment=str(body.get("prompt_fragment") or ""),
                    palette=body.get("palette"),
                    settings=body.get("settings"),
                    applicable_topics=body.get("applicable_topics"),
                    visual_traits=body.get("visual_traits"),
                    character_design=body.get("character_design"),
                    avoid=body.get("avoid"),
                    risks=body.get("risks"),
                    positive_examples=body.get("positive_examples"),
                    negative_examples=body.get("negative_examples"),
                    actor=body.get("actor"),
                )
                self._send_json({"style": style}, HTTPStatus.CREATED)
            except WorkflowError as exc:
                self._send_workflow_error(exc)
            return
        if path == "/api/art-bibles":
            try:
                body = self._read_json()
                art_bible = get_sop_store().create_art_bible_version(
                    str(body.get("project_id") or SOP_DEFAULT_PROJECT_ID),
                    semantic_version=str(body.get("semantic_version") or ""),
                    name=str(body.get("name") or ""),
                    content=body.get("content"),
                    release_notes=str(body.get("release_notes") or ""),
                    actor=body.get("actor"),
                )
                self._send_json({"art_bible": art_bible}, HTTPStatus.CREATED)
            except WorkflowError as exc:
                self._send_workflow_error(exc)
            return
        art_bible_publish = re.fullmatch(
            r"/api/art-bibles/(artbible_[a-f0-9]{32})/publish", path
        )
        if art_bible_publish:
            try:
                body = self._read_json()
                result = get_sop_store().publish_art_bible_version(
                    art_bible_publish.group(1), actor=body.get("actor")
                )
                self._send_json({"art_bible": result})
            except WorkflowError as exc:
                self._send_workflow_error(exc)
            return
        style_publish = re.fullmatch(
            r"/api/style-packs/(stylev_[a-f0-9]{32})/publish", path
        )
        if style_publish:
            try:
                body = self._read_json()
                style = get_sop_store().publish_style_pack_version(
                    style_publish.group(1), actor=body.get("actor")
                )
                self._send_json({"style": style})
            except WorkflowError as exc:
                self._send_workflow_error(exc)
            return
        style_benchmark_create = re.fullmatch(
            r"/api/style-packs/(stylev_[a-f0-9]{32})/benchmark", path
        )
        if style_benchmark_create:
            try:
                body = self._read_json()
                created = get_sop_store().create_style_benchmark_run(
                    style_benchmark_create.group(1),
                    body.get("poem_ids") or [],
                    provider=str(body.get("provider") or provider_name()),
                    model=str(
                        body.get("model")
                        or os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2")
                    ),
                    unit_cost=float(body.get("unit_cost") or 0),
                    actor=body.get("actor"),
                )
                started = get_sop_store().start_style_benchmark(
                    created["run"]["id"], actor=body.get("actor")
                )
                if started["batch"]["status"] != "budget_blocked":
                    ensure_batch_worker(started["batch"]["id"])
                self._send_json(started, HTTPStatus.CREATED)
            except (WorkflowError, TypeError, ValueError) as exc:
                if isinstance(exc, WorkflowError):
                    self._send_workflow_error(exc)
                else:
                    self._send_json(
                        {
                            "code": "INVALID_BENCHMARK_SETTINGS",
                            "message": "基准测试参数格式无效。",
                        },
                        HTTPStatus.BAD_REQUEST,
                    )
            return
        style_benchmark_evaluate = re.fullmatch(
            r"/api/style-benchmark-runs/(stylebench_[a-f0-9]{32})/evaluate",
            path,
        )
        if style_benchmark_evaluate:
            try:
                body = self._read_json()
                result = get_sop_store().evaluate_style_benchmark(
                    style_benchmark_evaluate.group(1),
                    style_match_score=body.get("style_match_score"),
                    off_topic_rate=body.get("off_topic_rate"),
                    favorite_rate=body.get("favorite_rate"),
                    notes=str(body.get("notes") or ""),
                    actor=body.get("actor"),
                )
                self._send_json({"run": result})
            except WorkflowError as exc:
                self._send_workflow_error(exc)
            return
        if path == "/api/backups":
            try:
                body = self._read_json()
                result = create_backup(
                    get_sop_store().database_path,
                    DATA_DIR,
                    DATA_DIR / "backups",
                )
                get_sop_store().record_system_audit(
                    "backup.created",
                    "backup",
                    result["name"],
                    after=result,
                    actor=body.get("actor"),
                )
                self._send_json({"backup": result}, HTTPStatus.CREATED)
            except (OSError, RuntimeError, ValueError) as exc:
                self._send_json(
                    {
                        "code": "BACKUP_FAILED",
                        "message": f"备份失败：{exc}",
                        "request_id": self.headers.get(
                            "X-Request-ID", "local-request"
                        ),
                    },
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            return
        backup_verify = re.fullmatch(r"/api/backups/(backup-[A-Za-z0-9-]+)/verify", path)
        if backup_verify:
            try:
                body = self._read_json()
                result = verify_backup(
                    DATA_DIR / "backups" / backup_verify.group(1)
                )
                get_sop_store().record_system_audit(
                    "backup.verified",
                    "backup",
                    result["name"],
                    after=result,
                    actor=body.get("actor"),
                )
                self._send_json({"backup": result})
            except (OSError, RuntimeError, ValueError) as exc:
                self._send_json(
                    {
                        "code": "BACKUP_VERIFY_FAILED",
                        "message": f"备份校验失败：{exc}",
                        "request_id": self.headers.get(
                            "X-Request-ID", "local-request"
                        ),
                    },
                    HTTPStatus.BAD_REQUEST,
                )
            return
        image_workflow_action = re.fullmatch(
            r"/api/images/([a-f0-9]{32})/(decision|qc-override|qc-calibration|rework|finalize)",
            path,
        )
        if image_workflow_action:
            try:
                body = self._read_json()
                image_id = image_workflow_action.group(1)
                action = image_workflow_action.group(2)
                if action == "decision":
                    reason_tags = body.get("reason_tags") or []
                    if not isinstance(reason_tags, list):
                        raise WorkflowError(
                            "INVALID_REASON_TAGS", "reason_tags 必须是数组。"
                        )
                    image = get_sop_store().decide_image(
                        image_id,
                        str(body.get("decision") or ""),
                        reason_tags=reason_tags,
                        note=str(body.get("note") or ""),
                        actor=body.get("actor"),
                    )
                    self._send_json({"image": image})
                elif action == "qc-override":
                    image = get_sop_store().override_qc(
                        image_id,
                        str(body.get("decision") or ""),
                        reason=str(body.get("reason") or ""),
                        actor=body.get("actor"),
                    )
                    self._send_json({"image": image})
                elif action == "qc-calibration":
                    human_scores = body.get("human_scores") or {}
                    reason_tags = body.get("reason_tags") or []
                    if not isinstance(human_scores, dict) or not isinstance(
                        reason_tags, list
                    ):
                        raise WorkflowError(
                            "INVALID_QC_CALIBRATION_PAYLOAD",
                            "human_scores 必须是对象，reason_tags 必须是数组。",
                        )
                    sample = get_sop_store().record_qc_calibration(
                        image_id,
                        human_decision=str(body.get("human_decision") or ""),
                        human_scores=human_scores,
                        reason_tags=reason_tags,
                        note=str(body.get("note") or ""),
                        actor=body.get("actor"),
                    )
                    self._send_json({"sample": sample}, HTTPStatus.CREATED)
                elif action == "rework":
                    preserve = body.get("preserve") or []
                    change = body.get("change") or []
                    avoid = body.get("avoid") or []
                    if not all(
                        isinstance(value, list)
                        for value in (preserve, change, avoid)
                    ):
                        raise WorkflowError(
                            "INVALID_REWORK_FIELDS",
                            "返工保持项、修改项和禁止项必须是数组。",
                        )
                    order = get_sop_store().create_rework_order(
                        image_id,
                        preserve=preserve,
                        change=change,
                        avoid=avoid,
                        note=str(body.get("note") or ""),
                        actor=body.get("actor"),
                    )
                    batch = get_sop_store().create_rework_batch(
                        order["id"],
                        actor=body.get("actor"),
                    )
                    batch = get_sop_store().start_batch(
                        batch["id"],
                        actor=body.get("actor"),
                    )
                    if batch["status"] != "budget_blocked":
                        ensure_batch_worker(batch["id"])
                    self._send_json(
                        {
                            "rework_order": get_sop_store().rework_order(order["id"]),
                            "batch": batch,
                        },
                        HTTPStatus.CREATED,
                    )
                else:
                    result = get_sop_store().finalize_image(
                        image_id,
                        reviewer_type=str(body.get("reviewer_type") or ""),
                        decision=str(body.get("decision") or ""),
                        reason=str(body.get("reason") or ""),
                        actor=body.get("actor"),
                    )
                    self._send_json(result)
            except (WorkflowError, TypeError) as exc:
                if isinstance(exc, WorkflowError):
                    self._send_workflow_error(exc)
                else:
                    self._send_json(
                        {
                            "code": "INVALID_PAYLOAD",
                            "message": "请求字段格式无效。",
                            "request_id": self.headers.get(
                                "X-Request-ID", "local-request"
                            ),
                        },
                        HTTPStatus.BAD_REQUEST,
                    )
            return
        if path in {"/api/exports/estimate", "/api/exports"}:
            try:
                body = self._read_json()
                poem_ids = body.get("poem_ids")
                if poem_ids is not None and not isinstance(poem_ids, list):
                    raise WorkflowError("INVALID_POEM_IDS", "poem_ids 必须是数组。")
                project_id = str(
                    body.get("project_id") or SOP_DEFAULT_PROJECT_ID
                )
                if path == "/api/exports/estimate":
                    self._send_json(
                        get_sop_store().export_estimate(
                            project_id,
                            poem_ids=poem_ids,
                        )
                    )
                else:
                    package = get_sop_store().create_export_package(
                        project_id,
                        DATA_DIR / "exports",
                        poem_ids=poem_ids,
                        actor=body.get("actor"),
                    )
                    self._send_json(
                        {"package": present_export_package(package)},
                        HTTPStatus.CREATED,
                    )
            except WorkflowError as exc:
                self._send_workflow_error(exc)
            return
        if path in {"/api/batches/estimate", "/api/batches"}:
            try:
                body = self._read_json()
                poem_ids = body.get("poem_ids")
                direction_ids = body.get("direction_ids")
                if not isinstance(poem_ids, list):
                    raise WorkflowError(
                        "POEM_IDS_REQUIRED", "poem_ids 必须是诗词 ID 数组。"
                    )
                if direction_ids is not None and not isinstance(direction_ids, list):
                    raise WorkflowError(
                        "INVALID_DIRECTION_IDS",
                        "direction_ids 必须是方向 ID 数组。",
                    )
                style_id = str(body.get("style_id") or "")
                provider = provider_name()
                model = (
                    "demo-renderer"
                    if provider == "demo"
                    else os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2")
                )
                arguments = {
                    "project_id": str(
                        body.get("project_id") or SOP_DEFAULT_PROJECT_ID
                    ),
                    "poem_ids": poem_ids,
                    "direction_ids": direction_ids,
                    "style_id": style_id,
                    "aspect_ratio": str(body.get("aspect_ratio") or "portrait"),
                    "count_per_direction": int(body.get("count_per_direction", 1)),
                    "provider": provider,
                    "model": model,
                    "unit_cost": estimated_unit_cost(provider),
                }
                if path == "/api/batches/estimate":
                    self._send_json(get_sop_store().estimate_batch(**arguments))
                else:
                    batch = get_sop_store().create_batch(
                        **arguments,
                        name=str(body.get("name") or ""),
                        priority=int(body.get("priority", 50)),
                        actor=body.get("actor"),
                    )
                    self._send_json({"batch": batch}, HTTPStatus.CREATED)
            except (WorkflowError, TypeError, ValueError) as exc:
                if isinstance(exc, WorkflowError):
                    self._send_workflow_error(exc)
                else:
                    self._send_json(
                        {
                            "code": "INVALID_BATCH_SETTINGS",
                            "message": "批次参数格式无效。",
                        },
                        HTTPStatus.BAD_REQUEST,
                    )
            return
        batch_action = re.fullmatch(
            r"/api/batches/(batch_[a-f0-9]{32})/"
            r"(start|pause|resume|cancel|retry-failed)",
            path,
        )
        if batch_action:
            try:
                body = self._read_json()
                batch_id = batch_action.group(1)
                action = batch_action.group(2)
                actor = body.get("actor")
                if action in {"start", "resume"}:
                    pending_batch = get_sop_store().batch(batch_id)
                    circuit = PROVIDER_CIRCUITS.status(pending_batch["provider"])
                    if circuit["state"] == "open":
                        raise WorkflowError(
                            "PROVIDER_CIRCUIT_OPEN",
                            "Provider 熔断中，请等待冷却后再恢复批次。",
                            status=503,
                        )
                    batch = get_sop_store().start_batch(batch_id, actor=actor)
                    if batch["status"] != "budget_blocked":
                        ensure_batch_worker(batch_id)
                    if batch["status"] == "budget_blocked":
                        self._send_json(
                            {
                                "code": "BUDGET_BLOCKED",
                                "message": "批次预计成本超过剩余预算，已阻止启动。",
                                "batch": batch,
                            },
                            HTTPStatus.CONFLICT,
                        )
                    else:
                        self._send_json({"batch": batch}, HTTPStatus.ACCEPTED)
                elif action == "pause":
                    self._send_json(
                        {
                            "batch": get_sop_store().pause_batch(
                                batch_id, actor=actor
                            )
                        }
                    )
                elif action == "cancel":
                    self._send_json(
                        {
                            "batch": get_sop_store().cancel_batch(
                                batch_id, actor=actor
                            )
                        }
                    )
                else:
                    batch = get_sop_store().retry_failed_tasks(
                        batch_id,
                        confirm_unknown=bool(body.get("confirm_unknown")),
                        actor=actor,
                    )
                    if batch["status"] == "budget_blocked":
                        self._send_json(
                            {
                                "code": "BUDGET_BLOCKED",
                                "message": "失败任务重试成本超过剩余预算。",
                                "batch": batch,
                            },
                            HTTPStatus.CONFLICT,
                        )
                    else:
                        ensure_batch_worker(batch_id)
                        self._send_json({"batch": batch}, HTTPStatus.ACCEPTED)
            except WorkflowError as exc:
                self._send_workflow_error(exc)
            return
        import_match = re.fullmatch(
            r"/api/projects/([a-z0-9-]{3,80})/poems/import", path
        )
        if import_match:
            try:
                body = self._read_json()
                records = body.get("records")
                if records is None and "content" in body:
                    try:
                        records = parse_import_document(
                            str(body.get("content") or ""),
                            str(body.get("format") or "json"),
                        )
                    except PoemImportContractError as exc:
                        raise WorkflowError(exc.code, str(exc)) from exc
                if bool(body.get("commit")):
                    result = get_sop_store().import_poems(
                        import_match.group(1),
                        records,
                        actor=body.get("actor"),
                    )
                    self._send_json(result, HTTPStatus.CREATED)
                else:
                    self._send_json(
                        get_sop_store().preview_poem_import(
                            import_match.group(1), records
                        )
                    )
            except WorkflowError as exc:
                self._send_workflow_error(exc)
            return
        source_update = re.fullmatch(
            r"/api/poems/([a-z0-9-]{3,80})/source", path
        )
        if source_update:
            try:
                body = self._read_json()
                source = get_sop_store().update_poem_source(
                    source_update.group(1),
                    body.get("source"),
                    actor=body.get("actor"),
                )
                self._send_json({"source": source})
            except WorkflowError as exc:
                self._send_workflow_error(exc)
            return
        content_revision = re.fullmatch(
            r"/api/poems/([a-z0-9-]{3,80})/content/revisions", path
        )
        if content_revision:
            try:
                body = self._read_json()
                result = get_sop_store().revise_poem_content(
                    content_revision.group(1),
                    body.get("content"),
                    actor=body.get("actor"),
                )
                self._send_json(result, HTTPStatus.CREATED)
            except WorkflowError as exc:
                self._send_workflow_error(exc)
            return
        if path == "/api/poems/content/bulk-approve":
            try:
                body = self._read_json()
                result = get_sop_store().bulk_approve_content(
                    body.get("poem_ids") or [],
                    actor=body.get("actor"),
                )
                self._send_json(result)
            except (WorkflowError, TypeError) as exc:
                if isinstance(exc, WorkflowError):
                    self._send_workflow_error(exc)
                else:
                    self._send_json(
                        {"code": "INVALID_PAYLOAD", "message": "请求字段格式无效。"},
                        HTTPStatus.BAD_REQUEST,
                    )
            return
        content_approval = re.fullmatch(
            r"/api/poems/([a-z0-9-]{3,80})/content/approve", path
        )
        if content_approval:
            try:
                body = self._read_json()
                result = get_sop_store().approve_content(
                    content_approval.group(1),
                    actor=body.get("actor"),
                )
                self._send_json({"poem": result})
            except WorkflowError as exc:
                self._send_workflow_error(exc)
            return
        if path == "/api/requirements/generate":
            try:
                body = self._read_json()
                result = get_sop_store().generate_requirements(
                    str(body.get("project_id") or SOP_DEFAULT_PROJECT_ID),
                    body.get("poem_ids") or [],
                    preserve_locked=bool(body.get("preserve_locked", True)),
                    actor=body.get("actor"),
                )
                self._send_json(result, HTTPStatus.CREATED)
            except (WorkflowError, TypeError) as exc:
                if isinstance(exc, WorkflowError):
                    self._send_workflow_error(exc)
                else:
                    self._send_json(
                        {"code": "INVALID_PAYLOAD", "message": "请求字段格式无效。"},
                        HTTPStatus.BAD_REQUEST,
                    )
            return
        if path == "/api/requirements/bulk-decision":
            try:
                body = self._read_json()
                result = get_sop_store().bulk_decide_requirements(
                    body.get("requirement_ids") or [],
                    str(body.get("decision") or ""),
                    reason=str(body.get("reason") or ""),
                    actor=body.get("actor"),
                )
                self._send_json(result)
            except (WorkflowError, TypeError) as exc:
                if isinstance(exc, WorkflowError):
                    self._send_workflow_error(exc)
                else:
                    self._send_json(
                        {"code": "INVALID_PAYLOAD", "message": "请求字段格式无效。"},
                        HTTPStatus.BAD_REQUEST,
                    )
            return
        requirement_action = re.fullmatch(
            r"/api/requirements/(req_[a-f0-9]{32})/(approve|reject)", path
        )
        if requirement_action:
            try:
                body = self._read_json()
                result = get_sop_store().decide_requirement(
                    requirement_action.group(1),
                    requirement_action.group(2),
                    reason=str(body.get("reason") or ""),
                    actor=body.get("actor"),
                )
                self._send_json({"requirement": result})
            except (WorkflowError, TypeError) as exc:
                if isinstance(exc, WorkflowError):
                    self._send_workflow_error(exc)
                else:
                    self._send_json(
                        {"code": "INVALID_PAYLOAD", "message": "请求字段格式无效。"},
                        HTTPStatus.BAD_REQUEST,
                    )
            return
        if path == "/api/directions/generate":
            try:
                body = self._read_json()
                result = get_sop_store().generate_directions(
                    str(body.get("project_id") or SOP_DEFAULT_PROJECT_ID),
                    body.get("poem_ids") or [],
                    preserve_locked=bool(body.get("preserve_locked", True)),
                    actor=body.get("actor"),
                )
                self._send_json(result, HTTPStatus.CREATED)
            except (WorkflowError, TypeError) as exc:
                if isinstance(exc, WorkflowError):
                    self._send_workflow_error(exc)
                else:
                    self._send_json(
                        {"code": "INVALID_PAYLOAD", "message": "请求字段格式无效。"},
                        HTTPStatus.BAD_REQUEST,
                    )
            return
        if path == "/api/directions/bulk-decision":
            try:
                body = self._read_json()
                result = get_sop_store().bulk_decide_directions(
                    body.get("direction_ids") or [],
                    str(body.get("decision") or ""),
                    reason=str(body.get("reason") or ""),
                    actor=body.get("actor"),
                )
                self._send_json(result)
            except (WorkflowError, TypeError) as exc:
                if isinstance(exc, WorkflowError):
                    self._send_workflow_error(exc)
                else:
                    self._send_json(
                        {"code": "INVALID_PAYLOAD", "message": "请求字段格式无效。"},
                        HTTPStatus.BAD_REQUEST,
                    )
            return
        direction_action = re.fullmatch(
            r"/api/directions/(dir_[a-f0-9]{32})/(approve|reject)", path
        )
        if direction_action:
            try:
                body = self._read_json()
                result = get_sop_store().decide_direction(
                    direction_action.group(1),
                    direction_action.group(2),
                    reason=str(body.get("reason") or ""),
                    actor=body.get("actor"),
                )
                self._send_json({"direction": result})
            except (WorkflowError, TypeError) as exc:
                if isinstance(exc, WorkflowError):
                    self._send_workflow_error(exc)
                else:
                    self._send_json(
                        {"code": "INVALID_PAYLOAD", "message": "请求字段格式无效。"},
                        HTTPStatus.BAD_REQUEST,
                    )
            return
        direction_mutation = re.fullmatch(
            r"/api/directions/(dir_[a-f0-9]{32})/(revise|copy|disable)", path
        )
        if direction_mutation:
            try:
                body = self._read_json()
                direction_id = direction_mutation.group(1)
                action = direction_mutation.group(2)
                if action == "revise":
                    result = get_sop_store().revise_direction(
                        direction_id,
                        body.get("content") or {},
                        actor=body.get("actor"),
                    )
                    status = HTTPStatus.CREATED
                elif action == "copy":
                    result = get_sop_store().copy_direction(
                        direction_id, actor=body.get("actor")
                    )
                    status = HTTPStatus.CREATED
                else:
                    result = get_sop_store().disable_direction(
                        direction_id,
                        reason=str(body.get("reason") or ""),
                        actor=body.get("actor"),
                    )
                    status = HTTPStatus.OK
                self._send_json({"direction": result}, status)
            except (WorkflowError, TypeError) as exc:
                if isinstance(exc, WorkflowError):
                    self._send_workflow_error(exc)
                else:
                    self._send_json(
                        {"code": "INVALID_PAYLOAD", "message": "请求字段格式无效。"},
                        HTTPStatus.BAD_REQUEST,
                    )
            return
        if path == "/api/projects":
            try:
                body = self._read_json()
                name = str(body.get("name", "")).strip()[:80]
                purpose = str(body.get("purpose", "诗画卡片")).strip()[:40]
                poem_ids = body.get("poem_ids") or [item["id"] for item in STORE.poems]
                poem_ids = [str(item) for item in poem_ids]
                style_id = str(body.get("style_id", STORE.styles[0]["id"]))
                aspect_ratio = str(body.get("aspect_ratio", "portrait"))
                if not name:
                    raise ValueError("请填写项目名称。")
                if not poem_ids or any(item not in STORE.poem_by_id for item in poem_ids):
                    raise ValueError("项目包含无效诗词。")
                if style_id not in STORE.style_by_id:
                    raise ValueError("请选择有效风格。")
                if aspect_ratio not in {"portrait", "square", "landscape"}:
                    raise ValueError("不支持的画面比例。")
            except (ValueError, TypeError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            project = {
                "id": uuid.uuid4().hex,
                "name": name,
                "purpose": purpose,
                "poem_ids": poem_ids,
                "style_id": style_id,
                "aspect_ratio": aspect_ratio,
                "status": "in_progress",
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
            STORE.add_project(project)
            self._send_json({"project": project}, HTTPStatus.CREATED)
            return
        if path != "/api/generate":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            body = self._read_json()
            poem_id = str(body.get("poem_id", ""))
            style_id = str(body.get("style_id", ""))
            count = int(body.get("count", 1))
            aspect_ratio = str(body.get("aspect_ratio", "portrait"))
            custom_note = str(body.get("custom_note", ""))[:500]
            project_id = str(body.get("project_id", STORE.projects[0]["id"]))
            generation_mode = str(body.get("generation_mode", "explore"))
            parent_image_id = body.get("parent_image_id")
            parent_image_id = str(parent_image_id) if parent_image_id else None
            preserve = body.get("preserve") or []
            preserve = [str(item)[:40] for item in preserve][:6]
            project = next(
                (item for item in STORE.projects if item["id"] == project_id), None
            )
            if not project:
                raise ValueError("请选择有效项目。")
            if poem_id not in STORE.poem_by_id:
                raise ValueError("请选择有效诗词。")
            if style_id not in STORE.style_by_id:
                raise ValueError("请选择有效风格。")
            if count < 1 or count > 4:
                raise ValueError("单次可生成 1–4 张。")
            if aspect_ratio not in {"portrait", "square", "landscape"}:
                raise ValueError("不支持的画面比例。")
            if generation_mode not in {"explore", "converge"}:
                raise ValueError("不支持的创作模式。")
            if generation_mode == "converge":
                parent = next(
                    (item for item in STORE.images if item["id"] == parent_image_id),
                    None,
                )
                if not parent or parent.get("project_id") != project_id:
                    raise ValueError("收敛迭代需要选择本项目中的参考候选图。")
        except (ValueError, TypeError) as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        job = {
            "id": uuid.uuid4().hex,
            "poem_id": poem_id,
            "poem_title": STORE.poem_by_id[poem_id]["title"],
            "style_id": style_id,
            "style_name": STORE.style_by_id[style_id]["name"],
            "provider": provider_name(),
            "count": count,
            "aspect_ratio": aspect_ratio,
            "custom_note": custom_note,
            "project_id": project_id,
            "project_name": project["name"],
            "generation_mode": generation_mode,
            "parent_image_id": parent_image_id,
            "preserve": preserve,
            "status": "queued",
            "progress": 0,
            "image_ids": [],
            "created_at": utc_now(),
        }
        STORE.add_job(job)
        threading.Thread(
            target=run_generation_job,
            args=(job["id"],),
            daemon=True,
            name=f"generation-{job['id'][:8]}",
        ).start()
        self._send_json({"job": job}, HTTPStatus.ACCEPTED)

    def do_PATCH(self) -> None:  # noqa: N802
        assert STORE is not None
        path = urlparse(self.path).path
        budget_match = re.fullmatch(
            r"/api/projects/([a-z0-9-]{3,80})/budget", path
        )
        if budget_match:
            try:
                body = self._read_json()
                if "hard_limit" not in body:
                    raise WorkflowError(
                        "BUDGET_REQUIRED", "请填写项目预算硬上限。"
                    )
                budget = get_sop_store().set_budget_policy(
                    budget_match.group(1),
                    hard_limit=float(body["hard_limit"]),
                    soft_ratio=float(body.get("soft_ratio", 0.7)),
                    actor=body.get("actor"),
                )
                self._send_json({"budget": budget})
            except (WorkflowError, TypeError, ValueError) as exc:
                if isinstance(exc, WorkflowError):
                    self._send_workflow_error(exc)
                else:
                    self._send_json(
                        {
                            "code": "INVALID_BUDGET",
                            "message": "预算与软提醒比例必须是有效数字。",
                            "request_id": self.headers.get(
                                "X-Request-ID", "local-request"
                            ),
                        },
                        HTTPStatus.BAD_REQUEST,
                    )
            return
        requirement_match = re.fullmatch(r"/api/requirements/(req_[a-f0-9]{32})", path)
        if requirement_match:
            try:
                body = self._read_json()
                changes = body.get("changes")
                if not isinstance(changes, dict):
                    raise WorkflowError("CHANGES_REQUIRED", "请提交需要修改的需求字段。")
                result = get_sop_store().revise_requirement(
                    requirement_match.group(1),
                    changes,
                    actor=body.get("actor"),
                )
                self._send_json({"requirement": result}, HTTPStatus.CREATED)
            except WorkflowError as exc:
                self._send_workflow_error(exc)
            return
        project_match = re.fullmatch(r"/api/projects/([a-z0-9-]{3,64})", path)
        if project_match:
            try:
                body = self._read_json()
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            updates = {}
            if "status" in body and body["status"] in {"in_progress", "completed", "archived"}:
                updates["status"] = body["status"]
            if "style_id" in body and body["style_id"] in STORE.style_by_id:
                updates["style_id"] = body["style_id"]
            project = STORE.update_project(project_match.group(1), **updates)
            if not project:
                self._send_json({"error": "项目不存在。"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"project": project})
            return
        match = re.fullmatch(r"/api/images/([a-f0-9]{32})", path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            body = self._read_json()
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        updates: dict[str, Any] = {}
        if "favorite" in body:
            updates["favorite"] = bool(body["favorite"])
        if "hidden" in body:
            updates["hidden"] = bool(body["hidden"])
        if "decision" in body:
            decision = str(body["decision"])
            if decision not in DECISION_VALUES:
                self._send_json({"error": "无效的评审结论。"}, HTTPStatus.BAD_REQUEST)
                return
            if decision == "final":
                current = next(
                    (item for item in STORE.images if item["id"] == match.group(1)),
                    None,
                )
                qc_candidate = body.get("qc") if isinstance(body.get("qc"), dict) else (current or {}).get("qc", {})
                if not all(bool(qc_candidate.get(key)) for key in QC_KEYS):
                    self._send_json(
                        {"error": "完成全部五项美术质检后才能标记为成品。"},
                        HTTPStatus.CONFLICT,
                    )
                    return
            updates["decision"] = decision
            updates["favorite"] = decision in {"selected", "final"}
        if "feedback_tags" in body:
            values = body["feedback_tags"] if isinstance(body["feedback_tags"], list) else []
            updates["feedback_tags"] = [str(item)[:40] for item in values][:8]
        if "review_note" in body:
            updates["review_note"] = str(body["review_note"])[:500]
        if "qc" in body:
            values = body["qc"] if isinstance(body["qc"], dict) else {}
            updates["qc"] = {
                key: bool(value) for key, value in values.items() if key in QC_KEYS
            }
        if not updates:
            self._send_json({"error": "没有可更新的字段。"}, HTTPStatus.BAD_REQUEST)
            return
        image = STORE.update_image(match.group(1), **updates)
        if not image:
            self._send_json({"error": "图片不存在。"}, HTTPStatus.NOT_FOUND)
            return
        self._send_json({"image": image})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="唐诗绘卷本地插图工作台")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    return parser.parse_args()


def main() -> None:
    global STORE, SOP_STORE
    args = parse_args()
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    STORE = StudioStore()
    SOP_STORE = get_sop_store()
    seed_demo_gallery()
    server = ThreadingHTTPServer((args.host, args.port), StudioHandler)
    display_host = "localhost" if args.host in {"127.0.0.1", "0.0.0.0"} else args.host
    print("唐诗绘卷已启动")
    print(f"访问地址: http://{display_host}:{server.server_port}")
    print(f"生成模式: {provider_name()}")
    if args.host == "0.0.0.0":
        print("局域网访问已开启，请使用本机局域网 IP 和相同端口。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
