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
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = Path(os.getenv("TANG_STUDIO_PUBLIC_DIR", ROOT / "public")).resolve()
DATA_DIR = Path(os.getenv("TANG_STUDIO_DATA_DIR", ROOT / "data")).resolve()
GENERATED_DIR = DATA_DIR / "generated"
STATE_FILE = DATA_DIR / "state.json"
POEMS_FILE = DATA_DIR / "poems.json"
STYLES_FILE = DATA_DIR / "styles.json"
APP_VERSION = "0.2.0"

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
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temp, path)


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
        project_ids = {item["id"] for item in self.projects}
        fallback_project_id = self.projects[0]["id"]
        for image in self.images:
            if image.get("project_id") not in project_ids:
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
            if job.get("project_id") not in project_ids:
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


def provider_name() -> str:
    requested = os.getenv("AI_PROVIDER", "auto").strip().lower()
    if requested == "auto":
        return "openai" if os.getenv("OPENAI_API_KEY") else "demo"
    return requested


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
) -> None:
    """Render a stylized local fallback, deliberately labelled as demo artwork."""
    width, height = 1024, 1280
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
  <rect width="1024" height="1280" fill="url(#wash-{seed})"/>
  {''.join(scene)}
  {frame}
  <rect width="1024" height="1280" filter="url(#{texture_id})" opacity=".72"/>
</svg>'''
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(svg, encoding="utf-8")


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
        output.write_bytes(base64.b64decode(item["b64_json"]))
        return
    if item.get("url"):
        with urllib.request.urlopen(item["url"], timeout=120) as response:
            output.write_bytes(response.read())
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
    output.write_bytes(base64.b64decode(data[0]["b64_json"]))


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
    prompt = build_prompt(poem, style, custom_note, workflow)
    if provider == "demo":
        render_demo_svg(output, poem, style, index)
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

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

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
        path = urlparse(self.path).path
        if path == "/api/health":
            self._send_json(
                {
                    "ok": True,
                    "version": APP_VERSION,
                    "provider": provider_name(),
                    "live_generation": provider_name() == "openai",
                    "time": utc_now(),
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
            }
            self._send_json(data)
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
    global STORE
    args = parse_args()
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    STORE = StudioStore()
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
