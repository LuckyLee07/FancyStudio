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
APP_VERSION = "0.1.0"


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
        state = read_json(STATE_FILE, {"jobs": [], "images": []})
        self.jobs = state.get("jobs", [])
        self.images = state.get("images", [])
        for job in self.jobs:
            if job.get("status") in {"queued", "running"}:
                job["status"] = "failed"
                job["error"] = "本地服务重启，未完成任务已停止，请重新生成。"
                job["finished_at"] = utc_now()
        self.persist()

    def persist(self) -> None:
        with self.lock:
            atomic_write_json(STATE_FILE, {"jobs": self.jobs, "images": self.images})

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "poems": self.poems,
                "styles": self.styles,
                "images": list(reversed(self.images[-200:])),
                "jobs": list(reversed(self.jobs[-30:])),
            }

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


def build_prompt(poem: dict[str, Any], style: dict[str, Any], custom_note: str) -> str:
    lines = "，".join(poem["lines"])
    constraints = "、".join(poem.get("avoid", []))
    note = custom_note.strip()
    note_block = f"\nAdditional art direction: {note}" if note else ""
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


def create_image_record(
    job_id: str,
    poem: dict[str, Any],
    style: dict[str, Any],
    index: int,
    provider: str,
    aspect_ratio: str,
    custom_note: str,
) -> dict[str, Any]:
    image_id = uuid.uuid4().hex
    extension = "svg" if provider == "demo" else "png"
    filename = f"{image_id}.{extension}"
    output = GENERATED_DIR / filename
    prompt = build_prompt(poem, style, custom_note)
    if provider == "demo":
        render_demo_svg(output, poem, style, index)
    elif provider == "openai":
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
        "provider": provider,
        "url": f"/generated/{filename}",
        "aspect_ratio": aspect_ratio,
        "prompt": prompt,
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
    STORE.update_job(job_id, status="running", started_at=utc_now(), progress=0)
    image_ids: list[str] = []
    try:
        for index in range(count):
            if provider == "demo":
                time.sleep(0.35)
            record = create_image_record(
                job_id, poem, style, index + int(time.time()), provider, ratio, note
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
                "provider": "sample",
                "url": f"/samples/{filename}",
                "aspect_ratio": "portrait",
                "prompt": build_prompt(poem, style, ""),
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
            if poem_id not in STORE.poem_by_id:
                raise ValueError("请选择有效诗词。")
            if style_id not in STORE.style_by_id:
                raise ValueError("请选择有效风格。")
            if count < 1 or count > 4:
                raise ValueError("单次可生成 1–4 张。")
            if aspect_ratio not in {"portrait", "square", "landscape"}:
                raise ValueError("不支持的画面比例。")
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
