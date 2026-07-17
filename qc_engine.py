"""Deterministic, offline image quality checks for the production pipeline.

The first QC version intentionally covers checks that can be proven locally:
file integrity, dimensions, aspect ratio, embedded SVG text and perceptual
similarity for supported PNG/SVG assets. Semantic, historical and aesthetic
judgements remain visible soft risks for human review instead of being reported
as a fake automatic pass.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
import struct
import zlib
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


QC_VERSION = "local-rules-v2"
EXPECTED_RATIOS = {"portrait": 2 / 3, "square": 1.0, "landscape": 3 / 2}


class ImageInspectionError(RuntimeError):
    """Raised when a file cannot be inspected safely."""


def hamming_distance(first: str, second: str) -> int:
    if not first or not second or len(first) != len(second):
        return 10_000
    try:
        return (int(first, 16) ^ int(second, 16)).bit_count()
    except ValueError:
        return 10_000


def _numeric_dimension(value: str | None) -> int:
    if not value:
        return 0
    match = re.match(r"\s*([0-9]+(?:\.[0-9]+)?)", value)
    return round(float(match.group(1))) if match else 0


def _simhash(tokens: list[str]) -> str:
    if not tokens:
        return ""
    vector = [0] * 64
    for token in tokens:
        digest = int.from_bytes(
            hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big"
        )
        for bit in range(64):
            vector[bit] += 1 if digest & (1 << bit) else -1
    result = 0
    for bit, weight in enumerate(vector):
        if weight >= 0:
            result |= 1 << bit
    return f"{result:016x}"


def _inspect_svg(path: Path) -> dict[str, Any]:
    try:
        root = ElementTree.parse(path).getroot()
    except (ElementTree.ParseError, OSError) as exc:
        raise ImageInspectionError("SVG 文件无法解析。") from exc
    width = _numeric_dimension(root.attrib.get("width"))
    height = _numeric_dimension(root.attrib.get("height"))
    if (not width or not height) and root.attrib.get("viewBox"):
        values = re.split(r"[\s,]+", root.attrib["viewBox"].strip())
        if len(values) == 4:
            try:
                width, height = round(float(values[2])), round(float(values[3]))
            except ValueError:
                width, height = 0, 0
    tags = [node.tag.rsplit("}", 1)[-1].lower() for node in root.iter()]
    embedded_text = any(tag in {"text", "foreignobject"} for tag in tags)
    tokens: list[str] = tags[:300]
    for node in root.iter():
        for key, value in sorted(node.attrib.items()):
            if key.lower() in {"id", "class"}:
                continue
            normalized = re.sub(
                r"-?\d+(?:\.\d+)?",
                lambda match: str(round(float(match.group(0)) / 16)),
                value.lower(),
            )
            tokens.append(f"{key.lower()}={normalized[:100]}")
    return {
        "width": width,
        "height": height,
        "decodable": True,
        "embedded_text": embedded_text,
        "perceptual_hash": _simhash(tokens),
        "decoder": "xml-svg",
    }


def _paeth(left: int, up: int, upper_left: int) -> int:
    estimate = left + up - upper_left
    left_distance = abs(estimate - left)
    up_distance = abs(estimate - up)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= up_distance and left_distance <= upper_left_distance:
        return left
    if up_distance <= upper_left_distance:
        return up
    return upper_left


def _inspect_png(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ImageInspectionError("PNG 文件签名无效。")
    offset = 8
    width = height = bit_depth = color_type = interlace = 0
    compressed = bytearray()
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise ImageInspectionError("PNG 数据块不完整。")
        payload = data[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length : end])[0]
        if zlib.crc32(chunk_type + payload) & 0xFFFFFFFF != expected_crc:
            raise ImageInspectionError("PNG 数据块校验失败。")
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
        elif chunk_type == b"IDAT":
            compressed.extend(payload)
        elif chunk_type == b"IEND":
            break
        offset = end
    if not width or not height or not compressed:
        raise ImageInspectionError("PNG 缺少尺寸或图像数据。")
    perceptual_hash = ""
    decoder = "png-header"
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color_type)
    if bit_depth == 8 and channels and interlace == 0:
        try:
            raw = zlib.decompress(bytes(compressed))
            row_bytes = width * channels
            expected = height * (row_bytes + 1)
            if len(raw) != expected:
                raise ValueError("unexpected decompressed length")
            rows: list[bytearray] = []
            cursor = 0
            previous = bytearray(row_bytes)
            for _ in range(height):
                filter_type = raw[cursor]
                cursor += 1
                scanline = bytearray(raw[cursor : cursor + row_bytes])
                cursor += row_bytes
                reconstructed = bytearray(row_bytes)
                for index, value in enumerate(scanline):
                    left = reconstructed[index - channels] if index >= channels else 0
                    up = previous[index]
                    upper_left = previous[index - channels] if index >= channels else 0
                    if filter_type == 0:
                        predictor = 0
                    elif filter_type == 1:
                        predictor = left
                    elif filter_type == 2:
                        predictor = up
                    elif filter_type == 3:
                        predictor = (left + up) // 2
                    elif filter_type == 4:
                        predictor = _paeth(left, up, upper_left)
                    else:
                        raise ValueError("unsupported PNG filter")
                    reconstructed[index] = (value + predictor) & 0xFF
                rows.append(reconstructed)
                previous = reconstructed
            luminance: list[int] = []
            for grid_y in range(8):
                y = min(height - 1, round((grid_y + 0.5) * height / 8 - 0.5))
                for grid_x in range(8):
                    x = min(width - 1, round((grid_x + 0.5) * width / 8 - 0.5))
                    base = x * channels
                    if color_type in {0, 4}:
                        lum = rows[y][base]
                    else:
                        red, green, blue = rows[y][base : base + 3]
                        lum = round(0.299 * red + 0.587 * green + 0.114 * blue)
                    luminance.append(lum)
            average = sum(luminance) / len(luminance)
            bits = 0
            for index, value in enumerate(luminance):
                if value >= average:
                    bits |= 1 << index
            perceptual_hash = f"{bits:016x}"
            decoder = "png-raster-v1"
        except (ValueError, zlib.error):
            # Header integrity is still proven; unsupported raster layouts are
            # routed with an explicit warning by the caller.
            pass
    return {
        "width": width,
        "height": height,
        "decodable": True,
        "embedded_text": False,
        "perceptual_hash": perceptual_hash,
        "decoder": decoder,
    }


def _inspect_jpeg(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    if not data.startswith(b"\xff\xd8"):
        raise ImageInspectionError("JPEG 文件签名无效。")
    offset = 2
    while offset + 9 < len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9}:
            continue
        if offset + 2 > len(data):
            break
        length = struct.unpack(">H", data[offset : offset + 2])[0]
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if offset + 7 > len(data):
                break
            height, width = struct.unpack(">HH", data[offset + 3 : offset + 7])
            return {
                "width": width,
                "height": height,
                "decodable": True,
                "embedded_text": False,
                "perceptual_hash": "",
                "decoder": "jpeg-header",
            }
        offset += max(length, 2)
    raise ImageInspectionError("JPEG 缺少有效尺寸信息。")


def inspect_image(path: Path, expected_aspect_ratio: str) -> dict[str, Any]:
    path = Path(path)
    hard_failures: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {
        "file_exists": path.is_file(),
        "file_nonempty": path.is_file() and path.stat().st_size > 128,
    }
    if not checks["file_exists"]:
        hard_failures.append("file_missing")
        return _result(path, {}, checks, hard_failures, warnings)
    if not checks["file_nonempty"]:
        hard_failures.append("file_too_small_or_empty")
    data = path.read_bytes()
    checksum = hashlib.sha256(data).hexdigest()
    extension = path.suffix.lower()
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    details: dict[str, Any] = {
        "checksum": checksum,
        "mime_type": mime_type,
        "file_size": len(data),
    }
    try:
        if extension == ".svg":
            decoded = _inspect_svg(path)
        elif extension == ".png":
            decoded = _inspect_png(path)
        elif extension in {".jpg", ".jpeg"}:
            decoded = _inspect_jpeg(path)
        else:
            raise ImageInspectionError("不支持的图片格式。")
        details.update(decoded)
        checks["format_supported"] = True
        checks["decodable"] = bool(decoded["decodable"])
    except (ImageInspectionError, OSError) as exc:
        checks["format_supported"] = extension in {".svg", ".png", ".jpg", ".jpeg"}
        checks["decodable"] = False
        hard_failures.append("image_not_decodable")
        warnings.append(str(exc))
    width = int(details.get("width") or 0)
    height = int(details.get("height") or 0)
    checks["dimensions_valid"] = width > 0 and height > 0
    if not checks["dimensions_valid"]:
        hard_failures.append("dimensions_missing")
    expected = EXPECTED_RATIOS.get(expected_aspect_ratio)
    actual = width / height if width and height else 0
    checks["expected_aspect_ratio"] = expected_aspect_ratio
    checks["actual_aspect_ratio"] = round(actual, 4) if actual else None
    checks["aspect_ratio_match"] = bool(expected and actual and abs(actual - expected) <= 0.04)
    if expected and actual and not checks["aspect_ratio_match"]:
        hard_failures.append("aspect_ratio_mismatch")
    checks["minimum_resolution"] = width >= 768 and height >= 768
    if width and height and not checks["minimum_resolution"]:
        warnings.append("resolution_below_production_baseline")
    checks["no_embedded_text"] = not bool(details.get("embedded_text"))
    if not checks["no_embedded_text"]:
        hard_failures.append("embedded_text_or_watermark")
    checks["perceptual_hash_available"] = bool(details.get("perceptual_hash"))
    if extension != ".svg":
        warnings.append("raster_text_and_brand_detection_requires_human_review")
    if not checks["perceptual_hash_available"]:
        warnings.append("perceptual_hash_unavailable_for_this_encoding")
    warnings.append("semantic_historical_and_aesthetic_checks_require_human_review")
    return _result(path, details, checks, hard_failures, warnings)


def _result(
    path: Path,
    details: dict[str, Any],
    checks: dict[str, Any],
    hard_failures: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    unique_failures = list(dict.fromkeys(hard_failures))
    unique_warnings = list(dict.fromkeys(warnings))
    # Local checks cannot prove poem relevance, historical plausibility or
    # aesthetic quality. A technically valid file therefore still requires a
    # visual reviewer (or an explicit human override) before candidate routing.
    status = "hard_fail" if unique_failures else "manual_required"
    score = max(0, 100 - len(unique_failures) * 35 - len(unique_warnings) * 5)
    return {
        "version": QC_VERSION,
        "status": status,
        "score": score,
        "hard_failures": unique_failures,
        "warnings": unique_warnings,
        "checks": checks,
        "file_path": str(path),
        "checksum": details.get("checksum", ""),
        "mime_type": details.get("mime_type", "application/octet-stream"),
        "file_size": int(details.get("file_size") or 0),
        "width": int(details.get("width") or 0),
        "height": int(details.get("height") or 0),
        "perceptual_hash": details.get("perceptual_hash", ""),
        "decoder": details.get("decoder", "none"),
        "coverage": ["L0_technical", "L1_local_rules", "L5_similarity"],
    }
