"""Poem import contract, template helpers and CSV/JSON parsing."""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date
from pathlib import Path
from typing import Any


POEM_IMPORT_SCHEMA_VERSION = "poem-import/v1"
SOURCE_TYPES = {
    "public_domain",
    "self_curated",
    "licensed",
    "academic_reference",
    "unknown",
}
SOURCE_VERIFICATION_STATUSES = {"unverified", "verified", "restricted"}
CSV_FIELDS = (
    "id",
    "title",
    "author",
    "dynasty",
    "lines",
    "theme",
    "mood",
    "imagery",
    "notes",
    "source_type",
    "source_citation",
    "source_license",
    "source_verification_status",
    "source_url",
    "source_verified_at",
)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PoemImportContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def schema_document() -> dict[str, Any]:
    path = Path(__file__).resolve().parent / "schemas" / "poem-import.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def json_template_document() -> dict[str, Any]:
    path = Path(__file__).resolve().parent / "templates" / "poem-import-template.json"
    return json.loads(path.read_text(encoding="utf-8"))


def csv_template_text() -> str:
    path = Path(__file__).resolve().parent / "templates" / "poem-import-template.csv"
    return path.read_text(encoding="utf-8")


def _split_pipe(value: Any) -> list[str]:
    return [item.strip() for item in str(value or "").split("|") if item.strip()]


def _csv_records(text: str) -> list[dict[str, Any]]:
    try:
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    except csv.Error as exc:
        raise PoemImportContractError("IMPORT_CSV_INVALID", "CSV 无法解析。") from exc
    if not reader.fieldnames:
        raise PoemImportContractError("IMPORT_CSV_INVALID", "CSV 缺少表头。")
    fieldnames = [str(item or "").strip() for item in reader.fieldnames]
    missing = [field for field in ("id", "title", "author", "lines") if field not in fieldnames]
    if missing:
        raise PoemImportContractError(
            "IMPORT_CSV_COLUMNS_MISSING",
            "CSV 缺少必填列：" + "、".join(missing),
        )
    unknown = [field for field in fieldnames if field and field not in CSV_FIELDS]
    if unknown:
        raise PoemImportContractError(
            "IMPORT_CSV_COLUMNS_UNKNOWN",
            "CSV 包含未定义列：" + "、".join(unknown),
        )
    records: list[dict[str, Any]] = []
    try:
        for row in reader:
            if not any(str(value or "").strip() for value in row.values()):
                continue
            records.append(
                {
                    "id": row.get("id", ""),
                    "title": row.get("title", ""),
                    "author": row.get("author", ""),
                    "dynasty": row.get("dynasty", "") or "唐",
                    "lines": _split_pipe(row.get("lines")),
                    "theme": row.get("theme", ""),
                    "mood": row.get("mood", ""),
                    "imagery": _split_pipe(row.get("imagery")),
                    "notes": row.get("notes", ""),
                    "source": {
                        "source_type": row.get("source_type", "") or "unknown",
                        "citation": row.get("source_citation", ""),
                        "license": row.get("source_license", ""),
                        "verification_status": row.get(
                            "source_verification_status", ""
                        )
                        or "unverified",
                        "url": row.get("source_url", ""),
                        "verified_at": row.get("source_verified_at", ""),
                    },
                }
            )
    except csv.Error as exc:
        raise PoemImportContractError("IMPORT_CSV_INVALID", "CSV 行数据无法解析。") from exc
    return records


def parse_import_document(content: str, format_hint: str) -> list[dict[str, Any]]:
    content = str(content or "")
    if not content.strip():
        raise PoemImportContractError("EMPTY_IMPORT", "导入内容为空。")
    if len(content.encode("utf-8")) > 2_000_000:
        raise PoemImportContractError("IMPORT_TOO_LARGE", "导入内容不能超过 2 MB。")
    format_name = str(format_hint or "json").strip().lower()
    if format_name == "csv":
        records = _csv_records(content)
    elif format_name == "json":
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise PoemImportContractError(
                "IMPORT_JSON_INVALID",
                f"JSON 格式错误：第 {exc.lineno} 行第 {exc.colno} 列。",
            ) from exc
        if isinstance(payload, list):
            records = payload
        elif isinstance(payload, dict):
            if payload.get("schema_version") != POEM_IMPORT_SCHEMA_VERSION:
                raise PoemImportContractError(
                    "IMPORT_SCHEMA_VERSION_UNSUPPORTED",
                    f"schema_version 必须是 {POEM_IMPORT_SCHEMA_VERSION}。",
                )
            records = payload.get("records")
        else:
            records = None
        if not isinstance(records, list):
            raise PoemImportContractError(
                "IMPORT_RECORDS_REQUIRED",
                "JSON 必须是记录数组，或包含 schema_version 和 records 的对象。",
            )
    else:
        raise PoemImportContractError(
            "IMPORT_FORMAT_UNSUPPORTED", "导入格式只支持 json 或 csv。"
        )
    if not records:
        raise PoemImportContractError("EMPTY_IMPORT", "导入文件中没有诗词记录。")
    if len(records) > 500:
        raise PoemImportContractError("IMPORT_TOO_LARGE", "单次最多导入 500 首诗词。")
    return records


def normalize_source(value: Any) -> tuple[dict[str, str], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if isinstance(value, str):
        citation = value.strip()[:500]
        source = {
            "source_type": "unknown",
            "citation": citation,
            "license": "needs-review",
            "verification_status": "unverified",
            "url": "",
            "verified_at": "",
        }
        if citation:
            warnings.append("来源为旧字符串格式，需补充来源类型、许可和复核状态。")
        else:
            warnings.append("缺少来源，导入后必须补录并完成内容复核。")
        return source, errors, warnings
    if not isinstance(value, dict):
        return {
            "source_type": "unknown",
            "citation": "",
            "license": "",
            "verification_status": "unverified",
            "url": "",
            "verified_at": "",
        }, errors, ["缺少来源，导入后必须补录并完成内容复核。"]
    source_type = str(value.get("source_type") or "unknown").strip()
    citation = str(value.get("citation") or "").strip()[:500]
    license_name = str(value.get("license") or "").strip()[:120]
    status = str(value.get("verification_status") or "unverified").strip()
    url = str(value.get("url") or "").strip()[:500]
    verified_at = str(value.get("verified_at") or "").strip()[:30]
    if source_type not in SOURCE_TYPES:
        errors.append("source.source_type 无效。")
    if status not in SOURCE_VERIFICATION_STATUSES:
        errors.append("source.verification_status 无效。")
    if not citation:
        if status == "verified":
            errors.append("已核验来源必须填写来源引文。")
        else:
            warnings.append("缺少来源引文，导入后不能通过内容校验。")
    if not license_name:
        if status == "verified":
            errors.append("已核验来源必须填写可用许可。")
        else:
            warnings.append("缺少来源许可，导入后不能通过内容校验。")
    if status == "verified":
        if not verified_at:
            errors.append("已核验来源必须填写 verified_at。")
        elif not _ISO_DATE.fullmatch(verified_at):
            errors.append("source.verified_at 必须是 YYYY-MM-DD。")
        else:
            try:
                verified_date = date.fromisoformat(verified_at)
            except ValueError:
                errors.append("source.verified_at 不是有效日期。")
            else:
                if verified_date > date.today():
                    errors.append("source.verified_at 不能晚于今天。")
        if source_type == "unknown" or license_name.lower() in {
            "unknown",
            "needs-review",
            "restricted",
        }:
            errors.append("已核验来源必须明确来源类型和可用许可。")
    if status == "restricted":
        warnings.append("来源许可受限，不能进入交付链路。")
    return {
        "source_type": source_type,
        "citation": citation,
        "license": license_name,
        "verification_status": status,
        "url": url,
        "verified_at": verified_at,
    }, errors, warnings
