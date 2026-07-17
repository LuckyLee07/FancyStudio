"""SQLite-backed production workflow for the Tang illustration SOP.

The legacy gallery remains available while this module becomes the source of
truth for production stages, approvals, versions, and audit history.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from qc_engine import EXPECTED_RATIOS, hamming_distance
from prompt_compiler import PromptCompileError, compile_generation_prompt
from requirement_schema import (
    GENERATOR_VERSION as REQUIREMENT_GENERATOR_VERSION,
    SCHEMA_VERSION as REQUIREMENT_SCHEMA_VERSION,
    validate_requirement_card,
    validate_with_single_repair,
)


DEFAULT_PROJECT_ID = "tang-300-production"

POEM_STATUSES = {
    "imported",
    "content_review",
    "requirement_draft",
    "requirement_review",
    "direction_draft",
    "direction_review",
    "ready_for_production",
    "generating",
    "candidate_review",
    "rework",
    "final_review",
    "approved",
    "exported",
    "blocked",
    "paused",
    "archived",
}

REQUIREMENT_STATUSES = {"draft", "in_review", "approved", "rejected"}
DIRECTION_STATUSES = {"draft", "in_review", "approved", "rejected", "disabled"}
DIRECTION_TYPES = ("narrative", "atmospheric", "symbolic")
INSTRUCTION_STATUSES = {"draft", "published", "retired"}
STYLE_PACK_STATUSES = {"draft", "published", "retired"}

BATCH_STATUSES = {
    "draft",
    "queued",
    "running",
    "paused",
    "completed",
    "partially_failed",
    "cancelled",
    "budget_blocked",
}

TASK_STATUSES = {
    "pending",
    "ready",
    "running",
    "succeeded",
    "failed",
    "retry_waiting",
    "cancelled",
    "blocked",
}

PRODUCTION_IMAGE_STATUSES = {
    "pending_qc",
    "review_ready",
    "qc_blocked",
    "needs_manual_qc",
    "selected",
    "rejected",
    "final_candidate",
    "finalized",
}

REVIEW_DECISIONS = {"candidate", "selected", "rejected", "final_candidate"}

REQUIREMENT_FIELDS = {
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
}

DIRECTION_FIELDS = {
    "title",
    "type",
    "subject",
    "shot",
    "foreground",
    "midground",
    "background",
    "action",
    "lighting",
    "palette",
    "whitespace",
    "preserve",
    "avoid",
    "risk_note",
    "art_director_note",
    "locked_fields",
}

STAGE_DEFINITIONS = (
    ("content", "内容校验", ("imported", "content_review")),
    ("requirements", "需求策划", ("requirement_draft", "requirement_review")),
    ("directions", "美术定向", ("direction_draft", "direction_review")),
    ("ready", "待排产", ("ready_for_production",)),
    ("generating", "生成中", ("generating",)),
    ("review", "审片返工", ("candidate_review", "rework", "final_review")),
    ("approved", "终审通过", ("approved",)),
    ("exported", "已交付", ("exported",)),
    ("blocked", "阻塞", ("blocked", "paused")),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class WorkflowError(ValueError):
    """A user-correctable workflow or validation failure."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class SopStore:
    """Thread-safe SQLite repository for the production workflow."""

    def __init__(
        self,
        database_path: Path,
        poem_seed_path: Path,
        style_seed_path: Path,
    ) -> None:
        self.database_path = database_path.resolve()
        self.poem_seed_path = poem_seed_path.resolve()
        self.style_seed_path = style_seed_path.resolve()
        self.lock = threading.RLock()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()
        self._seed()
        self._recover_interrupted_work()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=10,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _migrate(self) -> None:
        with self.lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                """
            )
            applied = {
                row["version"]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            if 1 not in applied:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;

                    CREATE TABLE production_projects (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        purpose TEXT NOT NULL,
                        audience TEXT NOT NULL DEFAULT '',
                        aspect_ratio TEXT NOT NULL DEFAULT 'portrait',
                        style_id TEXT NOT NULL DEFAULT '',
                        deadline TEXT,
                        status TEXT NOT NULL DEFAULT 'in_progress',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE poems (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL REFERENCES production_projects(id),
                        title TEXT NOT NULL,
                        author TEXT NOT NULL,
                        dynasty TEXT NOT NULL,
                        lines_json TEXT NOT NULL,
                        theme TEXT NOT NULL DEFAULT '',
                        mood TEXT NOT NULL DEFAULT '',
                        imagery_json TEXT NOT NULL DEFAULT '[]',
                        source TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL,
                        blocked_reason TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX idx_poems_project_status
                    ON poems(project_id, status);
                    CREATE INDEX idx_poems_author ON poems(author);

                    CREATE TABLE content_versions (
                        id TEXT PRIMARY KEY,
                        poem_id TEXT NOT NULL REFERENCES poems(id),
                        version INTEGER NOT NULL,
                        lines_json TEXT NOT NULL,
                        notes TEXT NOT NULL DEFAULT '',
                        source TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL,
                        approved_by TEXT,
                        created_at TEXT NOT NULL,
                        UNIQUE(poem_id, version)
                    );

                    CREATE TABLE instruction_versions (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL REFERENCES production_projects(id),
                        version INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        content_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        published_at TEXT,
                        created_by TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(project_id, version)
                    );

                    CREATE TABLE requirements (
                        id TEXT PRIMARY KEY,
                        poem_id TEXT NOT NULL REFERENCES poems(id),
                        instruction_id TEXT REFERENCES instruction_versions(id),
                        version INTEGER NOT NULL,
                        is_current INTEGER NOT NULL DEFAULT 1,
                        content_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        rejection_reason TEXT NOT NULL DEFAULT '',
                        created_by TEXT NOT NULL,
                        approved_by TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(poem_id, version)
                    );

                    CREATE INDEX idx_requirements_poem_current
                    ON requirements(poem_id, is_current);

                    CREATE TABLE directions (
                        id TEXT PRIMARY KEY,
                        poem_id TEXT NOT NULL REFERENCES poems(id),
                        requirement_id TEXT NOT NULL REFERENCES requirements(id),
                        version INTEGER NOT NULL,
                        type TEXT NOT NULL,
                        is_current INTEGER NOT NULL DEFAULT 1,
                        content_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        rejection_reason TEXT NOT NULL DEFAULT '',
                        created_by TEXT NOT NULL,
                        approved_by TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(poem_id, type, version)
                    );

                    CREATE INDEX idx_directions_poem_current
                    ON directions(poem_id, is_current);

                    CREATE TABLE audit_events (
                        id TEXT PRIMARY KEY,
                        actor_id TEXT NOT NULL,
                        actor_role TEXT NOT NULL,
                        action TEXT NOT NULL,
                        target_type TEXT NOT NULL,
                        target_id TEXT NOT NULL,
                        before_json TEXT,
                        after_json TEXT,
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX idx_audit_target
                    ON audit_events(target_type, target_id, created_at);

                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (1, CURRENT_TIMESTAMP);

                    COMMIT;
                    """
                )
            if 2 not in applied:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;

                    CREATE TABLE generation_batches (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL REFERENCES production_projects(id),
                        name TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        model TEXT NOT NULL,
                        style_id TEXT NOT NULL,
                        aspect_ratio TEXT NOT NULL,
                        count_per_direction INTEGER NOT NULL,
                        priority INTEGER NOT NULL DEFAULT 50,
                        status TEXT NOT NULL,
                        task_count INTEGER NOT NULL,
                        estimated_cost REAL NOT NULL DEFAULT 0,
                        actual_cost REAL NOT NULL DEFAULT 0,
                        currency TEXT NOT NULL DEFAULT 'USD',
                        budget_snapshot_json TEXT NOT NULL DEFAULT '{}',
                        settings_json TEXT NOT NULL DEFAULT '{}',
                        created_by TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT
                    );

                    CREATE INDEX idx_batches_project_status
                    ON generation_batches(project_id, status, created_at);

                    CREATE TABLE generation_tasks (
                        id TEXT PRIMARY KEY,
                        batch_id TEXT NOT NULL REFERENCES generation_batches(id),
                        poem_id TEXT NOT NULL REFERENCES poems(id),
                        direction_id TEXT NOT NULL REFERENCES directions(id),
                        sample_index INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        priority INTEGER NOT NULL DEFAULT 50,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        prompt_json TEXT NOT NULL,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL DEFAULT 3,
                        output_image_id TEXT,
                        last_error_code TEXT NOT NULL DEFAULT '',
                        last_error_message TEXT NOT NULL DEFAULT '',
                        retry_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT
                    );

                    CREATE INDEX idx_tasks_batch_status
                    ON generation_tasks(batch_id, status, priority, created_at);
                    CREATE INDEX idx_tasks_retry
                    ON generation_tasks(status, retry_at);
                    CREATE INDEX idx_tasks_poem
                    ON generation_tasks(poem_id, status);

                    CREATE TABLE generation_attempts (
                        id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL REFERENCES generation_tasks(id),
                        attempt_number INTEGER NOT NULL,
                        provider TEXT NOT NULL,
                        model TEXT NOT NULL,
                        status TEXT NOT NULL,
                        request_json TEXT NOT NULL DEFAULT '{}',
                        response_json TEXT,
                        error_code TEXT NOT NULL DEFAULT '',
                        error_message TEXT NOT NULL DEFAULT '',
                        duration_ms INTEGER,
                        estimated_cost REAL NOT NULL DEFAULT 0,
                        actual_cost REAL NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        finished_at TEXT,
                        UNIQUE(task_id, attempt_number)
                    );

                    CREATE INDEX idx_attempts_task
                    ON generation_attempts(task_id, attempt_number);

                    CREATE TABLE usage_records (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL REFERENCES production_projects(id),
                        batch_id TEXT NOT NULL REFERENCES generation_batches(id),
                        task_id TEXT NOT NULL REFERENCES generation_tasks(id),
                        attempt_id TEXT NOT NULL REFERENCES generation_attempts(id),
                        provider TEXT NOT NULL,
                        model TEXT NOT NULL,
                        units REAL NOT NULL DEFAULT 1,
                        estimated_cost REAL NOT NULL DEFAULT 0,
                        actual_cost REAL NOT NULL DEFAULT 0,
                        currency TEXT NOT NULL DEFAULT 'USD',
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX idx_usage_project_created
                    ON usage_records(project_id, created_at);

                    CREATE TABLE budget_policies (
                        project_id TEXT PRIMARY KEY REFERENCES production_projects(id),
                        currency TEXT NOT NULL DEFAULT 'USD',
                        hard_limit REAL NOT NULL DEFAULT 100,
                        soft_ratio REAL NOT NULL DEFAULT 0.7,
                        spent REAL NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL
                    );

                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (2, CURRENT_TIMESTAMP);

                    COMMIT;
                    """
                )
            if 3 not in applied:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;

                    CREATE TABLE production_images (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL REFERENCES production_projects(id),
                        batch_id TEXT NOT NULL REFERENCES generation_batches(id),
                        task_id TEXT NOT NULL UNIQUE REFERENCES generation_tasks(id),
                        poem_id TEXT NOT NULL REFERENCES poems(id),
                        direction_id TEXT NOT NULL REFERENCES directions(id),
                        style_id TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        model TEXT NOT NULL,
                        url TEXT NOT NULL,
                        file_path TEXT NOT NULL,
                        mime_type TEXT NOT NULL DEFAULT '',
                        checksum TEXT NOT NULL DEFAULT '',
                        perceptual_hash TEXT NOT NULL DEFAULT '',
                        file_size INTEGER NOT NULL DEFAULT 0,
                        width INTEGER NOT NULL DEFAULT 0,
                        height INTEGER NOT NULL DEFAULT 0,
                        aspect_ratio TEXT NOT NULL,
                        prompt TEXT NOT NULL DEFAULT '',
                        generation INTEGER NOT NULL DEFAULT 1,
                        parent_image_id TEXT,
                        status TEXT NOT NULL DEFAULT 'pending_qc',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX idx_production_images_review
                    ON production_images(project_id, status, poem_id, created_at);
                    CREATE INDEX idx_production_images_similarity
                    ON production_images(poem_id, perceptual_hash, checksum);

                    CREATE TABLE qc_results (
                        id TEXT PRIMARY KEY,
                        image_id TEXT NOT NULL REFERENCES production_images(id),
                        version TEXT NOT NULL,
                        status TEXT NOT NULL,
                        score REAL NOT NULL DEFAULT 0,
                        hard_failures_json TEXT NOT NULL DEFAULT '[]',
                        warnings_json TEXT NOT NULL DEFAULT '[]',
                        checks_json TEXT NOT NULL DEFAULT '{}',
                        coverage_json TEXT NOT NULL DEFAULT '[]',
                        duplicate_of TEXT,
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX idx_qc_image_created
                    ON qc_results(image_id, created_at);

                    CREATE TABLE qc_overrides (
                        id TEXT PRIMARY KEY,
                        image_id TEXT NOT NULL REFERENCES production_images(id),
                        qc_result_id TEXT NOT NULL REFERENCES qc_results(id),
                        decision TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        actor_id TEXT NOT NULL,
                        actor_role TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX idx_qc_overrides_image
                    ON qc_overrides(image_id, created_at);

                    CREATE TABLE review_decisions (
                        id TEXT PRIMARY KEY,
                        image_id TEXT NOT NULL REFERENCES production_images(id),
                        decision TEXT NOT NULL,
                        reason_tags_json TEXT NOT NULL DEFAULT '[]',
                        note TEXT NOT NULL DEFAULT '',
                        actor_id TEXT NOT NULL,
                        actor_role TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX idx_review_decisions_image
                    ON review_decisions(image_id, created_at);

                    CREATE TABLE rework_orders (
                        id TEXT PRIMARY KEY,
                        source_image_id TEXT NOT NULL REFERENCES production_images(id),
                        project_id TEXT NOT NULL REFERENCES production_projects(id),
                        poem_id TEXT NOT NULL REFERENCES poems(id),
                        direction_id TEXT NOT NULL REFERENCES directions(id),
                        preserve_json TEXT NOT NULL DEFAULT '[]',
                        change_json TEXT NOT NULL DEFAULT '[]',
                        avoid_json TEXT NOT NULL DEFAULT '[]',
                        note TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'draft',
                        output_image_id TEXT,
                        actor_id TEXT NOT NULL,
                        actor_role TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX idx_rework_source_status
                    ON rework_orders(source_image_id, status, created_at);

                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (3, CURRENT_TIMESTAMP);

                    COMMIT;
                    """
                )
            if 4 not in applied:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;

                    ALTER TABLE generation_tasks
                    ADD COLUMN rework_order_id TEXT REFERENCES rework_orders(id);

                    CREATE INDEX idx_tasks_rework
                    ON generation_tasks(rework_order_id);

                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (4, CURRENT_TIMESTAMP);

                    COMMIT;
                    """
                )
            if 5 not in applied:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;

                    CREATE TABLE final_approvals (
                        id TEXT PRIMARY KEY,
                        image_id TEXT NOT NULL REFERENCES production_images(id),
                        reviewer_type TEXT NOT NULL,
                        decision TEXT NOT NULL,
                        reason TEXT NOT NULL DEFAULT '',
                        actor_id TEXT NOT NULL,
                        actor_role TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX idx_final_approvals_image
                    ON final_approvals(image_id, reviewer_type, created_at);

                    CREATE TABLE final_assets (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL REFERENCES production_projects(id),
                        poem_id TEXT NOT NULL REFERENCES poems(id),
                        image_id TEXT NOT NULL REFERENCES production_images(id),
                        version INTEGER NOT NULL,
                        is_current INTEGER NOT NULL DEFAULT 1,
                        status TEXT NOT NULL DEFAULT 'locked',
                        spec_json TEXT NOT NULL DEFAULT '{}',
                        checksum TEXT NOT NULL,
                        file_path TEXT NOT NULL,
                        mime_type TEXT NOT NULL,
                        width INTEGER NOT NULL,
                        height INTEGER NOT NULL,
                        qc_result_id TEXT NOT NULL REFERENCES qc_results(id),
                        content_approval_id TEXT NOT NULL REFERENCES final_approvals(id),
                        art_approval_id TEXT NOT NULL REFERENCES final_approvals(id),
                        created_by TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(poem_id, version)
                    );

                    CREATE UNIQUE INDEX idx_final_assets_current_poem
                    ON final_assets(poem_id) WHERE is_current = 1;
                    CREATE INDEX idx_final_assets_project_created
                    ON final_assets(project_id, created_at);

                    CREATE TABLE export_packages (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL REFERENCES production_projects(id),
                        name TEXT NOT NULL,
                        status TEXT NOT NULL,
                        output_path TEXT NOT NULL DEFAULT '',
                        manifest_path TEXT NOT NULL DEFAULT '',
                        asset_count INTEGER NOT NULL DEFAULT 0,
                        package_checksum TEXT NOT NULL DEFAULT '',
                        created_by TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        completed_at TEXT,
                        error TEXT NOT NULL DEFAULT ''
                    );

                    CREATE INDEX idx_exports_project_created
                    ON export_packages(project_id, created_at);

                    CREATE TABLE export_items (
                        id TEXT PRIMARY KEY,
                        package_id TEXT NOT NULL REFERENCES export_packages(id),
                        final_asset_id TEXT NOT NULL REFERENCES final_assets(id),
                        relative_path TEXT NOT NULL,
                        checksum TEXT NOT NULL,
                        file_size INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(package_id, final_asset_id),
                        UNIQUE(package_id, relative_path)
                    );

                    CREATE INDEX idx_export_items_package
                    ON export_items(package_id);

                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (5, CURRENT_TIMESTAMP);

                    COMMIT;
                    """
                )
            if 6 not in applied:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;

                    CREATE TABLE style_pack_versions (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL REFERENCES production_projects(id),
                        style_id TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        short_name TEXT NOT NULL DEFAULT '',
                        description TEXT NOT NULL DEFAULT '',
                        prompt_fragment TEXT NOT NULL,
                        palette_json TEXT NOT NULL DEFAULT '[]',
                        settings_json TEXT NOT NULL DEFAULT '{}',
                        applicable_topics_json TEXT NOT NULL DEFAULT '[]',
                        status TEXT NOT NULL,
                        published_at TEXT,
                        created_by TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(project_id, style_id, version)
                    );

                    CREATE INDEX idx_style_pack_project_status
                    ON style_pack_versions(project_id, status, style_id, version);

                    ALTER TABLE generation_batches
                    ADD COLUMN style_version_id TEXT NOT NULL DEFAULT '';

                    ALTER TABLE production_images
                    ADD COLUMN style_version_id TEXT NOT NULL DEFAULT '';

                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (6, CURRENT_TIMESTAMP);

                    COMMIT;
                    """
                )
            if 7 not in applied:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;

                    ALTER TABLE production_images
                    ADD COLUMN prompt_hash TEXT NOT NULL DEFAULT '';

                    ALTER TABLE production_images
                    ADD COLUMN prompt_template_version TEXT NOT NULL DEFAULT '';

                    ALTER TABLE production_images
                    ADD COLUMN prompt_segments_json TEXT NOT NULL DEFAULT '{}';

                    CREATE INDEX idx_production_images_prompt_hash
                    ON production_images(project_id, prompt_hash);

                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (7, CURRENT_TIMESTAMP);

                    COMMIT;
                    """
                )
            if 8 not in applied:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;

                    ALTER TABLE requirements
                    ADD COLUMN content_version_id TEXT NOT NULL DEFAULT '';

                    ALTER TABLE requirements
                    ADD COLUMN schema_version TEXT NOT NULL DEFAULT 'legacy';

                    ALTER TABLE requirements
                    ADD COLUMN generator_version TEXT NOT NULL DEFAULT 'legacy';

                    ALTER TABLE requirements
                    ADD COLUMN input_hash TEXT NOT NULL DEFAULT '';

                    ALTER TABLE requirements
                    ADD COLUMN cache_hit INTEGER NOT NULL DEFAULT 0;

                    ALTER TABLE requirements
                    ADD COLUMN validation_json TEXT NOT NULL DEFAULT '{}';

                    CREATE TABLE requirement_generation_runs (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL REFERENCES production_projects(id),
                        poem_id TEXT NOT NULL REFERENCES poems(id),
                        content_version_id TEXT NOT NULL DEFAULT '',
                        instruction_id TEXT NOT NULL DEFAULT '',
                        schema_version TEXT NOT NULL,
                        generator_version TEXT NOT NULL,
                        input_hash TEXT NOT NULL,
                        status TEXT NOT NULL,
                        cache_hit INTEGER NOT NULL DEFAULT 0,
                        repair_attempts INTEGER NOT NULL DEFAULT 0,
                        raw_output_json TEXT,
                        normalized_output_json TEXT,
                        validation_json TEXT NOT NULL DEFAULT '{}',
                        error_code TEXT NOT NULL DEFAULT '',
                        error_message TEXT NOT NULL DEFAULT '',
                        requirement_id TEXT,
                        created_by TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        completed_at TEXT NOT NULL,
                        resolved_at TEXT
                    );

                    CREATE INDEX idx_requirement_runs_project_status
                    ON requirement_generation_runs(project_id, status, created_at);

                    CREATE INDEX idx_requirement_runs_poem_created
                    ON requirement_generation_runs(poem_id, created_at);

                    CREATE INDEX idx_requirement_runs_cache
                    ON requirement_generation_runs(input_hash, schema_version,
                                                   generator_version, status);

                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (8, CURRENT_TIMESTAMP);

                    COMMIT;
                    """
                )

    def _recover_interrupted_work(self) -> None:
        """Make crash recovery explicit without automatically repeating billed work."""

        now = utc_now()
        with self.lock, self._connect() as connection:
            running_tasks = connection.execute(
                "SELECT * FROM generation_tasks WHERE status = 'running'"
            ).fetchall()
            active_batches = connection.execute(
                """
                SELECT * FROM generation_batches
                WHERE status IN ('queued', 'running')
                """
            ).fetchall()
            if not running_tasks and not active_batches:
                return
            connection.execute("BEGIN IMMEDIATE")
            try:
                for task in running_tasks:
                    connection.execute(
                        """
                        UPDATE generation_tasks
                        SET status = 'blocked',
                            last_error_code = 'OUTCOME_UNKNOWN',
                            last_error_message = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            "服务在任务执行中退出；为避免重复计费，已停止自动重试，请人工核对后重试。",
                            now,
                            task["id"],
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE generation_attempts
                        SET status = 'interrupted',
                            error_code = 'OUTCOME_UNKNOWN',
                            error_message = ?, finished_at = ?
                        WHERE task_id = ? AND status = 'running'
                        """,
                        (
                            "服务中断，外部调用结果未知。",
                            now,
                            task["id"],
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE poems
                        SET status = 'blocked', blocked_reason = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            "生成任务执行结果未知，等待人工核对。",
                            now,
                            task["poem_id"],
                        ),
                    )
                    self._audit(
                        connection,
                        actor={"id": "system", "role": "system"},
                        action="task.interrupted",
                        target_type="generation_task",
                        target_id=task["id"],
                        before={"status": "running"},
                        after={"status": "blocked", "code": "OUTCOME_UNKNOWN"},
                    )
                for batch in active_batches:
                    connection.execute(
                        """
                        UPDATE generation_batches
                        SET status = 'paused', updated_at = ?
                        WHERE id = ?
                        """,
                        (now, batch["id"]),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _read_seed(self, path: Path) -> list[dict[str, Any]]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        return payload if isinstance(payload, list) else []

    def _seed(self) -> None:
        poems = self._read_seed(self.poem_seed_path)
        styles = self._read_seed(self.style_seed_path)
        now = utc_now()
        default_style = styles[0]["id"] if styles else ""
        instruction_content = {
            "audience": "教育内容与出版团队",
            "visual_goal": "诗意准确、唐代语境合理、系列风格统一",
            "composition_rules": [
                "主体层级清楚",
                "为诗文排版预留安全区域",
                "跨诗保持景别与叙事方式的多样性",
            ],
            "historical_rules": [
                "服饰、建筑、器物符合唐代语境",
                "有争议内容标记不确定性并交人工复核",
            ],
            "global_avoid": [
                "画面文字",
                "水印和标志",
                "现代器物",
                "受保护角色或品牌",
                "夸张仙侠特效",
            ],
        }
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO production_projects(
                        id, name, purpose, audience, aspect_ratio, style_id,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'in_progress', ?, ?)
                    """,
                    (
                        DEFAULT_PROJECT_ID,
                        "唐诗三百首 · 插图批量生产",
                        "教育出版与数字内容插图资产",
                        "内容编辑、美术指导与生产运营",
                        "portrait",
                        default_style,
                        now,
                        now,
                    ),
                )
                for poem in poems:
                    lines = poem.get("lines") or []
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO poems(
                            id, project_id, title, author, dynasty, lines_json,
                            theme, mood, imagery_json, source, status,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            poem["id"],
                            DEFAULT_PROJECT_ID,
                            poem.get("title", ""),
                            poem.get("author", ""),
                            poem.get("dynasty", "唐"),
                            _json(lines),
                            poem.get("theme", ""),
                            poem.get("mood", ""),
                            _json(poem.get("imagery", [])),
                            poem.get("source", "项目内置基准诗数据"),
                            "requirement_draft",
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO content_versions(
                            id, poem_id, version, lines_json, notes, source,
                            status, approved_by, created_at
                        ) VALUES (?, ?, 1, ?, ?, ?, 'approved', ?, ?)
                        """,
                        (
                            f"content_{poem['id']}_v1",
                            poem["id"],
                            _json(lines),
                            poem.get("visual_brief", ""),
                            poem.get("source", "项目内置基准诗数据"),
                            "seed",
                            now,
                        ),
                    )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO instruction_versions(
                        id, project_id, version, name, content_json, status,
                        published_at, created_by, created_at
                    ) VALUES (?, ?, 1, ?, ?, 'published', ?, 'seed', ?)
                    """,
                    (
                        "instruction_global_v1",
                        DEFAULT_PROJECT_ID,
                        "唐诗三百首全局创作规范 v1",
                        _json(instruction_content),
                        now,
                        now,
                    ),
                )
                for style in styles:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO style_pack_versions(
                            id, project_id, style_id, version, name, short_name,
                            description, prompt_fragment, palette_json,
                            settings_json, applicable_topics_json, status,
                            published_at, created_by, created_at
                        ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 'published',
                                  ?, 'seed', ?)
                        """,
                        (
                            f"stylev_{style['id']}_v1",
                            DEFAULT_PROJECT_ID,
                            style["id"],
                            style.get("name", style["id"]),
                            style.get("short_name", ""),
                            style.get("description", ""),
                            style.get("prompt_fragment", ""),
                            _json(style.get("palette", [])),
                            _json(
                                {
                                    key: style.get(key, "")
                                    for key in (
                                        "background",
                                        "foreground",
                                        "accent",
                                        "paper",
                                    )
                                }
                            ),
                            _json(style.get("applicable_topics", ["通用"])),
                            now,
                            now,
                        ),
                    )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO budget_policies(
                        project_id, currency, hard_limit, soft_ratio, spent,
                        updated_at
                    ) VALUES (?, 'USD', 100, 0.7, 0, ?)
                    """,
                    (DEFAULT_PROJECT_ID, now),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _actor(actor: dict[str, Any] | None) -> tuple[str, str]:
        actor = actor or {}
        actor_id = str(actor.get("id") or "local-user").strip()[:80]
        actor_role = str(actor.get("role") or "producer").strip()[:40]
        return actor_id or "local-user", actor_role or "producer"

    def _audit(
        self,
        connection: sqlite3.Connection,
        *,
        actor: dict[str, Any] | None,
        action: str,
        target_type: str,
        target_id: str,
        before: Any = None,
        after: Any = None,
    ) -> None:
        actor_id, actor_role = self._actor(actor)
        connection.execute(
            """
            INSERT INTO audit_events(
                id, actor_id, actor_role, action, target_type, target_id,
                before_json, after_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _new_id("audit"),
                actor_id,
                actor_role,
                action,
                target_type,
                target_id,
                _json(before) if before is not None else None,
                _json(after) if after is not None else None,
                utc_now(),
            ),
        )

    def record_system_audit(
        self,
        action: str,
        target_type: str,
        target_id: str,
        *,
        before: Any = None,
        after: Any = None,
        actor: dict[str, Any] | None = None,
    ) -> None:
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._audit(
                    connection,
                    actor=actor,
                    action=str(action)[:100],
                    target_type=str(target_type)[:80],
                    target_id=str(target_id)[:160],
                    before=before,
                    after=after,
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _project_dict(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _poem_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["lines"] = _decode(item.pop("lines_json", None), [])
        item["imagery"] = _decode(item.pop("imagery_json", None), [])
        if "requirement_content" in item:
            item["requirement"] = (
                {
                    "id": item.pop("requirement_id"),
                    "status": item.pop("requirement_status"),
                    "version": item.pop("requirement_version"),
                    "content": _decode(item.pop("requirement_content"), {}),
                }
                if item.get("requirement_id")
                else None
            )
            item.pop("requirement_id", None)
            item.pop("requirement_status", None)
            item.pop("requirement_version", None)
            item.pop("requirement_content", None)
        return item

    @staticmethod
    def _requirement_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["content"] = _decode(item.pop("content_json", None), {})
        if "validation_json" in item:
            item["validation"] = _decode(item.pop("validation_json", None), {})
        if "cache_hit" in item:
            item["cache_hit"] = bool(item["cache_hit"])
        return item

    @staticmethod
    def _requirement_run_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["raw_output"] = _decode(item.pop("raw_output_json", None), None)
        item["normalized_output"] = _decode(
            item.pop("normalized_output_json", None), None
        )
        item["validation"] = _decode(item.pop("validation_json", None), {})
        item["cache_hit"] = bool(item.get("cache_hit"))
        return item

    @staticmethod
    def _direction_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["content"] = _decode(item.pop("content_json", None), {})
        return item

    @staticmethod
    def _instruction_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["content"] = _decode(item.pop("content_json", None), {})
        return item

    @staticmethod
    def _style_pack_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["palette"] = _decode(item.pop("palette_json", None), [])
        item["settings"] = _decode(item.pop("settings_json", None), {})
        item["applicable_topics"] = _decode(
            item.pop("applicable_topics_json", None), []
        )
        return item

    @staticmethod
    def _batch_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["budget_snapshot"] = _decode(item.pop("budget_snapshot_json", None), {})
        item["settings"] = _decode(item.pop("settings_json", None), {})
        return item

    @staticmethod
    def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["prompt"] = _decode(item.pop("prompt_json", None), {})
        return item

    @staticmethod
    def _attempt_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["request"] = _decode(item.pop("request_json", None), {})
        item["response"] = _decode(item.pop("response_json", None), None)
        return item

    @staticmethod
    def _qc_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["hard_failures"] = _decode(item.pop("hard_failures_json", None), [])
        item["warnings"] = _decode(item.pop("warnings_json", None), [])
        item["checks"] = _decode(item.pop("checks_json", None), {})
        item["coverage"] = _decode(item.pop("coverage_json", None), [])
        return item

    @staticmethod
    def _production_image_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        for key in ("reason_tags_json", "preserve_json", "change_json", "avoid_json"):
            if key in item:
                item[key.removesuffix("_json")] = _decode(item.pop(key), [])
        if "prompt_segments_json" in item:
            item["prompt_segments"] = _decode(
                item.pop("prompt_segments_json", None), {}
            )
        return item

    def project(self, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM production_projects WHERE id = ?",
                (project_id,),
            ).fetchone()
        if not row:
            raise WorkflowError("PROJECT_NOT_FOUND", "生产项目不存在。", status=404)
        return self._project_dict(row)

    def health(self) -> dict[str, Any]:
        with self._connect() as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            task_counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM generation_tasks GROUP BY status"
                ).fetchall()
            }
            qc_blocked = connection.execute(
                """
                SELECT COUNT(*) AS count FROM production_images
                WHERE status IN ('qc_blocked', 'needs_manual_qc')
                """
            ).fetchone()["count"]
            failed_exports = connection.execute(
                "SELECT COUNT(*) AS count FROM export_packages WHERE status = 'failed'"
            ).fetchone()["count"]
            final_paths = [
                row["file_path"]
                for row in connection.execute(
                    "SELECT file_path FROM final_assets WHERE is_current = 1"
                ).fetchall()
            ]
        missing_final_files = sum(not Path(path).is_file() for path in final_paths)
        degraded = integrity != "ok" or missing_final_files > 0
        return {
            "status": "degraded" if degraded else "ok",
            "database_integrity": integrity,
            "database_bytes": self.database_path.stat().st_size
            if self.database_path.is_file()
            else 0,
            "tasks": task_counts,
            "qc_blocked": qc_blocked,
            "failed_exports": failed_exports,
            "missing_final_files": missing_final_files,
        }

    def summary(self, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any]:
        project = self.project(project_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM poems
                WHERE project_id = ?
                GROUP BY status
                """,
                (project_id,),
            ).fetchall()
            recent = connection.execute(
                """
                SELECT id, actor_id, actor_role, action, target_type,
                       target_id, created_at
                FROM audit_events
                ORDER BY created_at DESC
                LIMIT 12
                """
            ).fetchall()
            requirement_review = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM requirements r
                JOIN poems p ON p.id = r.poem_id
                WHERE p.project_id = ? AND r.is_current = 1
                  AND r.status = 'in_review'
                """,
                (project_id,),
            ).fetchone()["count"]
            direction_review = connection.execute(
                """
                SELECT COUNT(DISTINCT d.poem_id) AS count
                FROM directions d
                JOIN poems p ON p.id = d.poem_id
                WHERE p.project_id = ? AND d.is_current = 1
                  AND d.status = 'in_review'
                """,
                (project_id,),
            ).fetchone()["count"]
        counts = {row["status"]: row["count"] for row in rows}
        total = sum(counts.values())
        stages = []
        for key, label, statuses in STAGE_DEFINITIONS:
            stages.append(
                {
                    "key": key,
                    "label": label,
                    "count": sum(counts.get(status, 0) for status in statuses),
                    "statuses": list(statuses),
                }
            )
        delivered = counts.get("exported", 0)
        approved = counts.get("approved", 0)
        return {
            "project": project,
            "total_poems": total,
            "stages": stages,
            "status_counts": counts,
            "completion_percent": round((delivered + approved) / total * 100)
            if total
            else 0,
            "todos": {
                "requirement_review": requirement_review,
                "direction_review": direction_review,
                "ready_for_production": counts.get("ready_for_production", 0),
                "blocked": counts.get("blocked", 0),
            },
            "recent_activity": [dict(row) for row in recent],
        }

    def production_report(
        self,
        project_id: str = DEFAULT_PROJECT_ID,
        *,
        days: int = 7,
    ) -> dict[str, Any]:
        """Return bounded operational metrics and actionable anomaly groups."""

        self.project(project_id)
        try:
            days = max(1, min(int(days), 90))
        except (TypeError, ValueError) as exc:
            raise WorkflowError("INVALID_REPORT_RANGE", "日报范围必须是 1–90 天。") from exc
        now = datetime.now(timezone.utc)
        start_date = (now - timedelta(days=days - 1)).date()
        cutoff = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc).isoformat()
        stale_cutoff = (now - timedelta(minutes=30)).isoformat(timespec="seconds")
        with self._connect() as connection:
            task_metrics = dict(
                connection.execute(
                    """
                    SELECT COUNT(*) AS total,
                           SUM(CASE WHEN t.status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded,
                           SUM(CASE WHEN t.status = 'failed' THEN 1 ELSE 0 END) AS failed,
                           SUM(CASE WHEN t.status = 'blocked' THEN 1 ELSE 0 END) AS blocked
                    FROM generation_tasks t
                    JOIN generation_batches b ON b.id = t.batch_id
                    WHERE b.project_id = ?
                      AND COALESCE(t.finished_at, t.updated_at) >= ?
                    """,
                    (project_id, cutoff),
                ).fetchone()
            )
            generated = connection.execute(
                """
                SELECT COUNT(*) AS count FROM production_images
                WHERE project_id = ? AND created_at >= ?
                """,
                (project_id, cutoff),
            ).fetchone()["count"]
            reviewed = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM review_decisions r
                JOIN production_images i ON i.id = r.image_id
                WHERE i.project_id = ? AND r.created_at >= ?
                """,
                (project_id, cutoff),
            ).fetchone()["count"]
            reworks = connection.execute(
                """
                SELECT COUNT(*) AS count FROM rework_orders
                WHERE project_id = ? AND created_at >= ?
                """,
                (project_id, cutoff),
            ).fetchone()["count"]
            finalized = connection.execute(
                """
                SELECT COUNT(*) AS count FROM final_assets
                WHERE project_id = ? AND created_at >= ?
                """,
                (project_id, cutoff),
            ).fetchone()["count"]
            exported = connection.execute(
                """
                SELECT COALESCE(SUM(asset_count), 0) AS count
                FROM export_packages
                WHERE project_id = ? AND status = 'completed' AND created_at >= ?
                """,
                (project_id, cutoff),
            ).fetchone()["count"]
            cost = connection.execute(
                """
                SELECT COALESCE(SUM(u.actual_cost), 0) AS cost
                FROM usage_records u
                JOIN generation_batches b ON b.id = u.batch_id
                WHERE b.project_id = ? AND u.created_at >= ?
                """,
                (project_id, cutoff),
            ).fetchone()["cost"]
            task_daily = connection.execute(
                """
                SELECT substr(COALESCE(t.finished_at, t.updated_at), 1, 10) AS day,
                       SUM(CASE WHEN t.status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded,
                       SUM(CASE WHEN t.status IN ('failed', 'blocked') THEN 1 ELSE 0 END) AS failed
                FROM generation_tasks t
                JOIN generation_batches b ON b.id = t.batch_id
                WHERE b.project_id = ?
                  AND COALESCE(t.finished_at, t.updated_at) >= ?
                GROUP BY day
                """,
                (project_id, cutoff),
            ).fetchall()
            image_daily = connection.execute(
                """
                SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS generated
                FROM production_images
                WHERE project_id = ? AND created_at >= ? GROUP BY day
                """,
                (project_id, cutoff),
            ).fetchall()
            final_daily = connection.execute(
                """
                SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS finalized
                FROM final_assets
                WHERE project_id = ? AND created_at >= ? GROUP BY day
                """,
                (project_id, cutoff),
            ).fetchall()
            error_rows = connection.execute(
                """
                SELECT COALESCE(NULLIF(t.last_error_code, ''), 'UNKNOWN') AS code,
                       COUNT(*) AS count
                FROM generation_tasks t
                JOIN generation_batches b ON b.id = t.batch_id
                WHERE b.project_id = ? AND t.status IN ('failed', 'blocked')
                  AND t.updated_at >= ?
                GROUP BY code ORDER BY count DESC LIMIT 10
                """,
                (project_id, cutoff),
            ).fetchall()
            anomaly_counts = dict(
                connection.execute(
                    """
                    SELECT
                      (SELECT COUNT(*) FROM generation_tasks t JOIN generation_batches b ON b.id=t.batch_id WHERE b.project_id=? AND t.status='failed') AS failed_tasks,
                      (SELECT COUNT(*) FROM generation_tasks t JOIN generation_batches b ON b.id=t.batch_id WHERE b.project_id=? AND t.status='blocked') AS blocked_tasks,
                      (SELECT COUNT(*) FROM production_images WHERE project_id=? AND status='qc_blocked') AS qc_blocked,
                      (SELECT COUNT(*) FROM production_images WHERE project_id=? AND status='needs_manual_qc') AS manual_qc,
                      (SELECT COUNT(*) FROM generation_batches WHERE project_id=? AND status='budget_blocked') AS budget_blocked,
                      (SELECT COUNT(*) FROM export_packages WHERE project_id=? AND status='failed') AS failed_exports,
                      (SELECT COUNT(*) FROM requirement_generation_runs WHERE project_id=? AND status='failed' AND resolved_at IS NULL) AS failed_requirement_runs,
                      (SELECT COUNT(*) FROM poems WHERE project_id=? AND status='blocked') AS blocked_poems,
                      (SELECT COUNT(*) FROM generation_tasks t JOIN generation_batches b ON b.id=t.batch_id WHERE b.project_id=? AND t.status='running' AND t.updated_at < ?) AS stale_tasks
                    """,
                    (
                        project_id,
                        project_id,
                        project_id,
                        project_id,
                        project_id,
                        project_id,
                        project_id,
                        project_id,
                        project_id,
                        stale_cutoff,
                    ),
                ).fetchone()
            )

        daily_by_date: dict[str, dict[str, Any]] = {}
        for offset in range(days):
            day = (start_date + timedelta(days=offset)).isoformat()
            daily_by_date[day] = {
                "date": day,
                "generated": 0,
                "succeeded": 0,
                "failed": 0,
                "finalized": 0,
            }
        for row in task_daily:
            if row["day"] in daily_by_date:
                daily_by_date[row["day"]].update(
                    succeeded=int(row["succeeded"] or 0),
                    failed=int(row["failed"] or 0),
                )
        for row in image_daily:
            if row["day"] in daily_by_date:
                daily_by_date[row["day"]]["generated"] = int(row["generated"] or 0)
        for row in final_daily:
            if row["day"] in daily_by_date:
                daily_by_date[row["day"]]["finalized"] = int(row["finalized"] or 0)

        anomaly_definitions = (
            ("stale_tasks", "疑似卡住任务", "critical", "queue", "running", "核对 Provider 调用与任务 Attempt"),
            ("blocked_tasks", "结果未知任务", "critical", "queue", "blocked", "人工核对后显式重试"),
            ("failed_tasks", "失败任务", "high", "queue", "failed", "按错误分类重试或修正参数"),
            ("qc_blocked", "QC 硬失败", "high", "review", "qc_blocked", "检查文件、比例、文字或重复图"),
            ("manual_qc", "待人工 QC", "medium", "review", "needs_manual_qc", "人工确认自动规则未覆盖项"),
            ("budget_blocked", "预算阻塞批次", "high", "resources", "budget_blocked", "调整预算或取消批次"),
            ("failed_exports", "失败导出包", "high", "assets", "failed", "修复文件后重新导出"),
            ("failed_requirement_runs", "需求生成异常", "high", "requirements", "failed", "查看 Schema 错误并重试该诗需求"),
            ("blocked_poems", "阻塞诗词", "medium", "overview", "blocked", "查看阻塞原因并指定责任人"),
        )
        anomalies = [
            {
                "id": key,
                "label": label,
                "severity": severity,
                "count": int(anomaly_counts.get(key) or 0),
                "view": view,
                "filter": filter_value,
                "suggested_action": suggested_action,
            }
            for key, label, severity, view, filter_value, suggested_action in anomaly_definitions
            if int(anomaly_counts.get(key) or 0) > 0
        ]
        converged = int(task_metrics.get("succeeded") or 0) + int(
            task_metrics.get("failed") or 0
        ) + int(task_metrics.get("blocked") or 0)
        return {
            "project_id": project_id,
            "days": days,
            "period_start": cutoff,
            "generated": int(generated or 0),
            "reviewed": int(reviewed or 0),
            "reworks": int(reworks or 0),
            "finalized": int(finalized or 0),
            "exported_assets": int(exported or 0),
            "actual_cost": round(float(cost or 0), 6),
            "tasks": {
                "total": int(task_metrics.get("total") or 0),
                "succeeded": int(task_metrics.get("succeeded") or 0),
                "failed": int(task_metrics.get("failed") or 0),
                "blocked": int(task_metrics.get("blocked") or 0),
                "success_rate": round(int(task_metrics.get("succeeded") or 0) / converged * 100, 1)
                if converged
                else 0,
            },
            "daily": list(daily_by_date.values()),
            "error_breakdown": [dict(row) for row in error_rows],
            "anomalies": anomalies,
            "anomaly_count": sum(item["count"] for item in anomalies),
            "generated_at": utc_now(),
        }

    def list_poems(
        self,
        project_id: str = DEFAULT_PROJECT_ID,
        *,
        status: str | None = None,
        query: str = "",
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        if status and status not in POEM_STATUSES:
            raise WorkflowError("INVALID_STATUS", "不支持的诗词阶段。")
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        where = ["p.project_id = ?"]
        params: list[Any] = [project_id]
        if status:
            where.append("p.status = ?")
            params.append(status)
        query = query.strip()[:100]
        if query:
            where.append(
                "(p.title LIKE ? OR p.author LIKE ? OR p.theme LIKE ? OR p.mood LIKE ?)"
            )
            like = f"%{query}%"
            params.extend([like, like, like, like])
        where_sql = " AND ".join(where)
        with self._connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) AS count FROM poems p WHERE {where_sql}",
                params,
            ).fetchone()["count"]
            rows = connection.execute(
                f"""
                SELECT p.*,
                       r.id AS requirement_id,
                       r.status AS requirement_status,
                       r.version AS requirement_version,
                       r.content_json AS requirement_content,
                       (
                           SELECT COUNT(*)
                           FROM directions d
                           WHERE d.poem_id = p.id AND d.is_current = 1
                       ) AS direction_count,
                       (
                           SELECT COUNT(*)
                           FROM directions d
                           WHERE d.poem_id = p.id AND d.is_current = 1
                             AND d.status = 'approved'
                       ) AS approved_direction_count
                FROM poems p
                LEFT JOIN requirements r
                  ON r.poem_id = p.id AND r.is_current = 1
                WHERE {where_sql}
                ORDER BY p.updated_at DESC, p.title
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        return {
            "items": [self._poem_dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @staticmethod
    def _normalize_import_record(
        record: Any,
        index: int,
    ) -> tuple[dict[str, Any] | None, list[str], list[str]]:
        errors: list[str] = []
        warnings: list[str] = []
        if not isinstance(record, dict):
            return None, ["记录必须是 JSON 对象。"], warnings
        poem_id = str(record.get("id") or "").strip().lower()[:80]
        title = str(record.get("title") or "").strip()[:120]
        author = str(record.get("author") or "").strip()[:80]
        dynasty = str(record.get("dynasty") or "唐").strip()[:20]
        lines = record.get("lines")
        if isinstance(lines, str):
            lines = [item.strip() for item in lines.splitlines() if item.strip()]
        elif isinstance(lines, list):
            lines = [str(item).strip()[:200] for item in lines if str(item).strip()]
        else:
            lines = []
        source = str(record.get("source") or "").strip()[:500]
        if not poem_id:
            errors.append("缺少稳定 id。")
        elif not re.fullmatch(r"[a-z0-9-]{3,80}", poem_id):
            errors.append("id 仅支持小写字母、数字和连字符。")
        if not title:
            errors.append("缺少诗名。")
        if not author:
            errors.append("缺少作者。")
        if not lines:
            errors.append("正文不能为空。")
        if not source:
            warnings.append("缺少来源，导入后必须补录并完成内容复核。")
        imagery = record.get("imagery") or []
        if not isinstance(imagery, list):
            imagery = []
            warnings.append("imagery 不是数组，已按空数组处理。")
        normalized = {
            "id": poem_id or f"invalid-row-{index + 1}",
            "title": title,
            "author": author,
            "dynasty": dynasty or "唐",
            "lines": lines[:20],
            "theme": str(record.get("theme") or "").strip()[:80],
            "mood": str(record.get("mood") or "").strip()[:200],
            "imagery": [str(item).strip()[:80] for item in imagery if str(item).strip()][:20],
            "source": source,
            "notes": str(record.get("notes") or record.get("visual_brief") or "").strip()[:2000],
        }
        return normalized, errors, warnings

    def preview_poem_import(
        self,
        project_id: str,
        records: Any,
    ) -> dict[str, Any]:
        self.project(project_id)
        if not isinstance(records, list):
            raise WorkflowError("RECORDS_REQUIRED", "records 必须是诗词数组。")
        if not records:
            raise WorkflowError("EMPTY_IMPORT", "导入文件中没有诗词记录。")
        if len(records) > 500:
            raise WorkflowError("IMPORT_TOO_LARGE", "单次最多导入 500 首诗词。")
        normalized_records = []
        duplicate_ids: set[str] = set()
        seen_ids: set[str] = set()
        for index, record in enumerate(records):
            normalized, errors, warnings = self._normalize_import_record(record, index)
            if normalized and normalized["id"] in seen_ids:
                duplicate_ids.add(normalized["id"])
                errors.append("同一导入文件中 id 重复。")
            if normalized:
                seen_ids.add(normalized["id"])
            normalized_records.append(
                {
                    "index": index,
                    "record": normalized,
                    "errors": errors,
                    "warnings": warnings,
                }
            )
        valid_ids = [
            item["record"]["id"]
            for item in normalized_records
            if item["record"] and not item["errors"]
        ]
        existing: dict[str, sqlite3.Row] = {}
        if valid_ids:
            placeholders = ",".join("?" for _ in valid_ids)
            with self._connect() as connection:
                rows = connection.execute(
                    f"SELECT * FROM poems WHERE id IN ({placeholders})",
                    valid_ids,
                ).fetchall()
            existing = {row["id"]: row for row in rows}
        items = []
        counts = {
            "total": len(records),
            "new": 0,
            "unchanged": 0,
            "conflict": 0,
            "invalid": 0,
            "warnings": 0,
        }
        for item in normalized_records:
            record = item["record"]
            errors = item["errors"]
            warnings = item["warnings"]
            status = "new"
            conflict_fields: list[str] = []
            if errors or not record:
                status = "invalid"
            elif record["id"] in existing:
                current = existing[record["id"]]
                comparisons = {
                    "title": current["title"] == record["title"],
                    "author": current["author"] == record["author"],
                    "lines": _decode(current["lines_json"], []) == record["lines"],
                }
                conflict_fields = [key for key, matches in comparisons.items() if not matches]
                status = "conflict" if conflict_fields else "unchanged"
            counts[status] += 1
            if warnings:
                counts["warnings"] += 1
            items.append(
                {
                    "index": item["index"],
                    "id": record["id"] if record else "",
                    "title": record["title"] if record else "",
                    "author": record["author"] if record else "",
                    "status": status,
                    "errors": errors,
                    "warnings": warnings,
                    "conflict_fields": conflict_fields,
                    "normalized": record,
                }
            )
        return {
            "project_id": project_id,
            "can_commit": counts["invalid"] == 0 and counts["conflict"] == 0,
            "counts": counts,
            "items": items,
        }

    def import_poems(
        self,
        project_id: str,
        records: Any,
        *,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        preview = self.preview_poem_import(project_id, records)
        if not preview["can_commit"]:
            raise WorkflowError(
                "IMPORT_BLOCKED",
                "导入包含无效记录或正文冲突，请处理后重试。",
                status=409,
            )
        new_items = [item for item in preview["items"] if item["status"] == "new"]
        now = utc_now()
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for item in new_items:
                    poem = item["normalized"]
                    connection.execute(
                        """
                        INSERT INTO poems(
                            id, project_id, title, author, dynasty, lines_json,
                            theme, mood, imagery_json, source, status,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  'content_review', ?, ?)
                        """,
                        (
                            poem["id"],
                            project_id,
                            poem["title"],
                            poem["author"],
                            poem["dynasty"],
                            _json(poem["lines"]),
                            poem["theme"],
                            poem["mood"],
                            _json(poem["imagery"]),
                            poem["source"],
                            now,
                            now,
                        ),
                    )
                    content_id = _new_id("content")
                    connection.execute(
                        """
                        INSERT INTO content_versions(
                            id, poem_id, version, lines_json, notes, source,
                            status, created_at
                        ) VALUES (?, ?, 1, ?, ?, ?, 'in_review', ?)
                        """,
                        (
                            content_id,
                            poem["id"],
                            _json(poem["lines"]),
                            poem["notes"],
                            poem["source"],
                            now,
                        ),
                    )
                    self._audit(
                        connection,
                        actor=actor,
                        action="poem.imported",
                        target_type="poem",
                        target_id=poem["id"],
                        after={
                            "poem": poem,
                            "content_version_id": content_id,
                            "status": "content_review",
                        },
                    )
                connection.execute(
                    "UPDATE production_projects SET updated_at = ? WHERE id = ?",
                    (now, project_id),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {
            "project_id": project_id,
            "imported": len(new_items),
            "unchanged": preview["counts"]["unchanged"],
            "warnings": preview["counts"]["warnings"],
            "items": preview["items"],
        }

    def approve_content(
        self,
        poem_id: str,
        *,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _, actor_role = self._actor(actor)
        if actor_role not in {"content_editor", "producer", "system_admin"}:
            raise WorkflowError(
                "ROLE_FORBIDDEN",
                "只有内容编辑、制片人或系统管理员可以批准内容版本。",
                status=403,
            )
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                poem = connection.execute(
                    "SELECT * FROM poems WHERE id = ?",
                    (poem_id,),
                ).fetchone()
                if not poem:
                    raise WorkflowError(
                        "POEM_NOT_FOUND", "诗词不存在。", status=404
                    )
                if poem["status"] not in {"imported", "content_review"}:
                    raise WorkflowError(
                        "INVALID_CONTENT_STATE",
                        "只有待校验内容可以批准。",
                        status=409,
                    )
                if not poem["source"].strip():
                    raise WorkflowError(
                        "SOURCE_REQUIRED",
                        "补充诗词来源后才能批准内容。",
                        status=409,
                    )
                content = connection.execute(
                    """
                    SELECT *
                    FROM content_versions
                    WHERE poem_id = ?
                    ORDER BY version DESC
                    LIMIT 1
                    """,
                    (poem_id,),
                ).fetchone()
                if not content:
                    raise WorkflowError(
                        "CONTENT_VERSION_REQUIRED",
                        "诗词缺少可审核的内容版本。",
                        status=409,
                    )
                now = utc_now()
                actor_id, _ = self._actor(actor)
                connection.execute(
                    """
                    UPDATE content_versions
                    SET status = 'approved', approved_by = ?
                    WHERE id = ?
                    """,
                    (actor_id, content["id"]),
                )
                connection.execute(
                    """
                    UPDATE poems
                    SET status = 'requirement_draft', blocked_reason = '',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, poem_id),
                )
                result = self._poem_dict(
                    connection.execute(
                        "SELECT * FROM poems WHERE id = ?",
                        (poem_id,),
                    ).fetchone()
                )
                self._audit(
                    connection,
                    actor=actor,
                    action="content.approved",
                    target_type="poem",
                    target_id=poem_id,
                    before={
                        "poem_status": poem["status"],
                        "content_status": content["status"],
                        "content_version": content["version"],
                    },
                    after={
                        "poem_status": "requirement_draft",
                        "content_status": "approved",
                        "content_version": content["version"],
                    },
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def requirements(
        self,
        project_id: str = DEFAULT_PROJECT_ID,
        *,
        current_only: bool = True,
    ) -> list[dict[str, Any]]:
        current_clause = "AND r.is_current = 1" if current_only else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT r.*, p.title AS poem_title, p.author, p.theme
                FROM requirements r
                JOIN poems p ON p.id = r.poem_id
                WHERE p.project_id = ? {current_clause}
                ORDER BY r.updated_at DESC
                """,
                (project_id,),
            ).fetchall()
        return [self._requirement_dict(row) for row in rows]

    def requirement_generation_runs(
        self,
        project_id: str = DEFAULT_PROJECT_ID,
        *,
        poem_id: str | None = None,
        status: str | None = None,
        unresolved_only: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if status and status not in {"succeeded", "failed"}:
            raise WorkflowError(
                "INVALID_REQUIREMENT_RUN_STATUS",
                "不支持的需求生成运行状态。",
            )
        where = ["project_id = ?"]
        params: list[Any] = [project_id]
        if poem_id:
            where.append("poem_id = ?")
            params.append(str(poem_id)[:80])
        if status:
            where.append("status = ?")
            params.append(status)
        if unresolved_only:
            where.append("resolved_at IS NULL")
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM requirement_generation_runs
                WHERE {' AND '.join(where)}
                ORDER BY created_at DESC LIMIT ?
                """,
                [*params, max(1, min(int(limit), 500))],
            ).fetchall()
        return [self._requirement_run_dict(row) for row in rows]

    @staticmethod
    def _requirement_input_hash(
        content_version: sqlite3.Row | dict[str, Any],
        instruction: dict[str, Any],
    ) -> str:
        content = dict(content_version)
        payload = {
            "schema_version": REQUIREMENT_SCHEMA_VERSION,
            "generator_version": REQUIREMENT_GENERATOR_VERSION,
            "content_version": {
                "id": content.get("id"),
                "version": content.get("version"),
                "lines": _decode(content.get("lines_json"), content.get("lines", [])),
                "notes": content.get("notes", ""),
                "source": content.get("source", ""),
            },
            "instruction_version": {
                "id": instruction.get("id"),
                "version": instruction.get("version"),
                "content": instruction.get("content", {}),
            },
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def _record_requirement_run_locked(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        project_id: str,
        poem_id: str,
        content_version_id: str,
        instruction_id: str,
        input_hash: str,
        status: str,
        cache_hit: bool,
        repair_attempts: int,
        raw_output: Any,
        normalized_output: Any,
        validation: dict[str, Any],
        error_code: str,
        error_message: str,
        requirement_id: str | None,
        actor_id: str,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO requirement_generation_runs(
                id, project_id, poem_id, content_version_id, instruction_id,
                schema_version, generator_version, input_hash, status,
                cache_hit, repair_attempts, raw_output_json,
                normalized_output_json, validation_json, error_code,
                error_message, requirement_id, created_by, created_at,
                completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                project_id,
                poem_id,
                content_version_id,
                instruction_id,
                REQUIREMENT_SCHEMA_VERSION,
                REQUIREMENT_GENERATOR_VERSION,
                input_hash,
                status,
                int(cache_hit),
                max(0, min(int(repair_attempts), 1)),
                _json(raw_output) if raw_output is not None else None,
                _json(normalized_output) if normalized_output is not None else None,
                _json(validation),
                str(error_code)[:100],
                str(error_message)[:1000],
                requirement_id,
                actor_id,
                now,
                now,
            ),
        )

    def directions(
        self,
        project_id: str = DEFAULT_PROJECT_ID,
        *,
        current_only: bool = True,
    ) -> list[dict[str, Any]]:
        current_clause = "AND d.is_current = 1" if current_only else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT d.*, p.title AS poem_title, p.author
                FROM directions d
                JOIN poems p ON p.id = d.poem_id
                WHERE p.project_id = ? {current_clause}
                ORDER BY p.title, d.type
                """,
                (project_id,),
            ).fetchall()
        return [self._direction_dict(row) for row in rows]

    def poem_detail(self, poem_id: str) -> dict[str, Any]:
        poem_id = str(poem_id).strip()[:80]
        with self._connect() as connection:
            poem_row = connection.execute(
                "SELECT * FROM poems WHERE id = ?", (poem_id,)
            ).fetchone()
            if not poem_row:
                raise WorkflowError("POEM_NOT_FOUND", "诗词不存在。", status=404)

            content_rows = connection.execute(
                """
                SELECT * FROM content_versions
                WHERE poem_id = ? ORDER BY version DESC
                """,
                (poem_id,),
            ).fetchall()
            requirement_rows = connection.execute(
                """
                SELECT * FROM requirements
                WHERE poem_id = ? ORDER BY version DESC
                """,
                (poem_id,),
            ).fetchall()
            requirement_run_rows = connection.execute(
                """
                SELECT * FROM requirement_generation_runs
                WHERE poem_id = ? ORDER BY created_at DESC LIMIT 200
                """,
                (poem_id,),
            ).fetchall()
            direction_rows = connection.execute(
                """
                SELECT * FROM directions
                WHERE poem_id = ? ORDER BY type, version DESC
                """,
                (poem_id,),
            ).fetchall()
            task_rows = connection.execute(
                """
                SELECT t.*, b.name AS batch_name, b.provider, b.model,
                       b.style_id, b.style_version_id, b.aspect_ratio,
                       b.status AS batch_status, d.type AS direction_type
                FROM generation_tasks t
                JOIN generation_batches b ON b.id = t.batch_id
                JOIN directions d ON d.id = t.direction_id
                WHERE t.poem_id = ?
                ORDER BY t.created_at DESC LIMIT 1000
                """,
                (poem_id,),
            ).fetchall()
            image_ids = [
                row["id"]
                for row in connection.execute(
                    """
                    SELECT id FROM production_images
                    WHERE poem_id = ? ORDER BY created_at DESC LIMIT 1000
                    """,
                    (poem_id,),
                ).fetchall()
            ]
            images = [
                item
                for image_id in image_ids
                if (item := self._production_image_locked(connection, image_id))
            ]
            for image in images:
                image.pop("file_path", None)
                if image.get("final_asset"):
                    image["final_asset"].pop("file_path", None)

            rework_rows = connection.execute(
                """
                SELECT * FROM rework_orders
                WHERE poem_id = ? ORDER BY created_at DESC
                """,
                (poem_id,),
            ).fetchall()
            final_rows = connection.execute(
                """
                SELECT a.*, i.url, i.style_id, i.style_version_id
                FROM final_assets a
                JOIN production_images i ON i.id = a.image_id
                WHERE a.poem_id = ? ORDER BY a.version DESC
                """,
                (poem_id,),
            ).fetchall()
            export_rows = connection.execute(
                """
                SELECT p.id, p.name, p.status, p.asset_count,
                       p.package_checksum, p.created_at, p.completed_at,
                       a.id AS final_asset_id, a.version AS final_asset_version,
                       i.relative_path, i.checksum
                FROM export_items i
                JOIN export_packages p ON p.id = i.package_id
                JOIN final_assets a ON a.id = i.final_asset_id
                WHERE a.poem_id = ?
                ORDER BY p.created_at DESC
                """,
                (poem_id,),
            ).fetchall()

            linked_ids = [poem_id]
            linked_ids.extend(row["id"] for row in requirement_rows)
            linked_ids.extend(row["id"] for row in requirement_run_rows)
            linked_ids.extend(row["id"] for row in direction_rows)
            linked_ids.extend(row["id"] for row in task_rows)
            linked_ids.extend(image_ids)
            linked_ids.extend(row["id"] for row in rework_rows)
            linked_ids.extend(row["id"] for row in final_rows)
            linked_ids = list(dict.fromkeys(linked_ids))[:500]
            placeholders = ",".join("?" for _ in linked_ids)
            audit_rows = connection.execute(
                f"""
                SELECT * FROM audit_events
                WHERE target_id IN ({placeholders})
                ORDER BY created_at DESC LIMIT 500
                """,
                linked_ids,
            ).fetchall()

        contents = []
        for row in content_rows:
            item = dict(row)
            item["lines"] = _decode(item.pop("lines_json", None), [])
            contents.append(item)
        requirements = [self._requirement_dict(row) for row in requirement_rows]
        requirement_runs = [
            self._requirement_run_dict(row) for row in requirement_run_rows
        ]
        directions = [self._direction_dict(row) for row in direction_rows]
        tasks = [self._task_dict(row) for row in task_rows]
        reworks = [self._production_image_dict(row) for row in rework_rows]
        final_assets = []
        for row in final_rows:
            item = dict(row)
            item["spec"] = _decode(item.pop("spec_json", None), {})
            item.pop("file_path", None)
            final_assets.append(item)
        audit_events = []
        for row in audit_rows:
            item = dict(row)
            item["before"] = _decode(item.pop("before_json", None), None)
            item["after"] = _decode(item.pop("after_json", None), None)
            audit_events.append(item)
        return {
            "poem": self._poem_dict(poem_row),
            "content_versions": contents,
            "requirements": requirements,
            "requirement_generation_runs": requirement_runs,
            "directions": directions,
            "tasks": tasks,
            "images": images,
            "rework_orders": reworks,
            "final_assets": final_assets,
            "exports": [dict(row) for row in export_rows],
            "audit_events": audit_events,
            "counts": {
                "content_versions": len(contents),
                "requirements": len(requirements),
                "requirement_generation_runs": len(requirement_runs),
                "directions": len(directions),
                "tasks": len(tasks),
                "images": len(images),
                "reworks": len(reworks),
                "final_assets": len(final_assets),
                "exports": len(export_rows),
            },
        }

    def instruction(
        self,
        project_id: str = DEFAULT_PROJECT_ID,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM instruction_versions
                WHERE project_id = ? AND status = 'published'
                ORDER BY version DESC
                LIMIT 1
                """,
                (project_id,),
            ).fetchone()
        if not row:
            return None
        return self._instruction_dict(row)

    def instructions(
        self,
        project_id: str = DEFAULT_PROJECT_ID,
    ) -> list[dict[str, Any]]:
        self.project(project_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM instruction_versions
                WHERE project_id = ?
                ORDER BY version DESC
                """,
                (project_id,),
            ).fetchall()
        return [self._instruction_dict(row) for row in rows]

    @staticmethod
    def _normalized_instruction_content(content: Any) -> dict[str, Any]:
        if not isinstance(content, dict):
            raise WorkflowError("INVALID_INSTRUCTION", "指令内容必须是对象。")
        normalized = {
            "audience": str(content.get("audience") or "").strip()[:300],
            "visual_goal": str(content.get("visual_goal") or "").strip()[:1000],
        }
        if not normalized["audience"] or not normalized["visual_goal"]:
            raise WorkflowError(
                "INSTRUCTION_CORE_REQUIRED",
                "目标受众和视觉目标不能为空。",
            )
        for field in ("composition_rules", "historical_rules", "global_avoid"):
            values = content.get(field) or []
            if not isinstance(values, list):
                raise WorkflowError(
                    "INVALID_INSTRUCTION_RULES", f"{field} 必须是数组。"
                )
            normalized[field] = [
                str(value).strip()[:500]
                for value in values[:50]
                if str(value).strip()
            ]
        if not normalized["global_avoid"]:
            raise WorkflowError(
                "INSTRUCTION_AVOID_REQUIRED", "至少填写一条全局禁用规则。"
            )
        return normalized

    def create_instruction_version(
        self,
        project_id: str,
        *,
        name: str,
        content: dict[str, Any],
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.project(project_id)
        actor_id, actor_role = self._actor(actor)
        if actor_role not in {"content_editor", "producer", "system_admin"}:
            raise WorkflowError(
                "INSTRUCTION_ROLE_REQUIRED",
                "只有内容编辑、制片人或系统管理员可以创建指令版本。",
                status=403,
            )
        name = str(name).strip()[:120]
        if not name:
            raise WorkflowError("INSTRUCTION_NAME_REQUIRED", "请填写指令版本名称。")
        normalized = self._normalized_instruction_content(content)
        now = utc_now()
        instruction_id = _new_id("instruction")
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                version = int(
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(version), 0) + 1 AS version
                        FROM instruction_versions WHERE project_id = ?
                        """,
                        (project_id,),
                    ).fetchone()["version"]
                )
                connection.execute(
                    """
                    INSERT INTO instruction_versions(
                        id, project_id, version, name, content_json, status,
                        created_by, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'draft', ?, ?)
                    """,
                    (
                        instruction_id,
                        project_id,
                        version,
                        name,
                        _json(normalized),
                        actor_id,
                        now,
                    ),
                )
                result = self._instruction_dict(
                    connection.execute(
                        "SELECT * FROM instruction_versions WHERE id = ?",
                        (instruction_id,),
                    ).fetchone()
                )
                self._audit(
                    connection,
                    actor=actor,
                    action="instruction.created",
                    target_type="instruction_version",
                    target_id=instruction_id,
                    after=result,
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def publish_instruction_version(
        self,
        instruction_id: str,
        *,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _, actor_role = self._actor(actor)
        if actor_role not in {"producer", "system_admin"}:
            raise WorkflowError(
                "INSTRUCTION_PUBLISH_ROLE_REQUIRED",
                "只有制片人或系统管理员可以发布全局指令。",
                status=403,
            )
        now = utc_now()
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM instruction_versions WHERE id = ?",
                    (instruction_id,),
                ).fetchone()
                if not row:
                    raise WorkflowError(
                        "INSTRUCTION_NOT_FOUND", "指令版本不存在。", status=404
                    )
                if row["status"] == "published":
                    connection.execute("COMMIT")
                    return self._instruction_dict(row)
                if row["status"] != "draft":
                    raise WorkflowError(
                        "INVALID_INSTRUCTION_STATE",
                        "只有草稿指令可以发布。",
                        status=409,
                    )
                connection.execute(
                    """
                    UPDATE instruction_versions
                    SET status = 'retired'
                    WHERE project_id = ? AND status = 'published'
                    """,
                    (row["project_id"],),
                )
                connection.execute(
                    """
                    UPDATE instruction_versions
                    SET status = 'published', published_at = ?
                    WHERE id = ?
                    """,
                    (now, instruction_id),
                )
                result = self._instruction_dict(
                    connection.execute(
                        "SELECT * FROM instruction_versions WHERE id = ?",
                        (instruction_id,),
                    ).fetchone()
                )
                self._audit(
                    connection,
                    actor=actor,
                    action="instruction.published",
                    target_type="instruction_version",
                    target_id=instruction_id,
                    before=self._instruction_dict(row),
                    after=result,
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def retire_instruction_draft(
        self,
        instruction_id: str,
        *,
        reason: str,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _, actor_role = self._actor(actor)
        if actor_role not in {"content_editor", "producer", "system_admin"}:
            raise WorkflowError(
                "INSTRUCTION_ROLE_REQUIRED",
                "只有内容编辑、制片人或系统管理员可以作废指令草稿。",
                status=403,
            )
        reason = str(reason or "").strip()[:500]
        if not reason:
            raise WorkflowError("REASON_REQUIRED", "作废指令草稿时必须填写原因。")
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM instruction_versions WHERE id = ?",
                    (instruction_id,),
                ).fetchone()
                if not row:
                    raise WorkflowError(
                        "INSTRUCTION_NOT_FOUND", "指令版本不存在。", status=404
                    )
                if row["status"] == "retired":
                    connection.execute("COMMIT")
                    return self._instruction_dict(row)
                if row["status"] != "draft":
                    raise WorkflowError(
                        "PUBLISHED_INSTRUCTION_LOCKED",
                        "已发布指令不能直接作废；请发布替代版本。",
                        status=409,
                    )
                connection.execute(
                    "UPDATE instruction_versions SET status = 'retired' WHERE id = ?",
                    (instruction_id,),
                )
                result = self._instruction_dict(
                    connection.execute(
                        "SELECT * FROM instruction_versions WHERE id = ?",
                        (instruction_id,),
                    ).fetchone()
                )
                result["retirement_reason"] = reason
                self._audit(
                    connection,
                    actor=actor,
                    action="instruction.retired",
                    target_type="instruction_version",
                    target_id=instruction_id,
                    before=self._instruction_dict(row),
                    after=result,
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def style_pack_versions(
        self,
        project_id: str = DEFAULT_PROJECT_ID,
        *,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        self.project(project_id)
        if status and status not in STYLE_PACK_STATUSES:
            raise WorkflowError("INVALID_STYLE_STATUS", "不支持的风格状态。")
        where = ["project_id = ?"]
        params: list[Any] = [project_id]
        if status:
            where.append("status = ?")
            params.append(status)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM style_pack_versions
                WHERE {' AND '.join(where)}
                ORDER BY style_id, version DESC
                """,
                params,
            ).fetchall()
        return [self._style_pack_dict(row) for row in rows]

    def published_style_pack(
        self,
        project_id: str,
        style_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM style_pack_versions
                WHERE project_id = ? AND style_id = ? AND status = 'published'
                ORDER BY version DESC LIMIT 1
                """,
                (project_id, str(style_id).strip()[:80]),
            ).fetchone()
        if not row:
            raise WorkflowError(
                "PUBLISHED_STYLE_REQUIRED",
                "所选风格没有已发布版本。",
                status=409,
            )
        return self._style_pack_dict(row)

    @staticmethod
    def _normalize_string_list(value: Any, field: str, limit: int = 30) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise WorkflowError("INVALID_STYLE_FIELD", f"{field} 必须是数组。")
        return [str(item).strip()[:200] for item in value[:limit] if str(item).strip()]

    def create_style_pack_version(
        self,
        project_id: str,
        *,
        style_id: str,
        name: str,
        short_name: str = "",
        description: str = "",
        prompt_fragment: str,
        palette: list[str] | None = None,
        settings: dict[str, Any] | None = None,
        applicable_topics: list[str] | None = None,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.project(project_id)
        actor_id, actor_role = self._actor(actor)
        if actor_role not in {"art_director", "producer", "system_admin"}:
            raise WorkflowError(
                "STYLE_ROLE_REQUIRED",
                "只有美术指导、制片人或系统管理员可以创建风格版本。",
                status=403,
            )
        style_id = str(style_id).strip().lower()[:80]
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,79}", style_id):
            raise WorkflowError(
                "INVALID_STYLE_ID", "风格 ID 仅支持小写字母、数字和连字符。"
            )
        name = str(name).strip()[:120]
        prompt_fragment = str(prompt_fragment).strip()[:4000]
        if not name or not prompt_fragment:
            raise WorkflowError(
                "STYLE_CORE_REQUIRED", "风格名称和 Prompt 片段不能为空。"
            )
        palette_items = self._normalize_string_list(palette, "palette", 12)
        for color in palette_items:
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                raise WorkflowError("INVALID_PALETTE", f"无效色值：{color}")
        if settings is None:
            settings = {}
        if not isinstance(settings, dict):
            raise WorkflowError("INVALID_STYLE_SETTINGS", "风格设置必须是对象。")
        safe_settings = {
            key: str(settings.get(key) or "").strip()[:100]
            for key in ("background", "foreground", "accent", "paper")
        }
        topics = self._normalize_string_list(
            applicable_topics or ["通用"], "applicable_topics"
        )
        now = utc_now()
        version_id = _new_id("stylev")
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                version = int(
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(version), 0) + 1 AS version
                        FROM style_pack_versions
                        WHERE project_id = ? AND style_id = ?
                        """,
                        (project_id, style_id),
                    ).fetchone()["version"]
                )
                connection.execute(
                    """
                    INSERT INTO style_pack_versions(
                        id, project_id, style_id, version, name, short_name,
                        description, prompt_fragment, palette_json,
                        settings_json, applicable_topics_json, status,
                        created_by, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                    """,
                    (
                        version_id,
                        project_id,
                        style_id,
                        version,
                        name,
                        str(short_name).strip()[:80],
                        str(description).strip()[:1000],
                        prompt_fragment,
                        _json(palette_items),
                        _json(safe_settings),
                        _json(topics),
                        actor_id,
                        now,
                    ),
                )
                result = self._style_pack_dict(
                    connection.execute(
                        "SELECT * FROM style_pack_versions WHERE id = ?",
                        (version_id,),
                    ).fetchone()
                )
                self._audit(
                    connection,
                    actor=actor,
                    action="style_pack.created",
                    target_type="style_pack_version",
                    target_id=version_id,
                    after=result,
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def publish_style_pack_version(
        self,
        version_id: str,
        *,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _, actor_role = self._actor(actor)
        if actor_role not in {"art_director", "producer", "system_admin"}:
            raise WorkflowError(
                "STYLE_PUBLISH_ROLE_REQUIRED",
                "只有美术指导、制片人或系统管理员可以发布风格版本。",
                status=403,
            )
        now = utc_now()
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM style_pack_versions WHERE id = ?",
                    (version_id,),
                ).fetchone()
                if not row:
                    raise WorkflowError(
                        "STYLE_VERSION_NOT_FOUND", "风格版本不存在。", status=404
                    )
                if row["status"] == "published":
                    connection.execute("COMMIT")
                    return self._style_pack_dict(row)
                if row["status"] != "draft":
                    raise WorkflowError(
                        "INVALID_STYLE_STATE", "只有草稿风格可以发布。", status=409
                    )
                connection.execute(
                    """
                    UPDATE style_pack_versions SET status = 'retired'
                    WHERE project_id = ? AND style_id = ? AND status = 'published'
                    """,
                    (row["project_id"], row["style_id"]),
                )
                connection.execute(
                    """
                    UPDATE style_pack_versions
                    SET status = 'published', published_at = ? WHERE id = ?
                    """,
                    (now, version_id),
                )
                result = self._style_pack_dict(
                    connection.execute(
                        "SELECT * FROM style_pack_versions WHERE id = ?",
                        (version_id,),
                    ).fetchone()
                )
                self._audit(
                    connection,
                    actor=actor,
                    action="style_pack.published",
                    target_type="style_pack_version",
                    target_id=version_id,
                    before=self._style_pack_dict(row),
                    after=result,
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def budget_policy(
        self,
        project_id: str = DEFAULT_PROJECT_ID,
    ) -> dict[str, Any]:
        self.project(project_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM budget_policies WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            available = (
                self._available_budget_locked(connection, project_id) if row else None
            )
        if not row:
            raise WorkflowError(
                "BUDGET_POLICY_REQUIRED",
                "项目缺少预算策略。",
                status=409,
            )
        item = dict(row)
        item["reserved"] = available["reserved"]
        item["remaining"] = available["remaining"]
        item["soft_limit"] = round(item["hard_limit"] * item["soft_ratio"], 6)
        item["soft_warning"] = (
            item["spent"] + item["reserved"] >= item["soft_limit"]
        )
        return item

    def set_budget_policy(
        self,
        project_id: str,
        *,
        hard_limit: float,
        soft_ratio: float = 0.7,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        hard_limit = round(float(hard_limit), 6)
        soft_ratio = float(soft_ratio)
        if hard_limit < 0 or hard_limit > 1_000_000:
            raise WorkflowError("INVALID_BUDGET", "预算上限必须在 0 到 1,000,000 之间。")
        if soft_ratio < 0.1 or soft_ratio > 1:
            raise WorkflowError("INVALID_SOFT_RATIO", "软提醒比例必须在 0.1 到 1 之间。")
        self.project(project_id)
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                before = connection.execute(
                    "SELECT * FROM budget_policies WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO budget_policies(
                        project_id, currency, hard_limit, soft_ratio, spent,
                        updated_at
                    ) VALUES (?, 'USD', ?, ?, 0, ?)
                    ON CONFLICT(project_id) DO UPDATE SET
                        hard_limit = excluded.hard_limit,
                        soft_ratio = excluded.soft_ratio,
                        updated_at = excluded.updated_at
                    """,
                    (project_id, hard_limit, soft_ratio, now),
                )
                result = dict(
                    connection.execute(
                        "SELECT * FROM budget_policies WHERE project_id = ?",
                        (project_id,),
                    ).fetchone()
                )
                self._audit(
                    connection,
                    actor=actor,
                    action="budget.updated",
                    target_type="budget_policy",
                    target_id=project_id,
                    before=dict(before) if before else None,
                    after=result,
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.budget_policy(project_id)

    def _approved_direction_rows(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        poem_ids: Iterable[str],
        direction_ids: Iterable[str] | None = None,
    ) -> list[sqlite3.Row]:
        poem_ids = list(dict.fromkeys(str(item) for item in poem_ids))[:300]
        if not poem_ids:
            raise WorkflowError("EMPTY_SELECTION", "请至少选择一首待排产诗词。")
        poem_placeholders = ",".join("?" for _ in poem_ids)
        where = [
            "p.project_id = ?",
            f"p.id IN ({poem_placeholders})",
            "d.is_current = 1",
            "d.status = 'approved'",
        ]
        params: list[Any] = [project_id, *poem_ids]
        selected_directions = list(
            dict.fromkeys(str(item) for item in (direction_ids or []))
        )
        if selected_directions:
            direction_placeholders = ",".join("?" for _ in selected_directions)
            where.append(f"d.id IN ({direction_placeholders})")
            params.extend(selected_directions)
        rows = connection.execute(
            f"""
            SELECT d.id AS direction_id, d.poem_id, d.requirement_id,
                   d.version AS direction_version, d.type AS direction_type,
                   d.content_json AS direction_content_json,
                   p.title AS poem_title, p.author, p.dynasty,
                   p.lines_json, p.theme, p.mood, p.status AS poem_status,
                   cv.id AS content_version_id,
                   cv.version AS content_version,
                   r.instruction_id, r.version AS requirement_version,
                   r.content_json AS requirement_content_json,
                   iv.version AS instruction_version,
                   iv.name AS instruction_name,
                   iv.content_json AS instruction_content_json
            FROM directions d
            JOIN poems p ON p.id = d.poem_id
            JOIN requirements r ON r.id = d.requirement_id
            JOIN instruction_versions iv ON iv.id = r.instruction_id
            JOIN content_versions cv ON cv.poem_id = p.id
              AND cv.status = 'approved'
              AND cv.version = (
                  SELECT MAX(cv2.version) FROM content_versions cv2
                  WHERE cv2.poem_id = p.id AND cv2.status = 'approved'
              )
            WHERE {' AND '.join(where)}
            ORDER BY p.title, d.type
            """,
            params,
        ).fetchall()
        covered = {row["poem_id"] for row in rows}
        missing = [poem_id for poem_id in poem_ids if poem_id not in covered]
        if missing:
            raise WorkflowError(
                "APPROVED_DIRECTION_REQUIRED",
                f"以下诗词没有已批准方向：{', '.join(missing[:8])}",
                status=409,
            )
        invalid_status = sorted(
            {
                row["poem_id"]
                for row in rows
                if row["poem_status"] != "ready_for_production"
            }
        )
        if invalid_status:
            raise WorkflowError(
                "POEM_NOT_READY",
                f"以下诗词不在待排产阶段：{', '.join(invalid_status[:8])}",
                status=409,
            )
        return rows

    def estimate_batch(
        self,
        project_id: str,
        poem_ids: Iterable[str],
        *,
        direction_ids: Iterable[str] | None = None,
        style_id: str,
        aspect_ratio: str,
        count_per_direction: int,
        provider: str,
        model: str,
        unit_cost: float,
    ) -> dict[str, Any]:
        self.project(project_id)
        style_id = str(style_id).strip()[:80]
        provider = str(provider).strip()[:40]
        model = str(model).strip()[:100]
        if not style_id:
            raise WorkflowError("STYLE_REQUIRED", "请选择生产风格。")
        if aspect_ratio not in {"portrait", "square", "landscape"}:
            raise WorkflowError("INVALID_ASPECT_RATIO", "不支持的画面比例。")
        try:
            count_per_direction = int(count_per_direction)
            unit_cost = round(float(unit_cost), 6)
        except (TypeError, ValueError) as exc:
            raise WorkflowError("INVALID_BATCH_SETTINGS", "批次参数格式无效。") from exc
        if count_per_direction < 1 or count_per_direction > 4:
            raise WorkflowError("INVALID_COUNT", "每个方向可生成 1–4 张。")
        if unit_cost < 0 or unit_cost > 1000:
            raise WorkflowError("INVALID_UNIT_COST", "单张预估成本无效。")
        style_version = self.published_style_pack(project_id, style_id)
        with self._connect() as connection:
            rows = self._approved_direction_rows(
                connection, project_id, poem_ids, direction_ids
            )
        task_count = len(rows) * count_per_direction
        if task_count > 1000:
            raise WorkflowError("BATCH_TOO_LARGE", "单个批次最多包含 1000 个任务。")
        estimated_cost = round(task_count * unit_cost, 6)
        budget = self.budget_policy(project_id)
        warnings = []
        projected = budget["spent"] + estimated_cost
        if projected >= budget["soft_limit"]:
            warnings.append("批次执行后预计达到预算软提醒线。")
        if estimated_cost > budget["remaining"]:
            warnings.append("预计成本超过项目剩余预算，启动时将被硬停止。")
        selected_poem_ids = sorted({row["poem_id"] for row in rows})
        return {
            "project_id": project_id,
            "poem_ids": selected_poem_ids,
            "direction_ids": [row["direction_id"] for row in rows],
            "poem_count": len(selected_poem_ids),
            "direction_count": len(rows),
            "task_count": task_count,
            "count_per_direction": count_per_direction,
            "style_id": style_id,
            "style_version_id": style_version["id"],
            "style_version": style_version["version"],
            "style_name": style_version["name"],
            "aspect_ratio": aspect_ratio,
            "provider": provider,
            "model": model,
            "unit_cost": unit_cost,
            "estimated_cost": estimated_cost,
            "currency": budget["currency"],
            "budget": budget,
            "can_start": estimated_cost <= budget["remaining"],
            "warnings": warnings,
        }

    def create_batch(
        self,
        project_id: str,
        poem_ids: Iterable[str],
        *,
        direction_ids: Iterable[str] | None = None,
        name: str = "",
        style_id: str,
        aspect_ratio: str,
        count_per_direction: int,
        provider: str,
        model: str,
        unit_cost: float,
        priority: int = 50,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        estimate = self.estimate_batch(
            project_id,
            poem_ids,
            direction_ids=direction_ids,
            style_id=style_id,
            aspect_ratio=aspect_ratio,
            count_per_direction=count_per_direction,
            provider=provider,
            model=model,
            unit_cost=unit_cost,
        )
        try:
            priority = max(1, min(int(priority), 100))
        except (TypeError, ValueError) as exc:
            raise WorkflowError("INVALID_PRIORITY", "批次优先级无效。") from exc
        actor_id, _ = self._actor(actor)
        batch_id = _new_id("batch")
        now = utc_now()
        batch_name = str(name).strip()[:100] or (
            f"{len(estimate['poem_ids'])} 首诗 · {estimate['task_count']} 张生产批次"
        )
        style_version = self.published_style_pack(project_id, style_id)
        style_snapshot = {
            "id": style_id,
            "version_id": style_version["id"],
            "version": style_version["version"],
            "name": style_version["name"],
            "short_name": style_version["short_name"],
            "description": style_version["description"],
            "prompt_fragment": style_version["prompt_fragment"],
            "palette": style_version["palette"],
            **style_version["settings"],
        }
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = self._approved_direction_rows(
                    connection,
                    project_id,
                    estimate["poem_ids"],
                    estimate["direction_ids"],
                )
                connection.execute(
                    """
                    INSERT INTO generation_batches(
                        id, project_id, name, provider, model, style_id,
                        style_version_id, aspect_ratio, count_per_direction,
                        priority, status,
                        task_count, estimated_cost, actual_cost, currency,
                        budget_snapshot_json, settings_json, created_by,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, 0, ?,
                              ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        project_id,
                        batch_name,
                        provider,
                        model,
                        style_id,
                        estimate["style_version_id"],
                        aspect_ratio,
                        estimate["count_per_direction"],
                        priority,
                        estimate["task_count"],
                        estimate["estimated_cost"],
                        estimate["currency"],
                        _json(estimate["budget"]),
                        _json(
                            {
                                "unit_cost": estimate["unit_cost"],
                                "direction_ids": estimate["direction_ids"],
                                "poem_ids": estimate["poem_ids"],
                                "style_version_id": estimate["style_version_id"],
                                "style_version": estimate["style_version"],
                            }
                        ),
                        actor_id,
                        now,
                        now,
                    ),
                )
                for row in rows:
                    direction_content = _decode(
                        row["direction_content_json"], {}
                    )
                    requirement_content = _decode(
                        row["requirement_content_json"], {}
                    )
                    for sample_index in range(1, estimate["count_per_direction"] + 1):
                        idempotency_key = hashlib.sha256(
                            (
                                f"{batch_id}|{row['direction_id']}|{style_id}|"
                                f"{aspect_ratio}|{sample_index}"
                            ).encode("utf-8")
                        ).hexdigest()
                        task_id = _new_id("task")
                        prompt_payload = {
                            "poem": {
                                "id": row["poem_id"],
                                "title": row["poem_title"],
                                "author": row["author"],
                                "dynasty": row["dynasty"],
                                "lines": _decode(row["lines_json"], []),
                                "content_version_id": row["content_version_id"],
                                "content_version": row["content_version"],
                                "theme": row["theme"],
                                "mood": row["mood"],
                            },
                            "instruction": {
                                "id": row["instruction_id"],
                                "version": row["instruction_version"],
                                "name": row["instruction_name"],
                                "content": _decode(
                                    row["instruction_content_json"], {}
                                ),
                            },
                            "requirement": {
                                "id": row["requirement_id"],
                                "version": row["requirement_version"],
                                "instruction_id": row["instruction_id"],
                                "content": requirement_content,
                            },
                            "direction": {
                                "id": row["direction_id"],
                                "version": row["direction_version"],
                                "type": row["direction_type"],
                                "content": direction_content,
                            },
                            "style_id": style_id,
                            "style": style_snapshot,
                            "aspect_ratio": aspect_ratio,
                            "sample_index": sample_index,
                        }
                        try:
                            prompt_payload["compiled"] = compile_generation_prompt(
                                prompt_payload, provider
                            )
                        except PromptCompileError as exc:
                            raise WorkflowError(
                                exc.code,
                                str(exc),
                                status=409,
                            ) from exc
                        connection.execute(
                            """
                            INSERT INTO generation_tasks(
                                id, batch_id, poem_id, direction_id,
                                sample_index, status, priority, idempotency_key,
                                prompt_json, max_attempts, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, 3, ?, ?)
                            """,
                            (
                                task_id,
                                batch_id,
                                row["poem_id"],
                                row["direction_id"],
                                sample_index,
                                priority,
                                idempotency_key,
                                _json(prompt_payload),
                                now,
                                now,
                            ),
                        )
                batch = self._batch_dict(
                    connection.execute(
                        "SELECT * FROM generation_batches WHERE id = ?",
                        (batch_id,),
                    ).fetchone()
                )
                self._audit(
                    connection,
                    actor=actor,
                    action="batch.created",
                    target_type="generation_batch",
                    target_id=batch_id,
                    after=batch,
                )
                connection.execute("COMMIT")
                return batch
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def batches(
        self,
        project_id: str = DEFAULT_PROJECT_ID,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT b.*,
                       SUM(CASE WHEN t.status = 'succeeded' THEN 1 ELSE 0 END)
                           AS succeeded_count,
                       SUM(CASE WHEN t.status = 'failed' THEN 1 ELSE 0 END)
                           AS failed_count,
                       SUM(CASE WHEN t.status = 'blocked' THEN 1 ELSE 0 END)
                           AS blocked_count,
                       SUM(CASE WHEN t.status IN (
                           'pending', 'ready', 'running', 'retry_waiting'
                       ) THEN 1 ELSE 0 END) AS active_count
                FROM generation_batches b
                LEFT JOIN generation_tasks t ON t.batch_id = b.id
                WHERE b.project_id = ?
                GROUP BY b.id
                ORDER BY b.created_at DESC
                LIMIT ?
                """,
                (project_id, max(1, min(int(limit), 500))),
            ).fetchall()
        items = []
        for row in rows:
            item = self._batch_dict(row)
            done = (item.get("succeeded_count") or 0) + (
                item.get("failed_count") or 0
            ) + (item.get("blocked_count") or 0)
            item["progress"] = round(done / item["task_count"] * 100) if item["task_count"] else 0
            items.append(item)
        return items

    def batch(self, batch_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM generation_batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
        if not row:
            raise WorkflowError("BATCH_NOT_FOUND", "生产批次不存在。", status=404)
        return self._batch_dict(row)

    def tasks(
        self,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
        batch_id: str | None = None,
        status: str | None = None,
        poem_id: str | None = None,
        error_code: str | None = None,
        q: str = "",
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return self.task_page(
            project_id=project_id,
            batch_id=batch_id,
            status=status,
            poem_id=poem_id,
            error_code=error_code,
            q=q,
            limit=limit,
            offset=offset,
        )["items"]

    def task_page(
        self,
        *,
        project_id: str = DEFAULT_PROJECT_ID,
        batch_id: str | None = None,
        status: str | None = None,
        poem_id: str | None = None,
        error_code: str | None = None,
        q: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        if status and status not in TASK_STATUSES:
            raise WorkflowError("INVALID_TASK_STATUS", "不支持的任务状态。")
        limit = max(1, min(int(limit), 1000))
        offset = int(offset)
        if offset < 0:
            raise WorkflowError("INVALID_PAGINATION", "分页偏移量不能为负数。")
        where = ["b.project_id = ?"]
        params: list[Any] = [project_id]
        if batch_id:
            where.append("t.batch_id = ?")
            params.append(batch_id)
        if status:
            where.append("t.status = ?")
            params.append(status)
        if poem_id:
            where.append("t.poem_id = ?")
            params.append(poem_id)
        if error_code:
            where.append("t.last_error_code = ?")
            params.append(str(error_code).strip()[:100])
        query = str(q or "").strip()
        if query:
            like = f"%{query}%"
            where.append(
                "(p.title LIKE ? OR p.author LIKE ? OR t.poem_id LIKE ? "
                "OR t.last_error_code LIKE ? OR t.last_error_message LIKE ?)"
            )
            params.extend([like, like, like, like, like])
        where_sql = " AND ".join(where)
        with self._connect() as connection:
            total = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) AS count
                    FROM generation_tasks t
                    JOIN generation_batches b ON b.id = t.batch_id
                    JOIN poems p ON p.id = t.poem_id
                    WHERE {where_sql}
                    """,
                    params,
                ).fetchone()["count"]
            )
            rows = connection.execute(
                f"""
                SELECT t.*, p.title AS poem_title, p.author,
                       d.type AS direction_type, b.name AS batch_name,
                       b.provider, b.model, b.style_id, b.style_version_id,
                       b.aspect_ratio,
                       b.status AS batch_status
                FROM generation_tasks t
                JOIN generation_batches b ON b.id = t.batch_id
                JOIN poems p ON p.id = t.poem_id
                JOIN directions d ON d.id = t.direction_id
                WHERE {where_sql}
                ORDER BY t.priority DESC, t.created_at
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        items = [self._task_dict(row) for row in rows]
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_previous": offset > 0,
            "has_next": offset + len(items) < total,
            "filters": {
                "batch_id": batch_id or "",
                "status": status or "",
                "poem_id": poem_id or "",
                "error_code": error_code or "",
                "q": query,
            },
        }

    def attempts(
        self,
        task_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM generation_attempts
                WHERE task_id = ?
                ORDER BY attempt_number
                """,
                (task_id,),
            ).fetchall()
        return [self._attempt_dict(row) for row in rows]

    def register_production_image(
        self,
        image: dict[str, Any],
        task: dict[str, Any],
        inspection: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a generated image and its immutable QC result atomically."""

        image_id = str(image.get("id") or "")
        task_id = str(task.get("id") or "")
        if not re.fullmatch(r"[a-f0-9]{32}", image_id):
            raise WorkflowError("INVALID_IMAGE_ID", "候选图片 ID 无效。")
        now = utc_now()
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT id FROM production_images WHERE id = ? OR task_id = ?",
                    (image_id, task_id),
                ).fetchone()
                if existing:
                    connection.execute("COMMIT")
                    return self.production_image(existing["id"])
                task_row = connection.execute(
                    """
                    SELECT t.*, b.project_id, b.provider, b.model, b.style_id,
                           b.style_version_id, b.aspect_ratio
                    FROM generation_tasks t
                    JOIN generation_batches b ON b.id = t.batch_id
                    WHERE t.id = ?
                    """,
                    (task_id,),
                ).fetchone()
                if not task_row:
                    raise WorkflowError("TASK_NOT_FOUND", "生成任务不存在。", status=404)
                checksum = str(inspection.get("checksum") or "")
                perceptual_hash = str(inspection.get("perceptual_hash") or "")
                duplicate_of = None
                candidates = connection.execute(
                    """
                    SELECT id, checksum, perceptual_hash
                    FROM production_images
                    WHERE project_id = ? AND poem_id = ?
                    ORDER BY created_at DESC
                    LIMIT 200
                    """,
                    (task_row["project_id"], task_row["poem_id"]),
                ).fetchall()
                for candidate in candidates:
                    if checksum and checksum == candidate["checksum"]:
                        duplicate_of = candidate["id"]
                        break
                    if (
                        perceptual_hash
                        and candidate["perceptual_hash"]
                        and hamming_distance(
                            perceptual_hash, candidate["perceptual_hash"]
                        ) <= 4
                    ):
                        duplicate_of = candidate["id"]
                        break
                hard_failures = list(inspection.get("hard_failures") or [])
                warnings = list(inspection.get("warnings") or [])
                qc_status = str(inspection.get("status") or "manual_required")
                if duplicate_of:
                    hard_failures.append("near_duplicate")
                    qc_status = "hard_fail"
                if qc_status == "hard_fail" or hard_failures:
                    image_status = "qc_blocked"
                elif qc_status == "manual_required":
                    image_status = "needs_manual_qc"
                else:
                    image_status = "review_ready"
                parent_image_id = image.get("parent_image_id")
                parent = (
                    connection.execute(
                        "SELECT generation FROM production_images WHERE id = ?",
                        (parent_image_id,),
                    ).fetchone()
                    if parent_image_id
                    else None
                )
                generation = (int(parent["generation"]) + 1) if parent else 1
                compiled_prompt = (
                    _decode(task_row["prompt_json"], {}).get("compiled") or {}
                )
                connection.execute(
                    """
                    INSERT INTO production_images(
                        id, project_id, batch_id, task_id, poem_id,
                        direction_id, style_id, style_version_id, provider,
                        model, url,
                        file_path, mime_type, checksum, perceptual_hash,
                        file_size, width, height, aspect_ratio, prompt,
                        prompt_hash, prompt_template_version,
                        prompt_segments_json,
                        generation, parent_image_id, status, created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        image_id,
                        task_row["project_id"],
                        task_row["batch_id"],
                        task_id,
                        task_row["poem_id"],
                        task_row["direction_id"],
                        task_row["style_id"],
                        task_row["style_version_id"],
                        task_row["provider"],
                        task_row["model"],
                        str(image.get("url") or ""),
                        str(inspection.get("file_path") or ""),
                        str(inspection.get("mime_type") or ""),
                        checksum,
                        perceptual_hash,
                        int(inspection.get("file_size") or 0),
                        int(inspection.get("width") or 0),
                        int(inspection.get("height") or 0),
                        task_row["aspect_ratio"],
                        str(image.get("prompt") or compiled_prompt.get("text") or ""),
                        str(compiled_prompt.get("hash") or ""),
                        str(compiled_prompt.get("template_version") or ""),
                        _json(compiled_prompt.get("segments") or {}),
                        generation,
                        parent_image_id,
                        image_status,
                        str(image.get("created_at") or now),
                        now,
                    ),
                )
                qc_id = _new_id("qc")
                connection.execute(
                    """
                    INSERT INTO qc_results(
                        id, image_id, version, status, score,
                        hard_failures_json, warnings_json, checks_json,
                        coverage_json, duplicate_of, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        qc_id,
                        image_id,
                        str(inspection.get("version") or "unknown"),
                        qc_status,
                        max(0, min(float(inspection.get("score") or 0), 100)),
                        _json(list(dict.fromkeys(hard_failures))),
                        _json(list(dict.fromkeys(warnings))),
                        _json(inspection.get("checks") or {}),
                        _json(inspection.get("coverage") or []),
                        duplicate_of,
                        now,
                    ),
                )
                self._audit(
                    connection,
                    actor={"id": "qc-engine", "role": "system"},
                    action="image.qc_completed",
                    target_type="production_image",
                    target_id=image_id,
                    after={
                        "status": image_status,
                        "qc_result_id": qc_id,
                        "qc_status": qc_status,
                        "duplicate_of": duplicate_of,
                    },
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.production_image(image_id)

    def _production_image_locked(
        self,
        connection: sqlite3.Connection,
        image_id: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT i.*, p.title AS poem_title, p.author, p.dynasty,
                   d.type AS direction_type, d.content_json AS direction_content_json,
                   b.name AS batch_name
            FROM production_images i
            JOIN poems p ON p.id = i.poem_id
            JOIN directions d ON d.id = i.direction_id
            JOIN generation_batches b ON b.id = i.batch_id
            WHERE i.id = ?
            """,
            (image_id,),
        ).fetchone()
        if not row:
            return None
        item = self._production_image_dict(row)
        item["direction"] = _decode(item.pop("direction_content_json", None), {})
        qc = connection.execute(
            "SELECT * FROM qc_results WHERE image_id = ? ORDER BY created_at DESC LIMIT 1",
            (image_id,),
        ).fetchone()
        item["qc"] = self._qc_dict(qc) if qc else None
        decision = connection.execute(
            """
            SELECT * FROM review_decisions
            WHERE image_id = ? ORDER BY created_at DESC LIMIT 1
            """,
            (image_id,),
        ).fetchone()
        if decision:
            current_decision = dict(decision)
            current_decision["reason_tags"] = _decode(
                current_decision.pop("reason_tags_json", None), []
            )
            item["current_decision"] = current_decision
        else:
            item["current_decision"] = None
        item["override_count"] = connection.execute(
            "SELECT COUNT(*) AS count FROM qc_overrides WHERE image_id = ?",
            (image_id,),
        ).fetchone()["count"]
        item["child_count"] = connection.execute(
            "SELECT COUNT(*) AS count FROM production_images WHERE parent_image_id = ?",
            (image_id,),
        ).fetchone()["count"]
        item["rework_count"] = connection.execute(
            "SELECT COUNT(*) AS count FROM rework_orders WHERE source_image_id = ?",
            (image_id,),
        ).fetchone()["count"]
        approvals = {}
        for reviewer_type in ("content", "art"):
            approval = connection.execute(
                """
                SELECT * FROM final_approvals
                WHERE image_id = ? AND reviewer_type = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (image_id, reviewer_type),
            ).fetchone()
            approvals[reviewer_type] = dict(approval) if approval else None
        item["final_approvals"] = approvals
        final_asset = connection.execute(
            "SELECT * FROM final_assets WHERE image_id = ? ORDER BY version DESC LIMIT 1",
            (image_id,),
        ).fetchone()
        if final_asset:
            asset = dict(final_asset)
            asset["spec"] = _decode(asset.pop("spec_json", None), {})
            item["final_asset"] = asset
        else:
            item["final_asset"] = None
        return item

    def production_image(self, image_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            item = self._production_image_locked(connection, image_id)
        if not item:
            raise WorkflowError("IMAGE_NOT_FOUND", "生产候选不存在。", status=404)
        return item

    def review_queue(
        self,
        project_id: str = DEFAULT_PROJECT_ID,
        *,
        include_blocked: bool = False,
    ) -> dict[str, Any]:
        statuses = ["review_ready", "selected", "final_candidate"]
        if include_blocked:
            statuses.extend(["qc_blocked", "needs_manual_qc"])
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as connection:
            ids = [
                row["id"]
                for row in connection.execute(
                    f"""
                    SELECT id FROM production_images
                    WHERE project_id = ? AND status IN ({placeholders})
                    ORDER BY poem_id, created_at DESC
                    LIMIT 1000
                    """,
                    [project_id, *statuses],
                ).fetchall()
            ]
            images = [
                item
                for image_id in ids
                if (item := self._production_image_locked(connection, image_id))
            ]
        grouped: dict[str, dict[str, Any]] = {}
        for image in images:
            group = grouped.setdefault(
                image["poem_id"],
                {
                    "poem_id": image["poem_id"],
                    "poem_title": image["poem_title"],
                    "author": image["author"],
                    "candidates": [],
                },
            )
            group["candidates"].append(image)
        return {
            "groups": list(grouped.values()),
            "summary": {
                "poem_count": len(grouped),
                "candidate_count": len(images),
                "review_ready": sum(
                    image["status"] == "review_ready" for image in images
                ),
                "selected": sum(image["status"] == "selected" for image in images),
                "final_candidate": sum(
                    image["status"] == "final_candidate" for image in images
                ),
                "qc_blocked": sum(
                    image["status"] in {"qc_blocked", "needs_manual_qc"}
                    for image in images
                ),
            },
        }

    def decide_image(
        self,
        image_id: str,
        decision: str,
        *,
        reason_tags: Iterable[str] = (),
        note: str = "",
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        decision = str(decision)
        if decision not in REVIEW_DECISIONS:
            raise WorkflowError("INVALID_REVIEW_DECISION", "不支持的审片结论。")
        tags = list(dict.fromkeys(str(tag).strip()[:40] for tag in reason_tags if str(tag).strip()))[:8]
        if decision != "candidate" and not tags:
            raise WorkflowError("REVIEW_REASON_REQUIRED", "请选择至少一个审片理由标签。")
        next_status = "review_ready" if decision == "candidate" else decision
        actor_id, actor_role = self._actor(actor)
        now = utc_now()
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                image = connection.execute(
                    "SELECT * FROM production_images WHERE id = ?", (image_id,)
                ).fetchone()
                if not image:
                    raise WorkflowError("IMAGE_NOT_FOUND", "生产候选不存在。", status=404)
                if image["status"] in {"qc_blocked", "needs_manual_qc"}:
                    raise WorkflowError(
                        "QC_OVERRIDE_REQUIRED",
                        "该候选被 QC 隔离，必须先记录人工覆盖。",
                        status=409,
                    )
                connection.execute(
                    "UPDATE production_images SET status = ?, updated_at = ? WHERE id = ?",
                    (next_status, now, image_id),
                )
                decision_id = _new_id("review")
                connection.execute(
                    """
                    INSERT INTO review_decisions(
                        id, image_id, decision, reason_tags_json, note,
                        actor_id, actor_role, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision_id,
                        image_id,
                        decision,
                        _json(tags),
                        str(note).strip()[:1000],
                        actor_id,
                        actor_role,
                        now,
                    ),
                )
                poem_status = "final_review" if decision == "final_candidate" else "candidate_review"
                connection.execute(
                    "UPDATE poems SET status = ?, updated_at = ? WHERE id = ?",
                    (poem_status, now, image["poem_id"]),
                )
                self._audit(
                    connection,
                    actor=actor,
                    action=f"image.{decision}",
                    target_type="production_image",
                    target_id=image_id,
                    before={"status": image["status"]},
                    after={"status": next_status, "reason_tags": tags, "note": note},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.production_image(image_id)

    def override_qc(
        self,
        image_id: str,
        decision: str,
        *,
        reason: str,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if decision not in {"pass", "fail"}:
            raise WorkflowError("INVALID_QC_OVERRIDE", "人工覆盖只能选择通过或不通过。")
        reason = str(reason).strip()[:1000]
        if not reason:
            raise WorkflowError("OVERRIDE_REASON_REQUIRED", "人工覆盖必须填写原因。")
        actor_id, actor_role = self._actor(actor)
        now = utc_now()
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                image = connection.execute(
                    "SELECT * FROM production_images WHERE id = ?", (image_id,)
                ).fetchone()
                if not image:
                    raise WorkflowError("IMAGE_NOT_FOUND", "生产候选不存在。", status=404)
                qc = connection.execute(
                    "SELECT * FROM qc_results WHERE image_id = ? ORDER BY created_at DESC LIMIT 1",
                    (image_id,),
                ).fetchone()
                if not qc:
                    raise WorkflowError("QC_RESULT_NOT_FOUND", "候选没有可覆盖的 QC 结果。", status=409)
                next_status = "review_ready" if decision == "pass" else "qc_blocked"
                connection.execute(
                    """
                    INSERT INTO qc_overrides(
                        id, image_id, qc_result_id, decision, reason,
                        actor_id, actor_role, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _new_id("qcoverride"), image_id, qc["id"], decision,
                        reason, actor_id, actor_role, now,
                    ),
                )
                connection.execute(
                    "UPDATE production_images SET status = ?, updated_at = ? WHERE id = ?",
                    (next_status, now, image_id),
                )
                self._audit(
                    connection,
                    actor=actor,
                    action="image.qc_overridden",
                    target_type="production_image",
                    target_id=image_id,
                    before={"status": image["status"], "qc_status": qc["status"]},
                    after={"status": next_status, "decision": decision, "reason": reason},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.production_image(image_id)

    def create_rework_order(
        self,
        image_id: str,
        *,
        preserve: Iterable[str],
        change: Iterable[str],
        avoid: Iterable[str],
        note: str = "",
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        preserve_items = list(dict.fromkeys(str(item).strip()[:80] for item in preserve if str(item).strip()))[:10]
        change_items = list(dict.fromkeys(str(item).strip()[:80] for item in change if str(item).strip()))[:10]
        avoid_items = list(dict.fromkeys(str(item).strip()[:80] for item in avoid if str(item).strip()))[:10]
        if not change_items:
            raise WorkflowError("REWORK_CHANGE_REQUIRED", "返工单必须明确至少一项修改内容。")
        actor_id, actor_role = self._actor(actor)
        order_id = _new_id("rework")
        now = utc_now()
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                image = connection.execute(
                    "SELECT * FROM production_images WHERE id = ?", (image_id,)
                ).fetchone()
                if not image:
                    raise WorkflowError("IMAGE_NOT_FOUND", "生产候选不存在。", status=404)
                connection.execute(
                    """
                    INSERT INTO rework_orders(
                        id, source_image_id, project_id, poem_id, direction_id,
                        preserve_json, change_json, avoid_json, note, status,
                        actor_id, actor_role, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?)
                    """,
                    (
                        order_id, image_id, image["project_id"], image["poem_id"],
                        image["direction_id"], _json(preserve_items),
                        _json(change_items), _json(avoid_items),
                        str(note).strip()[:1000], actor_id, actor_role, now, now,
                    ),
                )
                connection.execute(
                    "UPDATE poems SET status = 'rework', updated_at = ? WHERE id = ?",
                    (now, image["poem_id"]),
                )
                self._audit(
                    connection,
                    actor=actor,
                    action="rework.created",
                    target_type="rework_order",
                    target_id=order_id,
                    after={
                        "source_image_id": image_id,
                        "preserve": preserve_items,
                        "change": change_items,
                        "avoid": avoid_items,
                    },
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.rework_order(order_id)

    def create_rework_batch(
        self,
        order_id: str,
        *,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create one idempotent, high-priority generation task for a rework order."""

        actor_id, _ = self._actor(actor)
        now = utc_now()
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT b.*
                    FROM generation_tasks t
                    JOIN generation_batches b ON b.id = t.batch_id
                    WHERE t.rework_order_id = ?
                    LIMIT 1
                    """,
                    (order_id,),
                ).fetchone()
                if existing:
                    connection.execute("COMMIT")
                    return self._batch_dict(existing)
                row = connection.execute(
                    """
                    SELECT r.*, i.task_id AS source_task_id, i.style_id,
                           i.style_version_id,
                           i.aspect_ratio, i.provider, i.model,
                           p.title AS poem_title,
                           t.prompt_json AS source_prompt_json,
                           b.settings_json AS source_settings_json
                    FROM rework_orders r
                    JOIN production_images i ON i.id = r.source_image_id
                    JOIN poems p ON p.id = r.poem_id
                    JOIN generation_tasks t ON t.id = i.task_id
                    JOIN generation_batches b ON b.id = i.batch_id
                    WHERE r.id = ?
                    """,
                    (order_id,),
                ).fetchone()
                if not row:
                    raise WorkflowError("REWORK_NOT_FOUND", "返工单不存在。", status=404)
                if row["status"] != "draft":
                    raise WorkflowError(
                        "INVALID_REWORK_STATE",
                        "只有草稿返工单可以进入生产队列。",
                        status=409,
                    )
                source_settings = _decode(row["source_settings_json"], {})
                unit_cost = max(0.0, float(source_settings.get("unit_cost", 0)))
                budget = self._available_budget_locked(connection, row["project_id"])
                batch_id = _new_id("batch")
                task_id = _new_id("task")
                source_prompt = _decode(row["source_prompt_json"], {})
                source_prompt["rework"] = {
                    "order_id": order_id,
                    "parent_image_id": row["source_image_id"],
                    "preserve": _decode(row["preserve_json"], []),
                    "change": _decode(row["change_json"], []),
                    "avoid": _decode(row["avoid_json"], []),
                    "note": row["note"],
                }
                source_prompt.pop("compiled", None)
                try:
                    source_prompt["compiled"] = compile_generation_prompt(
                        source_prompt, row["provider"]
                    )
                except PromptCompileError as exc:
                    raise WorkflowError(exc.code, str(exc), status=409) from exc
                connection.execute(
                    """
                    INSERT INTO generation_batches(
                        id, project_id, name, provider, model, style_id,
                        style_version_id, aspect_ratio, count_per_direction,
                        priority, status,
                        task_count, estimated_cost, actual_cost, currency,
                        budget_snapshot_json, settings_json, created_by,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 90, 'draft', 1, ?, 0,
                              'USD', ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        row["project_id"],
                        f"返工 · {row['poem_title']} · {row['source_image_id'][-8:]}",
                        row["provider"],
                        row["model"],
                        row["style_id"],
                        row["style_version_id"],
                        row["aspect_ratio"],
                        unit_cost,
                        _json(budget),
                        _json(
                            {
                                "unit_cost": unit_cost,
                                "poem_ids": [row["poem_id"]],
                                "direction_ids": [row["direction_id"]],
                                "rework_order_id": order_id,
                            }
                        ),
                        actor_id,
                        now,
                        now,
                    ),
                )
                idempotency_key = hashlib.sha256(
                    f"rework|{order_id}|{row['source_image_id']}".encode("utf-8")
                ).hexdigest()
                connection.execute(
                    """
                    INSERT INTO generation_tasks(
                        id, batch_id, poem_id, direction_id, sample_index,
                        status, priority, idempotency_key, prompt_json,
                        attempt_count, max_attempts, rework_order_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, 'pending', 90, ?, ?, 0, 3, ?, ?, ?)
                    """,
                    (
                        task_id,
                        batch_id,
                        row["poem_id"],
                        row["direction_id"],
                        idempotency_key,
                        _json(source_prompt),
                        order_id,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE rework_orders SET status = 'scheduled', updated_at = ? WHERE id = ?",
                    (now, order_id),
                )
                self._audit(
                    connection,
                    actor=actor,
                    action="rework.scheduled",
                    target_type="rework_order",
                    target_id=order_id,
                    before={"status": "draft"},
                    after={"status": "scheduled", "batch_id": batch_id, "task_id": task_id},
                )
                result = self._batch_dict(
                    connection.execute(
                        "SELECT * FROM generation_batches WHERE id = ?", (batch_id,)
                    ).fetchone()
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def rework_order(self, order_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM rework_orders WHERE id = ?", (order_id,)
            ).fetchone()
        if not row:
            raise WorkflowError("REWORK_NOT_FOUND", "返工单不存在。", status=404)
        return self._production_image_dict(row)

    def rework_orders(
        self,
        project_id: str = DEFAULT_PROJECT_ID,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT r.*, p.title AS poem_title, i.url AS source_url
                FROM rework_orders r
                JOIN poems p ON p.id = r.poem_id
                JOIN production_images i ON i.id = r.source_image_id
                WHERE r.project_id = ?
                ORDER BY r.created_at DESC
                LIMIT 500
                """,
                (project_id,),
            ).fetchall()
        return [self._production_image_dict(row) for row in rows]

    def _asset_file_errors(self, image: sqlite3.Row | dict[str, Any]) -> list[str]:
        image = dict(image)
        errors: list[str] = []
        path = Path(str(image["file_path"])).resolve()
        if not path.is_file():
            return ["asset_file_missing"]
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        if checksum != image["checksum"]:
            errors.append("asset_checksum_mismatch")
        width = int(image["width"] or 0)
        height = int(image["height"] or 0)
        if width < 768 or height < 768:
            errors.append("asset_resolution_below_baseline")
        spec = image.get("spec") or _decode(image.get("spec_json"), {})
        expected = EXPECTED_RATIOS.get(
            image.get("aspect_ratio") or spec.get("aspect_ratio")
        )
        actual = width / height if width and height else 0
        if not expected or not actual or abs(expected - actual) > 0.04:
            errors.append("asset_aspect_ratio_mismatch")
        if image["mime_type"] not in {
            "image/png",
            "image/jpeg",
            "image/svg+xml",
            "image/webp",
        }:
            errors.append("asset_mime_not_supported")
        return errors

    def finalize_image(
        self,
        image_id: str,
        *,
        reviewer_type: str,
        decision: str,
        reason: str = "",
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        reviewer_type = str(reviewer_type)
        decision = str(decision)
        if reviewer_type not in {"content", "art"}:
            raise WorkflowError("INVALID_REVIEWER_TYPE", "终审类型必须是内容或美术。")
        if decision not in {"approved", "rejected"}:
            raise WorkflowError("INVALID_FINAL_DECISION", "终审结论必须是通过或退回。")
        reason = str(reason).strip()[:1000]
        if decision == "rejected" and not reason:
            raise WorkflowError("FINAL_REASON_REQUIRED", "终审退回必须填写原因。")
        actor_id, actor_role = self._actor(actor)
        allowed_roles = {
            "content": {"content_editor", "producer"},
            "art": {"art_director", "producer"},
        }
        if actor_role not in allowed_roles[reviewer_type]:
            raise WorkflowError(
                "FINAL_ROLE_REQUIRED",
                "当前操作者角色不能执行这类终审。",
                status=403,
            )
        now = utc_now()
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                image = connection.execute(
                    "SELECT * FROM production_images WHERE id = ?", (image_id,)
                ).fetchone()
                if not image:
                    raise WorkflowError("IMAGE_NOT_FOUND", "生产候选不存在。", status=404)
                if image["status"] not in {"final_candidate", "finalized"}:
                    raise WorkflowError(
                        "FINAL_CANDIDATE_REQUIRED",
                        "只有终审候选可以提交内容或美术终审。",
                        status=409,
                    )
                latest_same = connection.execute(
                    """
                    SELECT * FROM final_approvals
                    WHERE image_id = ? AND reviewer_type = ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (image_id, reviewer_type),
                ).fetchone()
                current_asset = connection.execute(
                    """
                    SELECT * FROM final_assets
                    WHERE image_id = ? AND is_current = 1
                    ORDER BY version DESC LIMIT 1
                    """,
                    (image_id,),
                ).fetchone()
                if (
                    latest_same
                    and latest_same["decision"] == decision
                    and decision == "approved"
                    and current_asset
                ):
                    connection.execute("COMMIT")
                    result_asset = dict(current_asset)
                    result_asset["spec"] = _decode(
                        result_asset.pop("spec_json", None), {}
                    )
                    return {
                        "image": self.production_image(image_id),
                        "final_asset": result_asset,
                        "locked": True,
                    }
                approval_id = _new_id("finalapproval")
                connection.execute(
                    """
                    INSERT INTO final_approvals(
                        id, image_id, reviewer_type, decision, reason,
                        actor_id, actor_role, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        approval_id,
                        image_id,
                        reviewer_type,
                        decision,
                        reason,
                        actor_id,
                        actor_role,
                        now,
                    ),
                )
                self._audit(
                    connection,
                    actor=actor,
                    action=f"image.final_{reviewer_type}_{decision}",
                    target_type="production_image",
                    target_id=image_id,
                    after={"approval_id": approval_id, "decision": decision, "reason": reason},
                )
                if decision == "rejected":
                    connection.execute(
                        "UPDATE production_images SET status = 'selected', updated_at = ? WHERE id = ?",
                        (now, image_id),
                    )
                    connection.execute(
                        "UPDATE poems SET status = 'candidate_review', updated_at = ? WHERE id = ?",
                        (now, image["poem_id"]),
                    )
                    connection.execute("COMMIT")
                    return {
                        "image": self.production_image(image_id),
                        "final_asset": None,
                        "locked": False,
                    }
                latest_approvals = {}
                for kind in ("content", "art"):
                    latest_approvals[kind] = connection.execute(
                        """
                        SELECT * FROM final_approvals
                        WHERE image_id = ? AND reviewer_type = ?
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (image_id, kind),
                    ).fetchone()
                both_approved = all(
                    latest_approvals[kind]
                    and latest_approvals[kind]["decision"] == "approved"
                    for kind in ("content", "art")
                )
                final_asset = None
                if both_approved:
                    qc = connection.execute(
                        """
                        SELECT * FROM qc_results
                        WHERE image_id = ? ORDER BY created_at DESC LIMIT 1
                        """,
                        (image_id,),
                    ).fetchone()
                    if not qc:
                        raise WorkflowError(
                            "QC_RESULT_REQUIRED",
                            "终审候选缺少 QC 结果。",
                            status=409,
                        )
                    if _decode(qc["hard_failures_json"], []):
                        override = connection.execute(
                            """
                            SELECT * FROM qc_overrides
                            WHERE image_id = ? ORDER BY created_at DESC LIMIT 1
                            """,
                            (image_id,),
                        ).fetchone()
                        if not override or override["decision"] != "pass":
                            raise WorkflowError(
                                "QC_BLOCKS_FINALIZATION",
                                "候选仍有未覆盖的 QC 硬失败。",
                                status=409,
                            )
                    file_errors = self._asset_file_errors(image)
                    if file_errors:
                        raise WorkflowError(
                            "FINAL_ASSET_INVALID",
                            "终审资产文件校验失败：" + "、".join(file_errors),
                            status=409,
                        )
                    existing = connection.execute(
                        "SELECT * FROM final_assets WHERE image_id = ? ORDER BY version DESC LIMIT 1",
                        (image_id,),
                    ).fetchone()
                    if existing:
                        final_asset = dict(existing)
                    else:
                        version = connection.execute(
                            "SELECT COALESCE(MAX(version), 0) + 1 AS version FROM final_assets WHERE poem_id = ?",
                            (image["poem_id"],),
                        ).fetchone()["version"]
                        connection.execute(
                            "UPDATE final_assets SET is_current = 0 WHERE poem_id = ? AND is_current = 1",
                            (image["poem_id"],),
                        )
                        asset_id = _new_id("asset")
                        spec = {
                            "aspect_ratio": image["aspect_ratio"],
                            "width": image["width"],
                            "height": image["height"],
                            "mime_type": image["mime_type"],
                            "color_space": "vector" if image["mime_type"] == "image/svg+xml" else "sRGB_policy",
                        }
                        connection.execute(
                            """
                            INSERT INTO final_assets(
                                id, project_id, poem_id, image_id, version,
                                is_current, status, spec_json, checksum,
                                file_path, mime_type, width, height,
                                qc_result_id, content_approval_id,
                                art_approval_id, created_by, created_at
                            ) VALUES (?, ?, ?, ?, ?, 1, 'locked', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                asset_id,
                                image["project_id"],
                                image["poem_id"],
                                image_id,
                                version,
                                _json(spec),
                                image["checksum"],
                                image["file_path"],
                                image["mime_type"],
                                image["width"],
                                image["height"],
                                qc["id"],
                                latest_approvals["content"]["id"],
                                latest_approvals["art"]["id"],
                                actor_id,
                                now,
                            ),
                        )
                        final_asset = dict(
                            connection.execute(
                                "SELECT * FROM final_assets WHERE id = ?", (asset_id,)
                            ).fetchone()
                        )
                        self._audit(
                            connection,
                            actor=actor,
                            action="final_asset.locked",
                            target_type="final_asset",
                            target_id=asset_id,
                            after={
                                "poem_id": image["poem_id"],
                                "image_id": image_id,
                                "version": version,
                                "checksum": image["checksum"],
                            },
                        )
                    connection.execute(
                        "UPDATE production_images SET status = 'finalized', updated_at = ? WHERE id = ?",
                        (now, image_id),
                    )
                    connection.execute(
                        "UPDATE poems SET status = 'approved', blocked_reason = '', updated_at = ? WHERE id = ?",
                        (now, image["poem_id"]),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        if final_asset:
            final_asset["spec"] = _decode(final_asset.pop("spec_json", None), {})
        return {
            "image": self.production_image(image_id),
            "final_asset": final_asset,
            "locked": bool(final_asset),
        }

    def final_assets(
        self,
        project_id: str = DEFAULT_PROJECT_ID,
        *,
        query: str = "",
        current_only: bool = True,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        where = ["a.project_id = ?"]
        params: list[Any] = [project_id]
        if current_only:
            where.append("a.is_current = 1")
        if query.strip():
            where.append("(p.title LIKE ? OR p.author LIKE ? OR i.style_id LIKE ?)")
            pattern = f"%{query.strip()}%"
            params.extend([pattern, pattern, pattern])
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT a.*, p.title AS poem_title, p.author, p.dynasty,
                       i.url, i.style_id, i.style_version_id,
                       i.provider, i.model,
                       i.direction_id, i.batch_id, i.task_id
                FROM final_assets a
                JOIN poems p ON p.id = a.poem_id
                JOIN production_images i ON i.id = a.image_id
                WHERE {' AND '.join(where)}
                ORDER BY p.author, p.title, a.version DESC
                LIMIT ?
                """,
                [*params, max(1, min(int(limit), 1000))],
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["spec"] = _decode(item.pop("spec_json", None), {})
            result.append(item)
        return result

    def export_estimate(
        self,
        project_id: str = DEFAULT_PROJECT_ID,
        *,
        poem_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        selected = set(str(item) for item in (poem_ids or []))
        assets = self.final_assets(project_id, current_only=True, limit=1000)
        if selected:
            assets = [asset for asset in assets if asset["poem_id"] in selected]
        errors = []
        total_bytes = 0
        for asset in assets:
            file_errors = self._asset_file_errors(asset)
            if file_errors:
                errors.append({"asset_id": asset["id"], "errors": file_errors})
            else:
                total_bytes += Path(asset["file_path"]).stat().st_size
        if not assets:
            errors.append({"asset_id": None, "errors": ["no_final_assets"]})
        return {
            "project_id": project_id,
            "asset_count": len(assets),
            "asset_ids": [asset["id"] for asset in assets],
            "poem_ids": [asset["poem_id"] for asset in assets],
            "total_bytes": total_bytes,
            "errors": errors,
            "can_export": not errors,
        }

    def _export_manifest_asset(
        self,
        connection: sqlite3.Connection,
        asset_id: str,
        relative_path: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT a.*, p.title, p.author, p.dynasty, p.lines_json,
                   i.style_id, i.style_version_id,
                   sv.version AS style_version, sv.name AS style_name,
                   i.provider, i.model, i.prompt, i.prompt_hash,
                   i.prompt_template_version, i.prompt_segments_json,
                   i.generation,
                   i.parent_image_id, i.batch_id, i.task_id, i.direction_id,
                   d.type AS direction_type, d.content_json AS direction_json,
                   r.id AS requirement_id, r.version AS requirement_version,
                   r.content_json AS requirement_json,
                   q.version AS qc_version, q.status AS qc_status,
                   q.score AS qc_score, q.hard_failures_json,
                   q.warnings_json, ca.actor_id AS content_approved_by,
                   ca.created_at AS content_approved_at,
                   aa.actor_id AS art_approved_by,
                   aa.created_at AS art_approved_at
            FROM final_assets a
            JOIN poems p ON p.id = a.poem_id
            JOIN production_images i ON i.id = a.image_id
            JOIN directions d ON d.id = i.direction_id
            JOIN requirements r ON r.id = d.requirement_id
            JOIN qc_results q ON q.id = a.qc_result_id
            JOIN final_approvals ca ON ca.id = a.content_approval_id
            JOIN final_approvals aa ON aa.id = a.art_approval_id
            LEFT JOIN style_pack_versions sv ON sv.id = i.style_version_id
            WHERE a.id = ?
            """,
            (asset_id,),
        ).fetchone()
        if not row:
            raise WorkflowError("FINAL_ASSET_NOT_FOUND", "成品不存在。", status=404)
        return {
            "final_asset_id": row["id"],
            "final_asset_version": row["version"],
            "current": bool(row["is_current"]),
            "poem": {
                "id": row["poem_id"],
                "title": row["title"],
                "author": row["author"],
                "dynasty": row["dynasty"],
                "lines": _decode(row["lines_json"], []),
            },
            "file": {
                "path": relative_path,
                "checksum_sha256": row["checksum"],
                "mime_type": row["mime_type"],
                "width": row["width"],
                "height": row["height"],
            },
            "source": {
                "image_id": row["image_id"],
                "batch_id": row["batch_id"],
                "task_id": row["task_id"],
                "direction": {
                    "id": row["direction_id"],
                    "type": row["direction_type"],
                    "content": _decode(row["direction_json"], {}),
                },
                "requirement": {
                    "id": row["requirement_id"],
                    "version": row["requirement_version"],
                    "content": _decode(row["requirement_json"], {}),
                },
                "style": {
                    "id": row["style_id"],
                    "version_id": row["style_version_id"],
                    "version": row["style_version"],
                    "name": row["style_name"],
                },
                "style_id": row["style_id"],
                "provider": row["provider"],
                "model": row["model"],
                "prompt": row["prompt"],
                "prompt_hash": row["prompt_hash"],
                "prompt_template_version": row["prompt_template_version"],
                "prompt_segments": _decode(row["prompt_segments_json"], {}),
                "generation": row["generation"],
                "parent_image_id": row["parent_image_id"],
            },
            "qc": {
                "version": row["qc_version"],
                "status": row["qc_status"],
                "score": row["qc_score"],
                "hard_failures": _decode(row["hard_failures_json"], []),
                "warnings": _decode(row["warnings_json"], []),
            },
            "approvals": {
                "content": {
                    "actor_id": row["content_approved_by"],
                    "created_at": row["content_approved_at"],
                },
                "art": {
                    "actor_id": row["art_approved_by"],
                    "created_at": row["art_approved_at"],
                },
            },
        }

    def create_export_package(
        self,
        project_id: str,
        export_root: Path,
        *,
        poem_ids: Iterable[str] | None = None,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        estimate = self.export_estimate(project_id, poem_ids=poem_ids)
        if not estimate["can_export"]:
            raise WorkflowError(
                "EXPORT_PRECHECK_FAILED",
                "导出预检未通过，请先修复成品文件或选择终审成品。",
                status=409,
            )
        actor_id, _ = self._actor(actor)
        package_id = _new_id("export")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"tang-poems-{timestamp}-{package_id[-8:]}"
        export_root = Path(export_root).resolve()
        final_dir = export_root / name
        temp_dir = export_root / f".{name}.tmp-{uuid.uuid4().hex[:8]}"
        now = utc_now()
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO export_packages(
                        id, project_id, name, status, asset_count,
                        created_by, created_at
                    ) VALUES (?, ?, ?, 'creating', ?, ?, ?)
                    """,
                    (
                        package_id,
                        project_id,
                        name,
                        estimate["asset_count"],
                        actor_id,
                        now,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        try:
            (temp_dir / "assets").mkdir(parents=True, exist_ok=False)
            manifest_assets = []
            export_items = []
            with self._connect() as connection:
                for asset_id in estimate["asset_ids"]:
                    asset = connection.execute(
                        "SELECT * FROM final_assets WHERE id = ?", (asset_id,)
                    ).fetchone()
                    extension = Path(asset["file_path"]).suffix.lower()
                    relative_path = f"assets/{asset['poem_id']}_v{asset['version']}{extension}"
                    target = temp_dir / relative_path
                    shutil.copy2(asset["file_path"], target)
                    checksum = hashlib.sha256(target.read_bytes()).hexdigest()
                    if checksum != asset["checksum"]:
                        raise RuntimeError("导出复制后的文件校验和不一致。")
                    manifest_assets.append(
                        self._export_manifest_asset(connection, asset_id, relative_path)
                    )
                    export_items.append(
                        {
                            "asset_id": asset_id,
                            "relative_path": relative_path,
                            "checksum": checksum,
                            "file_size": target.stat().st_size,
                        }
                    )
            manifest = {
                "schema_version": "1.0",
                "package_id": package_id,
                "project_id": project_id,
                "created_at": now,
                "asset_count": len(manifest_assets),
                "assets": manifest_assets,
            }
            manifest_path = temp_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            manifest_checksum = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            package_checksum = hashlib.sha256(
                "\n".join(
                    [
                        *(f"{item['relative_path']}:{item['checksum']}" for item in export_items),
                        f"manifest.json:{manifest_checksum}",
                    ]
                ).encode("utf-8")
            ).hexdigest()
            export_root.mkdir(parents=True, exist_ok=True)
            if final_dir.exists():
                raise RuntimeError("导出目录已存在，禁止覆盖历史交付包。")
            os.replace(temp_dir, final_dir)
            completed_at = utc_now()
            with self.lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for item in export_items:
                        connection.execute(
                            """
                            INSERT INTO export_items(
                                id, package_id, final_asset_id, relative_path,
                                checksum, file_size, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                _new_id("exportitem"), package_id,
                                item["asset_id"], item["relative_path"],
                                item["checksum"], item["file_size"], completed_at,
                            ),
                        )
                    connection.execute(
                        """
                        UPDATE export_packages
                        SET status = 'completed', output_path = ?, manifest_path = ?,
                            package_checksum = ?, completed_at = ?
                        WHERE id = ?
                        """,
                        (
                            str(final_dir),
                            str(final_dir / "manifest.json"),
                            package_checksum,
                            completed_at,
                            package_id,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE poems SET status = 'exported', updated_at = ?
                        WHERE id IN (
                            SELECT a.poem_id FROM export_items e
                            JOIN final_assets a ON a.id = e.final_asset_id
                            WHERE e.package_id = ?
                        )
                        """,
                        (completed_at, package_id),
                    )
                    self._audit(
                        connection,
                        actor=actor,
                        action="export.completed",
                        target_type="export_package",
                        target_id=package_id,
                        after={
                            "asset_count": len(export_items),
                            "output_path": str(final_dir),
                            "package_checksum": package_checksum,
                        },
                    )
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
        except Exception as exc:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
            with self.lock, self._connect() as connection:
                connection.execute(
                    """
                    UPDATE export_packages SET status = 'failed', error = ?
                    WHERE id = ?
                    """,
                    (str(exc)[:1000], package_id),
                )
            raise WorkflowError("EXPORT_FAILED", f"导出失败：{exc}", status=500) from exc
        return self.export_package(package_id)

    def export_package(self, package_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM export_packages WHERE id = ?", (package_id,)
            ).fetchone()
            items = connection.execute(
                "SELECT * FROM export_items WHERE package_id = ? ORDER BY relative_path",
                (package_id,),
            ).fetchall()
        if not row:
            raise WorkflowError("EXPORT_NOT_FOUND", "导出包不存在。", status=404)
        result = dict(row)
        result["items"] = [dict(item) for item in items]
        return result

    def export_packages(
        self,
        project_id: str = DEFAULT_PROJECT_ID,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM export_packages
                WHERE project_id = ? ORDER BY created_at DESC LIMIT ?
                """,
                (project_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    def _available_budget_locked(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        *,
        exclude_batch_id: str | None = None,
    ) -> dict[str, float]:
        policy = connection.execute(
            "SELECT * FROM budget_policies WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if not policy:
            raise WorkflowError(
                "BUDGET_POLICY_REQUIRED",
                "项目缺少预算策略。",
                status=409,
            )
        params: list[Any] = [project_id]
        exclude = ""
        if exclude_batch_id:
            exclude = "AND id != ?"
            params.append(exclude_batch_id)
        reserved = connection.execute(
            f"""
            SELECT COALESCE(SUM(
                CASE
                    WHEN estimated_cost > actual_cost
                    THEN estimated_cost - actual_cost
                    ELSE 0
                END
            ), 0) AS reserved
            FROM generation_batches
            WHERE project_id = ?
              AND status IN ('queued', 'running', 'paused')
              {exclude}
            """,
            params,
        ).fetchone()["reserved"]
        remaining = max(0.0, policy["hard_limit"] - policy["spent"] - reserved)
        return {
            "hard_limit": float(policy["hard_limit"]),
            "spent": float(policy["spent"]),
            "reserved": round(float(reserved), 6),
            "remaining": round(float(remaining), 6),
        }

    def start_batch(
        self,
        batch_id: str,
        *,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                batch_row = connection.execute(
                    "SELECT * FROM generation_batches WHERE id = ?",
                    (batch_id,),
                ).fetchone()
                if not batch_row:
                    raise WorkflowError(
                        "BATCH_NOT_FOUND", "生产批次不存在。", status=404
                    )
                if batch_row["status"] not in {
                    "draft",
                    "paused",
                    "budget_blocked",
                }:
                    raise WorkflowError(
                        "INVALID_BATCH_STATE",
                        "只有草稿、暂停或预算阻塞批次可以启动。",
                        status=409,
                    )
                active_conflicts = connection.execute(
                    """
                    SELECT DISTINCT t.poem_id
                    FROM generation_tasks t
                    JOIN generation_tasks other ON other.poem_id = t.poem_id
                    JOIN generation_batches other_batch
                      ON other_batch.id = other.batch_id
                    WHERE t.batch_id = ?
                      AND other.batch_id != ?
                      AND other.status IN ('ready', 'running', 'retry_waiting')
                      AND other_batch.status IN ('queued', 'running')
                    """,
                    (batch_id, batch_id),
                ).fetchall()
                if active_conflicts:
                    raise WorkflowError(
                        "POEM_ALREADY_SCHEDULED",
                        "部分诗词已在其他运行批次中，请等待或取消冲突批次。",
                        status=409,
                    )
                unit_cost = float(
                    _decode(batch_row["settings_json"], {}).get("unit_cost", 0)
                )
                outstanding_count = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM generation_tasks
                    WHERE batch_id = ?
                      AND status IN ('pending', 'ready', 'retry_waiting')
                    """,
                    (batch_id,),
                ).fetchone()["count"]
                available = self._available_budget_locked(
                    connection,
                    batch_row["project_id"],
                    exclude_batch_id=batch_id,
                )
                outstanding_cost = round(outstanding_count * unit_cost, 6)
                now = utc_now()
                if outstanding_cost > available["remaining"]:
                    connection.execute(
                        """
                        UPDATE generation_batches
                        SET status = 'budget_blocked',
                            budget_snapshot_json = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (_json(available), now, batch_id),
                    )
                    connection.execute(
                        """
                        UPDATE rework_orders SET status = 'budget_blocked', updated_at = ?
                        WHERE id IN (
                            SELECT rework_order_id FROM generation_tasks
                            WHERE batch_id = ? AND rework_order_id IS NOT NULL
                        )
                        """,
                        (now, batch_id),
                    )
                    self._audit(
                        connection,
                        actor=actor,
                        action="batch.budget_blocked",
                        target_type="generation_batch",
                        target_id=batch_id,
                        before={"status": batch_row["status"]},
                        after={
                            "status": "budget_blocked",
                            "outstanding_cost": outstanding_cost,
                            "available": available,
                        },
                    )
                    connection.execute("COMMIT")
                    return self.batch(batch_id)
                connection.execute(
                    """
                    UPDATE generation_tasks
                    SET status = 'ready', updated_at = ?
                    WHERE batch_id = ? AND status = 'pending'
                    """,
                    (now, batch_id),
                )
                runnable = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM generation_tasks
                    WHERE batch_id = ?
                      AND status IN ('ready', 'retry_waiting')
                    """,
                    (batch_id,),
                ).fetchone()["count"]
                if not runnable:
                    raise WorkflowError(
                        "NO_RUNNABLE_TASKS",
                        "批次没有可执行任务；失败项请使用“重试失败”。",
                        status=409,
                    )
                connection.execute(
                    """
                    UPDATE generation_batches
                    SET status = 'queued', budget_snapshot_json = ?,
                        started_at = COALESCE(started_at, ?), updated_at = ?
                    WHERE id = ?
                    """,
                    (_json(available), now, now, batch_id),
                )
                connection.execute(
                    """
                    UPDATE poems
                    SET status = 'generating', blocked_reason = '', updated_at = ?
                    WHERE id IN (
                        SELECT DISTINCT poem_id
                        FROM generation_tasks
                        WHERE batch_id = ?
                          AND status IN ('ready', 'retry_waiting')
                    )
                    """,
                    (now, batch_id),
                )
                connection.execute(
                    """
                    UPDATE rework_orders SET status = 'running', updated_at = ?
                    WHERE id IN (
                        SELECT rework_order_id FROM generation_tasks
                        WHERE batch_id = ? AND rework_order_id IS NOT NULL
                    )
                    """,
                    (now, batch_id),
                )
                self._audit(
                    connection,
                    actor=actor,
                    action="batch.started",
                    target_type="generation_batch",
                    target_id=batch_id,
                    before={"status": batch_row["status"]},
                    after={"status": "queued", "reserved": outstanding_cost},
                )
                result = self._batch_dict(
                    connection.execute(
                        "SELECT * FROM generation_batches WHERE id = ?",
                        (batch_id,),
                    ).fetchone()
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def pause_batch(
        self,
        batch_id: str,
        *,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM generation_batches WHERE id = ?",
                    (batch_id,),
                ).fetchone()
                if not row:
                    raise WorkflowError(
                        "BATCH_NOT_FOUND", "生产批次不存在。", status=404
                    )
                if row["status"] not in {"queued", "running"}:
                    raise WorkflowError(
                        "INVALID_BATCH_STATE",
                        "只有排队或运行中的批次可以暂停。",
                        status=409,
                    )
                now = utc_now()
                connection.execute(
                    """
                    UPDATE generation_batches
                    SET status = 'paused', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, batch_id),
                )
                self._audit(
                    connection,
                    actor=actor,
                    action="batch.paused",
                    target_type="generation_batch",
                    target_id=batch_id,
                    before={"status": row["status"]},
                    after={"status": "paused"},
                )
                result = self._batch_dict(
                    connection.execute(
                        "SELECT * FROM generation_batches WHERE id = ?",
                        (batch_id,),
                    ).fetchone()
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def pause_provider_batches(
        self,
        provider: str,
        *,
        reason: str,
        actor: dict[str, Any] | None = None,
    ) -> list[str]:
        """Pause every active batch for an unhealthy provider in one transaction."""

        provider = str(provider).strip()[:40]
        reason = str(reason).strip()[:500] or "Provider 暂时不可用。"
        now = utc_now()
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    """
                    SELECT id, status FROM generation_batches
                    WHERE provider = ? AND status IN ('queued', 'running')
                    """,
                    (provider,),
                ).fetchall()
                batch_ids = [row["id"] for row in rows]
                if batch_ids:
                    placeholders = ",".join("?" for _ in batch_ids)
                    connection.execute(
                        f"""
                        UPDATE generation_batches SET status='paused', updated_at=?
                        WHERE id IN ({placeholders})
                        """,
                        [now, *batch_ids],
                    )
                    connection.execute(
                        f"""
                        UPDATE poems SET status='paused', blocked_reason=?, updated_at=?
                        WHERE status='generating' AND id IN (
                            SELECT DISTINCT poem_id FROM generation_tasks
                            WHERE batch_id IN ({placeholders})
                        )
                        """,
                        [reason, now, *batch_ids],
                    )
                    for row in rows:
                        self._audit(
                            connection,
                            actor=actor,
                            action="batch.provider_circuit_paused",
                            target_type="generation_batch",
                            target_id=row["id"],
                            before={"status": row["status"]},
                            after={"status": "paused", "provider": provider, "reason": reason},
                        )
                connection.execute("COMMIT")
                return batch_ids
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def cancel_batch(
        self,
        batch_id: str,
        *,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM generation_batches WHERE id = ?",
                    (batch_id,),
                ).fetchone()
                if not row:
                    raise WorkflowError(
                        "BATCH_NOT_FOUND", "生产批次不存在。", status=404
                    )
                if row["status"] in {"completed", "cancelled"}:
                    raise WorkflowError(
                        "INVALID_BATCH_STATE",
                        "已完成或已取消批次不能再次取消。",
                        status=409,
                    )
                now = utc_now()
                connection.execute(
                    """
                    UPDATE generation_tasks
                    SET status = 'cancelled', finished_at = ?, updated_at = ?
                    WHERE batch_id = ?
                      AND status IN (
                          'pending', 'ready', 'retry_waiting', 'blocked'
                      )
                    """,
                    (now, now, batch_id),
                )
                connection.execute(
                    """
                    UPDATE rework_orders SET status = 'cancelled', updated_at = ?
                    WHERE id IN (
                        SELECT rework_order_id FROM generation_tasks
                        WHERE batch_id = ? AND rework_order_id IS NOT NULL
                    )
                    """,
                    (now, batch_id),
                )
                connection.execute(
                    """
                    UPDATE generation_batches
                    SET status = 'cancelled', finished_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, batch_id),
                )
                self._audit(
                    connection,
                    actor=actor,
                    action="batch.cancelled",
                    target_type="generation_batch",
                    target_id=batch_id,
                    before={"status": row["status"]},
                    after={"status": "cancelled"},
                )
                self._restore_poem_states_locked(connection, batch_id, now)
                result = self._batch_dict(
                    connection.execute(
                        "SELECT * FROM generation_batches WHERE id = ?",
                        (batch_id,),
                    ).fetchone()
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def retry_failed_tasks(
        self,
        batch_id: str,
        *,
        confirm_unknown: bool = False,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                batch = connection.execute(
                    "SELECT * FROM generation_batches WHERE id = ?",
                    (batch_id,),
                ).fetchone()
                if not batch:
                    raise WorkflowError(
                        "BATCH_NOT_FOUND", "生产批次不存在。", status=404
                    )
                unknown = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM generation_tasks
                    WHERE batch_id = ? AND status = 'blocked'
                      AND last_error_code = 'OUTCOME_UNKNOWN'
                    """,
                    (batch_id,),
                ).fetchone()["count"]
                if unknown and not confirm_unknown:
                    raise WorkflowError(
                        "UNKNOWN_OUTCOME_CONFIRMATION_REQUIRED",
                        "存在结果未知任务；确认已核对外部账单和资产后才能重试。",
                        status=409,
                    )
                retryable = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM generation_tasks
                    WHERE batch_id = ? AND status IN ('failed', 'blocked')
                    """,
                    (batch_id,),
                ).fetchone()["count"]
                if not retryable:
                    raise WorkflowError(
                        "NO_FAILED_TASKS",
                        "批次没有可重试的失败任务。",
                        status=409,
                    )
                unit_cost = float(
                    _decode(batch["settings_json"], {}).get("unit_cost", 0)
                )
                retry_cost = round(retryable * unit_cost, 6)
                available = self._available_budget_locked(
                    connection,
                    batch["project_id"],
                    exclude_batch_id=batch_id,
                )
                if retry_cost > available["remaining"]:
                    now = utc_now()
                    connection.execute(
                        """
                        UPDATE generation_batches
                        SET status = 'budget_blocked',
                            budget_snapshot_json = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (_json(available), now, batch_id),
                    )
                    self._audit(
                        connection,
                        actor=actor,
                        action="batch.retry_budget_blocked",
                        target_type="generation_batch",
                        target_id=batch_id,
                        before={"status": batch["status"]},
                        after={
                            "status": "budget_blocked",
                            "retry_cost": retry_cost,
                            "available": available,
                        },
                    )
                    result = self._batch_dict(
                        connection.execute(
                            "SELECT * FROM generation_batches WHERE id = ?",
                            (batch_id,),
                        ).fetchone()
                    )
                    connection.execute("COMMIT")
                    return result
                now = utc_now()
                connection.execute(
                    """
                    UPDATE generation_tasks
                    SET status = 'ready', last_error_code = '',
                        last_error_message = '', retry_at = NULL,
                        finished_at = NULL, updated_at = ?
                    WHERE batch_id = ? AND status IN ('failed', 'blocked')
                    """,
                    (now, batch_id),
                )
                connection.execute(
                    """
                    UPDATE generation_batches
                    SET status = 'queued', finished_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, batch_id),
                )
                connection.execute(
                    """
                    UPDATE poems
                    SET status = 'generating', blocked_reason = '', updated_at = ?
                    WHERE id IN (
                        SELECT DISTINCT poem_id
                        FROM generation_tasks
                        WHERE batch_id = ? AND status = 'ready'
                    )
                    """,
                    (now, batch_id),
                )
                self._audit(
                    connection,
                    actor=actor,
                    action="batch.retry_failed",
                    target_type="generation_batch",
                    target_id=batch_id,
                    before={"status": batch["status"], "failed_count": retryable},
                    after={"status": "queued", "retried_count": retryable},
                )
                result = self._batch_dict(
                    connection.execute(
                        "SELECT * FROM generation_batches WHERE id = ?",
                        (batch_id,),
                    ).fetchone()
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def claim_next_task(self, batch_id: str) -> dict[str, Any] | None:
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                batch = connection.execute(
                    "SELECT * FROM generation_batches WHERE id = ?",
                    (batch_id,),
                ).fetchone()
                if not batch:
                    raise WorkflowError(
                        "BATCH_NOT_FOUND", "生产批次不存在。", status=404
                    )
                if batch["status"] not in {"queued", "running"}:
                    connection.execute("COMMIT")
                    return None
                now = utc_now()
                task = connection.execute(
                    """
                    SELECT *
                    FROM generation_tasks
                    WHERE batch_id = ?
                      AND (
                          status = 'ready'
                          OR (status = 'retry_waiting' AND retry_at <= ?)
                      )
                    ORDER BY priority DESC, created_at
                    LIMIT 1
                    """,
                    (batch_id, now),
                ).fetchone()
                if not task:
                    self._refresh_batch_state_locked(connection, batch_id, now)
                    connection.execute("COMMIT")
                    return None
                attempt_number = task["attempt_count"] + 1
                attempt_id = _new_id("attempt")
                unit_cost = float(
                    _decode(batch["settings_json"], {}).get("unit_cost", 0)
                )
                connection.execute(
                    """
                    UPDATE generation_tasks
                    SET status = 'running', attempt_count = ?,
                        started_at = COALESCE(started_at, ?),
                        updated_at = ?, retry_at = NULL
                    WHERE id = ?
                    """,
                    (attempt_number, now, now, task["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO generation_attempts(
                        id, task_id, attempt_number, provider, model, status,
                        request_json, estimated_cost, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        task["id"],
                        attempt_number,
                        batch["provider"],
                        batch["model"],
                        task["prompt_json"],
                        unit_cost,
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE generation_batches
                    SET status = 'running', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, batch_id),
                )
                claimed = self._task_dict(
                    connection.execute(
                        """
                        SELECT t.*, p.title AS poem_title, p.author, p.dynasty,
                               p.lines_json, p.theme, p.mood,
                               b.project_id, b.name AS batch_name, b.provider,
                               b.model, b.style_id, b.style_version_id,
                               b.aspect_ratio,
                               b.settings_json, b.status AS batch_status
                        FROM generation_tasks t
                        JOIN generation_batches b ON b.id = t.batch_id
                        JOIN poems p ON p.id = t.poem_id
                        WHERE t.id = ?
                        """,
                        (task["id"],),
                    ).fetchone()
                )
                claimed["lines"] = _decode(claimed.pop("lines_json", None), [])
                claimed["batch_settings"] = _decode(
                    claimed.pop("settings_json", None), {}
                )
                claimed["attempt_id"] = attempt_id
                claimed["attempt_number"] = attempt_number
                connection.execute("COMMIT")
                return claimed
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def complete_task(
        self,
        task_id: str,
        attempt_id: str,
        *,
        output_image_id: str,
        actual_cost: float,
        duration_ms: int,
        response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        actual_cost = max(0.0, round(float(actual_cost), 6))
        now = utc_now()
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                task = connection.execute(
                    """
                    SELECT t.*, b.project_id, b.provider, b.model
                    FROM generation_tasks t
                    JOIN generation_batches b ON b.id = t.batch_id
                    WHERE t.id = ?
                    """,
                    (task_id,),
                ).fetchone()
                if not task:
                    raise WorkflowError(
                        "TASK_NOT_FOUND", "生成任务不存在。", status=404
                    )
                if task["status"] != "running":
                    raise WorkflowError(
                        "INVALID_TASK_STATE",
                        "只有运行中任务可以标记成功。",
                        status=409,
                    )
                attempt = connection.execute(
                    """
                    SELECT *
                    FROM generation_attempts
                    WHERE id = ? AND task_id = ? AND status = 'running'
                    """,
                    (attempt_id, task_id),
                ).fetchone()
                if not attempt:
                    raise WorkflowError(
                        "ATTEMPT_NOT_FOUND",
                        "运行尝试不存在或已经结束。",
                        status=409,
                    )
                connection.execute(
                    """
                    UPDATE generation_tasks
                    SET status = 'succeeded', output_image_id = ?,
                        last_error_code = '', last_error_message = '',
                        finished_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (output_image_id, now, now, task_id),
                )
                connection.execute(
                    """
                    UPDATE generation_attempts
                    SET status = 'succeeded', response_json = ?,
                        duration_ms = ?, actual_cost = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    (
                        _json(response or {"output_image_id": output_image_id}),
                        max(0, int(duration_ms)),
                        actual_cost,
                        now,
                        attempt_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO usage_records(
                        id, project_id, batch_id, task_id, attempt_id,
                        provider, model, units, estimated_cost, actual_cost,
                        currency, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 'USD', ?)
                    """,
                    (
                        _new_id("usage"),
                        task["project_id"],
                        task["batch_id"],
                        task_id,
                        attempt_id,
                        task["provider"],
                        task["model"],
                        attempt["estimated_cost"],
                        actual_cost,
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE generation_batches
                    SET actual_cost = actual_cost + ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (actual_cost, now, task["batch_id"]),
                )
                connection.execute(
                    """
                    UPDATE budget_policies
                    SET spent = spent + ?, updated_at = ?
                    WHERE project_id = ?
                    """,
                    (actual_cost, now, task["project_id"]),
                )
                if task["rework_order_id"]:
                    connection.execute(
                        """
                        UPDATE rework_orders
                        SET status = 'completed', output_image_id = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (output_image_id, now, task["rework_order_id"]),
                    )
                    self._audit(
                        connection,
                        actor={"id": "worker", "role": "system"},
                        action="rework.completed",
                        target_type="rework_order",
                        target_id=task["rework_order_id"],
                        after={"status": "completed", "output_image_id": output_image_id},
                    )
                self._audit(
                    connection,
                    actor={"id": "worker", "role": "system"},
                    action="task.succeeded",
                    target_type="generation_task",
                    target_id=task_id,
                    before={"status": "running"},
                    after={
                        "status": "succeeded",
                        "output_image_id": output_image_id,
                        "actual_cost": actual_cost,
                    },
                )
                self._refresh_batch_state_locked(
                    connection, task["batch_id"], now
                )
                result = self._task_dict(
                    connection.execute(
                        "SELECT * FROM generation_tasks WHERE id = ?",
                        (task_id,),
                    ).fetchone()
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def fail_task(
        self,
        task_id: str,
        attempt_id: str,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
        duration_ms: int,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                task = connection.execute(
                    "SELECT * FROM generation_tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                if not task:
                    raise WorkflowError(
                        "TASK_NOT_FOUND", "生成任务不存在。", status=404
                    )
                if task["status"] != "running":
                    raise WorkflowError(
                        "INVALID_TASK_STATE",
                        "只有运行中任务可以记录失败。",
                        status=409,
                    )
                can_retry = retryable and task["attempt_count"] < task["max_attempts"]
                next_status = "retry_waiting" if can_retry else "failed"
                retry_at = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=min(30, 2 ** max(0, task["attempt_count"] - 1)))
                ).isoformat(timespec="seconds") if can_retry else None
                connection.execute(
                    """
                    UPDATE generation_tasks
                    SET status = ?, last_error_code = ?,
                        last_error_message = ?, retry_at = ?,
                        finished_at = CASE WHEN ? = 'failed' THEN ? ELSE NULL END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        next_status,
                        str(error_code)[:80],
                        str(error_message)[:1000],
                        retry_at,
                        next_status,
                        now,
                        now,
                        task_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE generation_attempts
                    SET status = 'failed', error_code = ?, error_message = ?,
                        duration_ms = ?, finished_at = ?
                    WHERE id = ? AND task_id = ?
                    """,
                    (
                        str(error_code)[:80],
                        str(error_message)[:1000],
                        max(0, int(duration_ms)),
                        now,
                        attempt_id,
                        task_id,
                    ),
                )
                if task["rework_order_id"] and not can_retry:
                    connection.execute(
                        """
                        UPDATE rework_orders
                        SET status = 'failed', updated_at = ?
                        WHERE id = ?
                        """,
                        (now, task["rework_order_id"]),
                    )
                self._audit(
                    connection,
                    actor={"id": "worker", "role": "system"},
                    action="task.retry_scheduled" if can_retry else "task.failed",
                    target_type="generation_task",
                    target_id=task_id,
                    before={"status": "running"},
                    after={
                        "status": next_status,
                        "error_code": error_code,
                        "retry_at": retry_at,
                    },
                )
                self._refresh_batch_state_locked(
                    connection, task["batch_id"], now
                )
                result = self._task_dict(
                    connection.execute(
                        "SELECT * FROM generation_tasks WHERE id = ?",
                        (task_id,),
                    ).fetchone()
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _refresh_batch_state_locked(
        self,
        connection: sqlite3.Connection,
        batch_id: str,
        now: str,
    ) -> None:
        batch = connection.execute(
            "SELECT * FROM generation_batches WHERE id = ?",
            (batch_id,),
        ).fetchone()
        if not batch:
            return
        counts = {
            row["status"]: row["count"]
            for row in connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM generation_tasks
                WHERE batch_id = ?
                GROUP BY status
                """,
                (batch_id,),
            ).fetchall()
        }
        non_terminal = sum(
            counts.get(status, 0)
            for status in ("pending", "ready", "running", "retry_waiting")
        )
        if non_terminal or batch["status"] in {"cancelled", "budget_blocked"}:
            return
        failed = counts.get("failed", 0) + counts.get("blocked", 0)
        next_status = "partially_failed" if failed else "completed"
        connection.execute(
            """
            UPDATE generation_batches
            SET status = ?, finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (next_status, now, now, batch_id),
        )
        self._restore_poem_states_locked(connection, batch_id, now)

    def _restore_poem_states_locked(
        self,
        connection: sqlite3.Connection,
        batch_id: str,
        now: str,
    ) -> None:
        poem_ids = [
            row["poem_id"]
            for row in connection.execute(
                "SELECT DISTINCT poem_id FROM generation_tasks WHERE batch_id = ?",
                (batch_id,),
            ).fetchall()
        ]
        for poem_id in poem_ids:
            counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM generation_tasks
                    WHERE batch_id = ? AND poem_id = ?
                    GROUP BY status
                    """,
                    (batch_id, poem_id),
                ).fetchall()
            }
            image_counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM production_images
                    WHERE batch_id = ? AND poem_id = ?
                    GROUP BY status
                    """,
                    (batch_id, poem_id),
                ).fetchall()
            }
            if image_counts.get("final_candidate", 0):
                poem_status = "final_review"
                blocked_reason = ""
            elif any(
                image_counts.get(status, 0)
                for status in ("review_ready", "selected")
            ):
                poem_status = "candidate_review"
                blocked_reason = ""
            elif any(
                image_counts.get(status, 0)
                for status in ("qc_blocked", "needs_manual_qc")
            ):
                poem_status = "blocked"
                blocked_reason = "候选未通过自动质检或需要人工 QC。"
            elif counts.get("succeeded", 0):
                # Compatibility for tasks completed before ProductionImage v3.
                poem_status = "candidate_review"
                blocked_reason = ""
            elif counts.get("blocked", 0):
                poem_status = "blocked"
                blocked_reason = "生成任务需要人工核对。"
            else:
                poem_status = "ready_for_production"
                blocked_reason = ""
            connection.execute(
                """
                UPDATE poems
                SET status = ?, blocked_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (poem_status, blocked_reason, now, poem_id),
            )

    def execution_state(self, batch_id: str) -> dict[str, Any]:
        batch = self.batch(batch_id)
        with self._connect() as connection:
            counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM generation_tasks
                    WHERE batch_id = ?
                    GROUP BY status
                    """,
                    (batch_id,),
                ).fetchall()
            }
            next_retry = connection.execute(
                """
                SELECT MIN(retry_at) AS retry_at
                FROM generation_tasks
                WHERE batch_id = ? AND status = 'retry_waiting'
                """,
                (batch_id,),
            ).fetchone()["retry_at"]
        done = sum(
            counts.get(status, 0) for status in ("succeeded", "failed", "blocked")
        )
        batch["progress"] = (
            round(done / batch["task_count"] * 100) if batch["task_count"] else 0
        )
        return {"batch": batch, "counts": counts, "next_retry_at": next_retry}

    def snapshot(self, project_id: str = DEFAULT_PROJECT_ID) -> dict[str, Any]:
        task_page = self.task_page(project_id=project_id, limit=50)
        return {
            "summary": self.summary(project_id),
            "production_report": self.production_report(project_id),
            "poems": self.list_poems(project_id)["items"],
            "requirements": self.requirements(project_id),
            "requirement_generation_failures": self.requirement_generation_runs(
                project_id,
                status="failed",
                unresolved_only=True,
            ),
            "requirement_schema": {
                "schema_version": REQUIREMENT_SCHEMA_VERSION,
                "generator_version": REQUIREMENT_GENERATOR_VERSION,
            },
            "directions": self.directions(project_id),
            "instruction": self.instruction(project_id),
            "instruction_versions": self.instructions(project_id),
            "style_packs": self.style_pack_versions(project_id),
            "batches": self.batches(project_id),
            "tasks": self.tasks(project_id=project_id, limit=300),
            "task_page": task_page,
            "budget": self.budget_policy(project_id),
            "review_queue": self.review_queue(project_id, include_blocked=True),
            "rework_orders": self.rework_orders(project_id),
            "final_assets": self.final_assets(project_id, current_only=False),
            "export_packages": self.export_packages(project_id),
            "workflow": {
                "poem_statuses": sorted(POEM_STATUSES),
                "requirement_statuses": sorted(REQUIREMENT_STATUSES),
                "direction_statuses": sorted(DIRECTION_STATUSES),
                "instruction_statuses": sorted(INSTRUCTION_STATUSES),
                "style_pack_statuses": sorted(STYLE_PACK_STATUSES),
                "batch_statuses": sorted(BATCH_STATUSES),
                "task_statuses": sorted(TASK_STATUSES),
                "production_image_statuses": sorted(PRODUCTION_IMAGE_STATUSES),
            },
        }

    def _poem_rows(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        poem_ids: Iterable[str],
    ) -> dict[str, sqlite3.Row]:
        unique_ids = list(dict.fromkeys(str(item) for item in poem_ids))[:300]
        if not unique_ids:
            raise WorkflowError("EMPTY_SELECTION", "请至少选择一首诗。")
        placeholders = ",".join("?" for _ in unique_ids)
        rows = connection.execute(
            f"""
            SELECT *
            FROM poems
            WHERE project_id = ? AND id IN ({placeholders})
            """,
            [project_id, *unique_ids],
        ).fetchall()
        indexed = {row["id"]: row for row in rows}
        if len(indexed) != len(unique_ids):
            missing = [item for item in unique_ids if item not in indexed]
            raise WorkflowError(
                "POEM_NOT_FOUND",
                f"项目中不存在以下诗词：{', '.join(missing[:5])}",
                status=404,
            )
        return indexed

    @staticmethod
    def _requirement_content(
        poem: sqlite3.Row,
        content_version: sqlite3.Row | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        imagery = _decode(poem["imagery_json"], [])
        must_have = imagery[:3] or [poem["theme"]]
        source = dict(content_version) if content_version is not None else dict(poem)
        poem_quote = "，".join(_decode(source.get("lines_json"), []))
        return {
            "theme": poem["theme"],
            "mood": poem["mood"],
            "time_and_place": "根据原诗与注释确认；不确定处保留人工复核标记",
            "subject": "以诗中核心意象为主体，人物仅在叙事需要时出现",
            "core_imagery": imagery,
            "composition": "建立清楚的前中后景，保留诗文排版安全区",
            "must_have": must_have,
            "avoid": ["画面文字", "现代器物", "夸张仙侠特效"],
            "historical_risks": ["服饰、建筑与器物需符合唐代语境"],
            "uncertainties": ["人物身份与具体地点若无明确依据，不作事实化描绘"],
            "evidence": [
                {
                    "source": "original_poem",
                    "quote": poem_quote,
                    "supports": [
                        "theme",
                        "mood",
                        "core_imagery",
                        "must_have",
                    ],
                }
            ],
            "confidence": {
                "time_and_place": {
                    "score": 0.52,
                    "level": "low",
                    "basis": "原诗提供时间氛围，但具体地点通常未被直接指明",
                    "requires_review": True,
                },
                "subject": {
                    "score": 0.74,
                    "level": "medium",
                    "basis": "主体由原诗核心意象和题材共同推导",
                    "requires_review": False,
                },
                "composition": {
                    "score": 0.68,
                    "level": "medium",
                    "basis": "构图是生产建议，不作为诗文事实",
                    "requires_review": False,
                },
                "historical_risks": {
                    "score": 0.45,
                    "level": "low",
                    "basis": "尚未接入逐项唐代史料知识卡，需内容编辑复核",
                    "requires_review": True,
                },
            },
            "editor_note": "",
            "locked_fields": [],
        }

    def _generate_requirement_candidate(
        self,
        poem: sqlite3.Row,
        content_version: sqlite3.Row,
        instruction: dict[str, Any],
    ) -> dict[str, Any]:
        del instruction
        return self._requirement_content(poem, content_version)

    def generate_requirements(
        self,
        project_id: str,
        poem_ids: Iterable[str],
        *,
        preserve_locked: bool = True,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _, actor_role = self._actor(actor)
        if actor_role not in {"content_editor", "producer", "system_admin"}:
            raise WorkflowError(
                "ROLE_FORBIDDEN",
                "只有内容编辑、制片人或系统管理员可以生成需求卡。",
                status=403,
            )
        results: list[dict[str, Any]] = []
        instruction = self.instruction(project_id)
        if not instruction:
            raise WorkflowError(
                "INSTRUCTION_REQUIRED",
                "项目没有已发布的全局 AI 指令。",
                status=409,
            )
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                poem_rows = self._poem_rows(connection, project_id, poem_ids)
                actor_id, _ = self._actor(actor)
                for poem_id, poem in poem_rows.items():
                    if poem["status"] in {
                        "generating",
                        "candidate_review",
                        "final_review",
                        "approved",
                        "exported",
                    }:
                        results.append(
                            {
                                "poem_id": poem_id,
                                "ok": False,
                                "code": "DOWNSTREAM_LOCKED",
                                "message": "诗词已进入下游生产，不能直接重生成需求。",
                            }
                        )
                        continue
                    now = utc_now()
                    run_id = _new_id("reqrun")
                    content_version = connection.execute(
                        """
                        SELECT * FROM content_versions
                        WHERE poem_id = ? AND status = 'approved'
                        ORDER BY version DESC LIMIT 1
                        """,
                        (poem_id,),
                    ).fetchone()
                    if not content_version:
                        input_hash = hashlib.sha256(
                            f"{REQUIREMENT_SCHEMA_VERSION}|{REQUIREMENT_GENERATOR_VERSION}|{poem_id}|missing-content".encode(
                                "utf-8"
                            )
                        ).hexdigest()
                        validation = {
                            "schema_version": REQUIREMENT_SCHEMA_VERSION,
                            "valid": False,
                            "repair_attempts": 0,
                            "initial_issues": [],
                            "final_issues": [
                                {
                                    "path": "$.content_version",
                                    "code": "APPROVED_CONTENT_REQUIRED",
                                    "message": "需求生成前必须存在已批准 ContentVersion。",
                                }
                            ],
                        }
                        self._record_requirement_run_locked(
                            connection,
                            run_id=run_id,
                            project_id=project_id,
                            poem_id=poem_id,
                            content_version_id="",
                            instruction_id=instruction["id"],
                            input_hash=input_hash,
                            status="failed",
                            cache_hit=False,
                            repair_attempts=0,
                            raw_output=None,
                            normalized_output=None,
                            validation=validation,
                            error_code="APPROVED_CONTENT_REQUIRED",
                            error_message="需求生成前必须批准内容版本。",
                            requirement_id=None,
                            actor_id=actor_id,
                            now=now,
                        )
                        connection.execute(
                            "UPDATE poems SET status = 'blocked', blocked_reason = ?, updated_at = ? WHERE id = ?",
                            ("缺少已批准内容版本，无法生成需求卡。", now, poem_id),
                        )
                        self._audit(
                            connection,
                            actor=actor,
                            action="requirement.generation_failed",
                            target_type="requirement_generation_run",
                            target_id=run_id,
                            after={
                                "poem_id": poem_id,
                                "error_code": "APPROVED_CONTENT_REQUIRED",
                            },
                        )
                        results.append(
                            {
                                "poem_id": poem_id,
                                "ok": False,
                                "run_id": run_id,
                                "code": "APPROVED_CONTENT_REQUIRED",
                                "message": "需求生成前必须批准内容版本。",
                            }
                        )
                        continue

                    input_hash = self._requirement_input_hash(
                        content_version, instruction
                    )
                    cached = connection.execute(
                        """
                        SELECT * FROM requirement_generation_runs
                        WHERE input_hash = ? AND schema_version = ?
                          AND generator_version = ? AND status = 'succeeded'
                          AND normalized_output_json IS NOT NULL
                        ORDER BY completed_at DESC LIMIT 1
                        """,
                        (
                            input_hash,
                            REQUIREMENT_SCHEMA_VERSION,
                            REQUIREMENT_GENERATOR_VERSION,
                        ),
                    ).fetchone()
                    cache_hit = bool(cached)
                    if cached:
                        raw_output = _decode(cached["normalized_output_json"], {})
                    else:
                        try:
                            raw_output = self._generate_requirement_candidate(
                                poem, content_version, instruction
                            )
                        except Exception as exc:  # provider/planner isolation per poem
                            validation = {
                                "schema_version": REQUIREMENT_SCHEMA_VERSION,
                                "valid": False,
                                "repair_attempts": 0,
                                "initial_issues": [],
                                "final_issues": [],
                            }
                            self._record_requirement_run_locked(
                                connection,
                                run_id=run_id,
                                project_id=project_id,
                                poem_id=poem_id,
                                content_version_id=content_version["id"],
                                instruction_id=instruction["id"],
                                input_hash=input_hash,
                                status="failed",
                                cache_hit=False,
                                repair_attempts=0,
                                raw_output=None,
                                normalized_output=None,
                                validation=validation,
                                error_code="REQUIREMENT_GENERATOR_FAILED",
                                error_message=str(exc),
                                requirement_id=None,
                                actor_id=actor_id,
                                now=now,
                            )
                            connection.execute(
                                "UPDATE poems SET status = 'blocked', blocked_reason = ?, updated_at = ? WHERE id = ?",
                                ("需求生成器调用失败，请在异常中心重试。", now, poem_id),
                            )
                            self._audit(
                                connection,
                                actor=actor,
                                action="requirement.generation_failed",
                                target_type="requirement_generation_run",
                                target_id=run_id,
                                after={
                                    "poem_id": poem_id,
                                    "error_code": "REQUIREMENT_GENERATOR_FAILED",
                                },
                            )
                            results.append(
                                {
                                    "poem_id": poem_id,
                                    "ok": False,
                                    "run_id": run_id,
                                    "code": "REQUIREMENT_GENERATOR_FAILED",
                                    "message": "需求生成器调用失败。",
                                }
                            )
                            continue

                    source_text = "，".join(
                        _decode(content_version["lines_json"], [])
                    )
                    normalized_output, validation = validate_with_single_repair(
                        raw_output,
                        source_text=source_text,
                    )
                    if not validation["valid"]:
                        issues = validation.get("final_issues", [])
                        self._record_requirement_run_locked(
                            connection,
                            run_id=run_id,
                            project_id=project_id,
                            poem_id=poem_id,
                            content_version_id=content_version["id"],
                            instruction_id=instruction["id"],
                            input_hash=input_hash,
                            status="failed",
                            cache_hit=cache_hit,
                            repair_attempts=validation["repair_attempts"],
                            raw_output=raw_output,
                            normalized_output=normalized_output,
                            validation=validation,
                            error_code="REQUIREMENT_SCHEMA_INVALID",
                            error_message=(issues[0]["message"] if issues else "Schema 校验失败。"),
                            requirement_id=None,
                            actor_id=actor_id,
                            now=now,
                        )
                        connection.execute(
                            "UPDATE poems SET status = 'blocked', blocked_reason = ?, updated_at = ? WHERE id = ?",
                            ("需求输出连续两次未通过 RequirementCard Schema。", now, poem_id),
                        )
                        self._audit(
                            connection,
                            actor=actor,
                            action="requirement.generation_failed",
                            target_type="requirement_generation_run",
                            target_id=run_id,
                            after={
                                "poem_id": poem_id,
                                "error_code": "REQUIREMENT_SCHEMA_INVALID",
                                "issues": issues[:8],
                            },
                        )
                        results.append(
                            {
                                "poem_id": poem_id,
                                "ok": False,
                                "run_id": run_id,
                                "code": "REQUIREMENT_SCHEMA_INVALID",
                                "message": "需求输出自动修复一次后仍未通过 Schema。",
                                "issues": issues[:8],
                            }
                        )
                        continue

                    current = connection.execute(
                        """
                        SELECT *
                        FROM requirements
                        WHERE poem_id = ? AND is_current = 1
                        """,
                        (poem_id,),
                    ).fetchone()
                    content = _decode(_json(normalized_output), {})
                    preserved_fields: list[str] = []
                    if current and preserve_locked:
                        current_content = _decode(current["content_json"], {})
                        locked_fields = [
                            str(item)
                            for item in current_content.get("locked_fields", [])
                            if str(item) in REQUIREMENT_FIELDS
                            and str(item) not in {"locked_fields", "confidence", "evidence"}
                        ]
                        for field in locked_fields:
                            if field in current_content:
                                content[field] = current_content[field]
                                preserved_fields.append(field)
                        content["locked_fields"] = locked_fields
                    locked_issues = validate_requirement_card(
                        content,
                        source_text=source_text,
                    )
                    if locked_issues:
                        locked_validation = {
                            **validation,
                            "valid": False,
                            "final_issues": locked_issues,
                            "locked_overlay_invalid": True,
                        }
                        self._record_requirement_run_locked(
                            connection,
                            run_id=run_id,
                            project_id=project_id,
                            poem_id=poem_id,
                            content_version_id=content_version["id"],
                            instruction_id=instruction["id"],
                            input_hash=input_hash,
                            status="failed",
                            cache_hit=cache_hit,
                            repair_attempts=validation["repair_attempts"],
                            raw_output=raw_output,
                            normalized_output=normalized_output,
                            validation=locked_validation,
                            error_code="LOCKED_FIELD_SCHEMA_INVALID",
                            error_message=locked_issues[0]["message"],
                            requirement_id=None,
                            actor_id=actor_id,
                            now=now,
                        )
                        connection.execute(
                            "UPDATE poems SET status = 'blocked', blocked_reason = ?, updated_at = ? WHERE id = ?",
                            ("锁定字段与 RequirementCard Schema 冲突，请人工修订。", now, poem_id),
                        )
                        self._audit(
                            connection,
                            actor=actor,
                            action="requirement.generation_failed",
                            target_type="requirement_generation_run",
                            target_id=run_id,
                            after={
                                "poem_id": poem_id,
                                "error_code": "LOCKED_FIELD_SCHEMA_INVALID",
                                "issues": locked_issues[:8],
                            },
                        )
                        results.append(
                            {
                                "poem_id": poem_id,
                                "ok": False,
                                "run_id": run_id,
                                "code": "LOCKED_FIELD_SCHEMA_INVALID",
                                "message": "锁定字段与当前 Schema 冲突。",
                                "issues": locked_issues[:8],
                            }
                        )
                        continue

                    version = (
                        connection.execute(
                            """
                            SELECT COALESCE(MAX(version), 0) + 1 AS next_version
                            FROM requirements
                            WHERE poem_id = ?
                            """,
                            (poem_id,),
                        ).fetchone()["next_version"]
                    )
                    if current:
                        connection.execute(
                            "UPDATE requirements SET is_current = 0 WHERE id = ?",
                            (current["id"],),
                        )
                    requirement_id = _new_id("req")
                    if current:
                        connection.execute(
                            """
                            UPDATE directions
                            SET is_current = 0, updated_at = ?
                            WHERE poem_id = ? AND is_current = 1
                            """,
                            (utc_now(), poem_id),
                        )
                    requirement_validation = {
                        **validation,
                        "run_id": run_id,
                        "input_hash": input_hash,
                        "cache_hit": cache_hit,
                        "locked_fields_preserved": preserved_fields,
                    }
                    connection.execute(
                        """
                        INSERT INTO requirements(
                            id, poem_id, instruction_id, version, is_current,
                            content_json, status, created_by, created_at, updated_at,
                            content_version_id, schema_version, generator_version,
                            input_hash, cache_hit, validation_json
                        ) VALUES (?, ?, ?, ?, 1, ?, 'in_review', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            requirement_id,
                            poem_id,
                            instruction["id"],
                            version,
                            _json(content),
                            actor_id,
                            now,
                            now,
                            content_version["id"],
                            REQUIREMENT_SCHEMA_VERSION,
                            REQUIREMENT_GENERATOR_VERSION,
                            input_hash,
                            int(cache_hit),
                            _json(requirement_validation),
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE poems
                        SET status = 'requirement_review', blocked_reason = '',
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (now, poem_id),
                    )
                    after = {
                        "id": requirement_id,
                        "version": version,
                        "status": "in_review",
                        "content": content,
                        "schema_version": REQUIREMENT_SCHEMA_VERSION,
                        "generator_version": REQUIREMENT_GENERATOR_VERSION,
                        "content_version_id": content_version["id"],
                        "instruction_id": instruction["id"],
                        "input_hash": input_hash,
                        "cache_hit": cache_hit,
                    }
                    self._record_requirement_run_locked(
                        connection,
                        run_id=run_id,
                        project_id=project_id,
                        poem_id=poem_id,
                        content_version_id=content_version["id"],
                        instruction_id=instruction["id"],
                        input_hash=input_hash,
                        status="succeeded",
                        cache_hit=cache_hit,
                        repair_attempts=validation["repair_attempts"],
                        raw_output=raw_output,
                        normalized_output=normalized_output,
                        validation=requirement_validation,
                        error_code="",
                        error_message="",
                        requirement_id=requirement_id,
                        actor_id=actor_id,
                        now=now,
                    )
                    connection.execute(
                        """
                        UPDATE requirement_generation_runs
                        SET resolved_at = ?
                        WHERE poem_id = ? AND status = 'failed'
                          AND resolved_at IS NULL AND id != ?
                        """,
                        (now, poem_id, run_id),
                    )
                    self._audit(
                        connection,
                        actor=actor,
                        action="requirement.generated",
                        target_type="requirement",
                        target_id=requirement_id,
                        before=dict(current) if current else None,
                        after=after,
                    )
                    results.append(
                        {
                            "poem_id": poem_id,
                            "ok": True,
                            "requirement_id": requirement_id,
                            "run_id": run_id,
                            "version": version,
                            "preserved_fields": preserved_fields,
                            "cache_hit": cache_hit,
                            "repair_attempts": validation["repair_attempts"],
                            "input_hash": input_hash,
                        }
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {
            "results": results,
            "succeeded": sum(1 for item in results if item["ok"]),
            "failed": sum(1 for item in results if not item["ok"]),
        }

    def revise_requirement(
        self,
        requirement_id: str,
        changes: dict[str, Any],
        *,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _, actor_role = self._actor(actor)
        if actor_role not in {"content_editor", "producer", "system_admin"}:
            raise WorkflowError(
                "ROLE_FORBIDDEN",
                "只有内容编辑、制片人或系统管理员可以修订需求卡。",
                status=403,
            )
        unknown = set(changes) - REQUIREMENT_FIELDS
        if unknown:
            raise WorkflowError(
                "INVALID_FIELDS",
                f"需求卡包含不支持的字段：{', '.join(sorted(unknown))}",
            )
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT *
                    FROM requirements
                    WHERE id = ? AND is_current = 1
                    """,
                    (requirement_id,),
                ).fetchone()
                if not row:
                    raise WorkflowError(
                        "REQUIREMENT_NOT_FOUND",
                        "当前需求版本不存在。",
                        status=404,
                    )
                if row["status"] == "approved":
                    raise WorkflowError(
                        "APPROVED_REQUIREMENT_LOCKED",
                        "已批准需求不可覆盖，请先退回后再修订。",
                        status=409,
                    )
                content = _decode(row["content_json"], {})
                content.update(changes)
                content_version = connection.execute(
                    "SELECT * FROM content_versions WHERE id = ?",
                    (row["content_version_id"],),
                ).fetchone()
                if not content_version:
                    content_version = connection.execute(
                        """
                        SELECT * FROM content_versions
                        WHERE poem_id = ? AND status = 'approved'
                        ORDER BY version DESC LIMIT 1
                        """,
                        (row["poem_id"],),
                    ).fetchone()
                if not content_version:
                    raise WorkflowError(
                        "APPROVED_CONTENT_REQUIRED",
                        "修订需求卡前必须存在已批准 ContentVersion。",
                        status=409,
                    )
                source_text = "，".join(
                    _decode(content_version["lines_json"], [])
                )
                issues = validate_requirement_card(content, source_text=source_text)
                if issues:
                    first = issues[0]
                    raise WorkflowError(
                        "REQUIREMENT_SCHEMA_INVALID",
                        f"需求卡未通过 Schema：{first['path']} {first['message']}",
                    )
                connection.execute(
                    "UPDATE requirements SET is_current = 0 WHERE id = ?",
                    (requirement_id,),
                )
                new_id = _new_id("req")
                version = row["version"] + 1
                now = utc_now()
                actor_id, _ = self._actor(actor)
                connection.execute(
                    """
                    INSERT INTO requirements(
                        id, poem_id, instruction_id, version, is_current,
                        content_json, status, created_by, created_at, updated_at,
                        content_version_id, schema_version, generator_version,
                        input_hash, cache_hit, validation_json
                    ) VALUES (?, ?, ?, ?, 1, ?, 'in_review', ?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        new_id,
                        row["poem_id"],
                        row["instruction_id"],
                        version,
                        _json(content),
                        actor_id,
                        now,
                        now,
                        content_version["id"],
                        REQUIREMENT_SCHEMA_VERSION,
                        row["generator_version"],
                        row["input_hash"],
                        _json(
                            {
                                "schema_version": REQUIREMENT_SCHEMA_VERSION,
                                "valid": True,
                                "repair_attempts": 0,
                                "manual_revision": True,
                                "source_requirement_id": requirement_id,
                                "final_issues": [],
                            }
                        ),
                    ),
                )
                connection.execute(
                    """
                    UPDATE poems
                    SET status = 'requirement_review', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, row["poem_id"]),
                )
                result = {
                    "id": new_id,
                    "poem_id": row["poem_id"],
                    "instruction_id": row["instruction_id"],
                    "version": version,
                    "is_current": 1,
                    "content": content,
                    "status": "in_review",
                    "rejection_reason": "",
                    "created_by": actor_id,
                    "approved_by": None,
                    "created_at": now,
                    "updated_at": now,
                    "content_version_id": content_version["id"],
                    "schema_version": REQUIREMENT_SCHEMA_VERSION,
                    "generator_version": row["generator_version"],
                    "input_hash": row["input_hash"],
                    "cache_hit": False,
                    "validation": {
                        "schema_version": REQUIREMENT_SCHEMA_VERSION,
                        "valid": True,
                        "repair_attempts": 0,
                        "manual_revision": True,
                        "source_requirement_id": requirement_id,
                        "final_issues": [],
                    },
                }
                self._audit(
                    connection,
                    actor=actor,
                    action="requirement.revised",
                    target_type="requirement",
                    target_id=new_id,
                    before=self._requirement_dict(row),
                    after=result,
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def decide_requirement(
        self,
        requirement_id: str,
        decision: str,
        *,
        reason: str = "",
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if decision not in {"approve", "reject"}:
            raise WorkflowError("INVALID_DECISION", "不支持的需求审核结论。")
        _, actor_role = self._actor(actor)
        if actor_role not in {"content_editor", "producer", "system_admin"}:
            raise WorkflowError(
                "ROLE_FORBIDDEN",
                "只有内容编辑、制片人或系统管理员可以审核需求。",
                status=403,
            )
        reason = reason.strip()[:500]
        if decision == "reject" and not reason:
            raise WorkflowError("REASON_REQUIRED", "退回需求时必须填写原因。")
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT *
                    FROM requirements
                    WHERE id = ? AND is_current = 1
                    """,
                    (requirement_id,),
                ).fetchone()
                if not row:
                    raise WorkflowError(
                        "REQUIREMENT_NOT_FOUND",
                        "当前需求版本不存在。",
                        status=404,
                    )
                if row["status"] not in {"in_review", "rejected"}:
                    raise WorkflowError(
                        "INVALID_REQUIREMENT_STATE",
                        "只有待审核或已退回需求可以执行此操作。",
                        status=409,
                    )
                now = utc_now()
                actor_id, _ = self._actor(actor)
                next_status = "approved" if decision == "approve" else "rejected"
                poem_status = (
                    "direction_draft"
                    if decision == "approve"
                    else "requirement_draft"
                )
                connection.execute(
                    """
                    UPDATE requirements
                    SET status = ?, rejection_reason = ?, approved_by = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        next_status,
                        "" if decision == "approve" else reason,
                        actor_id if decision == "approve" else None,
                        now,
                        requirement_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE poems
                    SET status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (poem_status, now, row["poem_id"]),
                )
                result = self._requirement_dict(
                    connection.execute(
                        "SELECT * FROM requirements WHERE id = ?",
                        (requirement_id,),
                    ).fetchone()
                )
                self._audit(
                    connection,
                    actor=actor,
                    action=f"requirement.{next_status}",
                    target_type="requirement",
                    target_id=requirement_id,
                    before=self._requirement_dict(row),
                    after=result,
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def bulk_decide_requirements(
        self,
        requirement_ids: Iterable[str],
        decision: str,
        *,
        reason: str = "",
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ids = list(dict.fromkeys(str(item) for item in requirement_ids))[:300]
        if not ids:
            raise WorkflowError("EMPTY_SELECTION", "请至少选择一条需求。")
        results: list[dict[str, Any]] = []
        for requirement_id in ids:
            try:
                requirement = self.decide_requirement(
                    requirement_id,
                    decision,
                    reason=reason,
                    actor=actor,
                )
                results.append(
                    {
                        "requirement_id": requirement_id,
                        "poem_id": requirement["poem_id"],
                        "ok": True,
                        "status": requirement["status"],
                    }
                )
            except WorkflowError as exc:
                results.append(
                    {
                        "requirement_id": requirement_id,
                        "ok": False,
                        "code": exc.code,
                        "message": str(exc),
                    }
                )
        return {
            "decision": decision,
            "results": results,
            "succeeded": sum(1 for item in results if item["ok"]),
            "failed": sum(1 for item in results if not item["ok"]),
        }

    @staticmethod
    def _direction_content(
        poem: sqlite3.Row,
        requirement: sqlite3.Row,
        direction_type: str,
    ) -> dict[str, Any]:
        requirement_content = _decode(requirement["content_json"], {})
        imagery = requirement_content.get("core_imagery") or _decode(
            poem["imagery_json"], []
        )
        first = imagery[0] if imagery else poem["theme"]
        second = imagery[1] if len(imagery) > 1 else "自然环境"
        templates = {
            "narrative": {
                "title": f"{first} · 叙事场景",
                "subject": "以诗中人物行动或事件为画面主线",
                "shot": "中远景，人物与环境关系清楚",
                "foreground": f"以{second}建立空间入口",
                "midground": "核心人物或叙事动作",
                "background": "符合诗意时空的远景",
                "action": "动作克制，避免舞台化表演",
                "whitespace": "中等留白",
            },
            "atmospheric": {
                "title": f"{first} · 意境留白",
                "subject": "自然意象为主体，人物弱化或不出现",
                "shot": "远景或大全景，强调空间与气候",
                "foreground": "少量近景作为尺度",
                "midground": f"突出{first}与{second}的关系",
                "background": "大面积天空、水面或山势",
                "action": "依靠光、雾、风或水面变化表达情绪",
                "whitespace": "高留白",
            },
            "symbolic": {
                "title": f"{first} · 象征构成",
                "subject": "提炼一个核心意象作为视觉焦点",
                "shot": "近中景结合，构图更凝练",
                "foreground": "象征性纹理或局部器物",
                "midground": f"放大{first}的视觉重量",
                "background": "简化为色块与含蓄空间线索",
                "action": "以物喻情，不把隐喻误作历史事实",
                "whitespace": "中高留白",
            },
        }
        content = templates[direction_type]
        content.update(
            {
                "type": direction_type,
                "lighting": "自然、克制，服务诗中时间与情绪",
                "palette": "低至中饱和，遵循项目风格包",
                "preserve": list(requirement_content.get("must_have", []))[:4],
                "avoid": list(requirement_content.get("avoid", []))[:8],
                "risk_note": "涉及人物、服饰、器物和建筑时进入历史复核",
                "art_director_note": "",
                "locked_fields": [],
            }
        )
        return content

    def generate_directions(
        self,
        project_id: str,
        poem_ids: Iterable[str],
        *,
        preserve_locked: bool = True,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _, actor_role = self._actor(actor)
        if actor_role not in {"art_director", "producer", "system_admin"}:
            raise WorkflowError(
                "ROLE_FORBIDDEN",
                "只有美术指导、制片人或系统管理员可以生成画面方向。",
                status=403,
            )
        results: list[dict[str, Any]] = []
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                poem_rows = self._poem_rows(connection, project_id, poem_ids)
                actor_id, _ = self._actor(actor)
                for poem_id, poem in poem_rows.items():
                    production_count = connection.execute(
                        "SELECT COUNT(*) AS count FROM generation_tasks WHERE poem_id = ?",
                        (poem_id,),
                    ).fetchone()["count"]
                    if production_count:
                        results.append(
                            {
                                "poem_id": poem_id,
                                "ok": False,
                                "code": "DIRECTION_IN_PRODUCTION",
                                "message": "该诗已有生产任务，方向版本已冻结；请从候选图创建返工单。",
                            }
                        )
                        continue
                    requirement = connection.execute(
                        """
                        SELECT *
                        FROM requirements
                        WHERE poem_id = ? AND is_current = 1
                          AND status = 'approved'
                        """,
                        (poem_id,),
                    ).fetchone()
                    if not requirement:
                        results.append(
                            {
                                "poem_id": poem_id,
                                "ok": False,
                                "code": "APPROVED_REQUIREMENT_REQUIRED",
                                "message": "先批准需求卡，再生成画面方向。",
                            }
                        )
                        continue
                    now = utc_now()
                    generated: list[str] = []
                    preserved_by_type: dict[str, list[str]] = {}
                    for direction_type in DIRECTION_TYPES:
                        current = connection.execute(
                            """
                            SELECT *
                            FROM directions
                            WHERE poem_id = ? AND type = ? AND is_current = 1
                            """,
                            (poem_id, direction_type),
                        ).fetchone()
                        version = connection.execute(
                            """
                            SELECT COALESCE(MAX(version), 0) + 1 AS next_version
                            FROM directions
                            WHERE poem_id = ? AND type = ?
                            """,
                            (poem_id, direction_type),
                        ).fetchone()["next_version"]
                        if current:
                            connection.execute(
                                "UPDATE directions SET is_current = 0 WHERE id = ?",
                                (current["id"],),
                            )
                        direction_id = _new_id("dir")
                        content = self._direction_content(
                            poem, requirement, direction_type
                        )
                        preserved_fields: list[str] = []
                        if current and preserve_locked:
                            current_content = _decode(current["content_json"], {})
                            locked_fields = [
                                str(field)
                                for field in current_content.get("locked_fields", [])
                                if str(field) in DIRECTION_FIELDS
                            ]
                            for field in locked_fields:
                                if field in current_content:
                                    content[field] = current_content[field]
                                    preserved_fields.append(field)
                            content["locked_fields"] = locked_fields
                        preserved_by_type[direction_type] = preserved_fields
                        connection.execute(
                            """
                            INSERT INTO directions(
                                id, poem_id, requirement_id, version, type,
                                is_current, content_json, status, created_by,
                                created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, 1, ?, 'in_review', ?, ?, ?)
                            """,
                            (
                                direction_id,
                                poem_id,
                                requirement["id"],
                                version,
                                direction_type,
                                _json(content),
                                actor_id,
                                now,
                                now,
                            ),
                        )
                        self._audit(
                            connection,
                            actor=actor,
                            action="direction.generated",
                            target_type="direction",
                            target_id=direction_id,
                            before=dict(current) if current else None,
                            after={
                                "id": direction_id,
                                "type": direction_type,
                                "version": version,
                                "status": "in_review",
                                "content": content,
                            },
                        )
                        generated.append(direction_id)
                    connection.execute(
                        """
                        UPDATE poems
                        SET status = 'direction_review', updated_at = ?
                        WHERE id = ?
                        """,
                        (now, poem_id),
                    )
                    results.append(
                        {
                            "poem_id": poem_id,
                            "ok": True,
                            "direction_ids": generated,
                            "preserved_fields": preserved_by_type,
                        }
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {
            "results": results,
            "succeeded": sum(1 for item in results if item["ok"]),
            "failed": sum(1 for item in results if not item["ok"]),
        }

    def decide_direction(
        self,
        direction_id: str,
        decision: str,
        *,
        reason: str = "",
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if decision not in {"approve", "reject"}:
            raise WorkflowError("INVALID_DECISION", "不支持的方向审核结论。")
        _, actor_role = self._actor(actor)
        if actor_role not in {"art_director", "producer", "system_admin"}:
            raise WorkflowError(
                "ROLE_FORBIDDEN",
                "只有美术指导、制片人或系统管理员可以审核画面方向。",
                status=403,
            )
        reason = reason.strip()[:500]
        if decision == "reject" and not reason:
            raise WorkflowError("REASON_REQUIRED", "退回方向时必须填写原因。")
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT *
                    FROM directions
                    WHERE id = ? AND is_current = 1
                    """,
                    (direction_id,),
                ).fetchone()
                if not row:
                    raise WorkflowError(
                        "DIRECTION_NOT_FOUND",
                        "当前方向版本不存在。",
                        status=404,
                    )
                if row["status"] not in {"in_review", "rejected"}:
                    raise WorkflowError(
                        "INVALID_DIRECTION_STATE",
                        "只有待审核或已退回方向可以执行此操作。",
                        status=409,
                    )
                now = utc_now()
                actor_id, _ = self._actor(actor)
                next_status = "approved" if decision == "approve" else "rejected"
                connection.execute(
                    """
                    UPDATE directions
                    SET status = ?, rejection_reason = ?, approved_by = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        next_status,
                        "" if decision == "approve" else reason,
                        actor_id if decision == "approve" else None,
                        now,
                        direction_id,
                    ),
                )
                approved_count = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM directions
                    WHERE poem_id = ? AND is_current = 1
                      AND status = 'approved'
                    """,
                    (row["poem_id"],),
                ).fetchone()["count"]
                pending_count = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM directions
                    WHERE poem_id = ? AND is_current = 1
                      AND status = 'in_review'
                    """,
                    (row["poem_id"],),
                ).fetchone()["count"]
                poem_status = (
                    "ready_for_production"
                    if approved_count > 0
                    else ("direction_review" if pending_count > 0 else "direction_draft")
                )
                connection.execute(
                    """
                    UPDATE poems
                    SET status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (poem_status, now, row["poem_id"]),
                )
                result = self._direction_dict(
                    connection.execute(
                        "SELECT * FROM directions WHERE id = ?",
                        (direction_id,),
                    ).fetchone()
                )
                self._audit(
                    connection,
                    actor=actor,
                    action=f"direction.{next_status}",
                    target_type="direction",
                    target_id=direction_id,
                    before=self._direction_dict(row),
                    after=result,
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def bulk_decide_directions(
        self,
        direction_ids: Iterable[str],
        decision: str,
        *,
        reason: str = "",
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ids = list(dict.fromkeys(str(item) for item in direction_ids))[:300]
        if not ids:
            raise WorkflowError("EMPTY_SELECTION", "请至少选择一个画面方向。")
        results: list[dict[str, Any]] = []
        for direction_id in ids:
            try:
                direction = self.decide_direction(
                    direction_id,
                    decision,
                    reason=reason,
                    actor=actor,
                )
                results.append(
                    {
                        "direction_id": direction_id,
                        "poem_id": direction["poem_id"],
                        "ok": True,
                        "status": direction["status"],
                    }
                )
            except WorkflowError as exc:
                results.append(
                    {
                        "direction_id": direction_id,
                        "ok": False,
                        "code": exc.code,
                        "message": str(exc),
                    }
                )
        return {
            "decision": decision,
            "results": results,
            "succeeded": sum(1 for item in results if item["ok"]),
            "failed": sum(1 for item in results if not item["ok"]),
        }

    @staticmethod
    def _normalize_direction_content(
        source: dict[str, Any], updates: dict[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(updates, dict):
            raise WorkflowError("INVALID_DIRECTION_CONTENT", "方向内容必须是对象。")
        allowed_text = {
            "title",
            "subject",
            "shot",
            "foreground",
            "midground",
            "background",
            "action",
            "lighting",
            "palette",
            "whitespace",
            "risk_note",
            "art_director_note",
        }
        allowed_lists = {"preserve", "avoid", "locked_fields"}
        unknown = set(updates) - allowed_text - allowed_lists - {"type"}
        if unknown:
            raise WorkflowError(
                "INVALID_DIRECTION_FIELD",
                f"方向包含不支持的字段：{', '.join(sorted(unknown))}",
            )
        result = dict(source)
        locked_fields = {
            str(item) for item in source.get("locked_fields", []) if str(item)
        }
        for key in locked_fields:
            if key in updates and updates[key] != source.get(key):
                raise WorkflowError(
                    "DIRECTION_FIELD_LOCKED",
                    f"字段 {key} 已锁定，不能在修订中覆盖。",
                    status=409,
                )
        for key in allowed_text:
            if key in updates:
                result[key] = str(updates.get(key) or "").strip()[:1000]
        for key in allowed_lists:
            if key in updates:
                value = updates.get(key)
                if not isinstance(value, list):
                    raise WorkflowError(
                        "INVALID_DIRECTION_CONTENT", f"字段 {key} 必须是数组。"
                    )
                result[key] = [
                    str(item).strip()[:300]
                    for item in value[:30]
                    if str(item).strip()
                ]
        for required in ("title", "subject", "shot"):
            if not str(result.get(required) or "").strip():
                raise WorkflowError(
                    "DIRECTION_FIELD_REQUIRED", f"方向字段 {required} 不能为空。"
                )
        return result

    @staticmethod
    def _direction_poem_status_locked(
        connection: sqlite3.Connection, poem_id: str
    ) -> str:
        counts = {
            row["status"]: int(row["count"])
            for row in connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM directions
                WHERE poem_id = ? AND is_current = 1
                GROUP BY status
                """,
                (poem_id,),
            ).fetchall()
        }
        if counts.get("approved", 0):
            return "ready_for_production"
        if counts.get("in_review", 0):
            return "direction_review"
        return "direction_draft"

    def revise_direction(
        self,
        direction_id: str,
        content: dict[str, Any] | None = None,
        *,
        copy_source: bool = False,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        actor_id, actor_role = self._actor(actor)
        if actor_role not in {"art_director", "producer", "system_admin"}:
            raise WorkflowError(
                "ROLE_FORBIDDEN",
                "只有美术指导、制片人或系统管理员可以修订画面方向。",
                status=403,
            )
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM directions WHERE id = ? AND is_current = 1",
                    (direction_id,),
                ).fetchone()
                if not row:
                    raise WorkflowError(
                        "DIRECTION_NOT_FOUND", "当前方向版本不存在。", status=404
                    )
                if not copy_source and row["status"] == "disabled":
                    raise WorkflowError(
                        "INVALID_DIRECTION_STATE",
                        "停用方向只能复制为新的待审核版本。",
                        status=409,
                    )
                task_count = connection.execute(
                    "SELECT COUNT(*) AS count FROM generation_tasks WHERE poem_id = ?",
                    (row["poem_id"],),
                ).fetchone()["count"]
                if task_count:
                    raise WorkflowError(
                        "DIRECTION_LOCKED_BY_PRODUCTION",
                        "该诗已创建生产任务；方向版本已冻结，请通过结构化返工调整成图。",
                        status=409,
                    )
                source_content = _decode(row["content_json"], {})
                next_content = self._normalize_direction_content(
                    source_content, content or {}
                )
                next_version = connection.execute(
                    """
                    SELECT COALESCE(MAX(version), 0) + 1 AS next_version
                    FROM directions WHERE poem_id = ? AND type = ?
                    """,
                    (row["poem_id"], row["type"]),
                ).fetchone()["next_version"]
                now = utc_now()
                next_id = _new_id("dir")
                connection.execute(
                    "UPDATE directions SET is_current = 0, updated_at = ? WHERE id = ?",
                    (now, direction_id),
                )
                connection.execute(
                    """
                    INSERT INTO directions(
                        id, poem_id, requirement_id, version, type, is_current,
                        content_json, status, created_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, 'in_review', ?, ?, ?)
                    """,
                    (
                        next_id,
                        row["poem_id"],
                        row["requirement_id"],
                        next_version,
                        row["type"],
                        _json(next_content),
                        actor_id,
                        now,
                        now,
                    ),
                )
                poem_status = self._direction_poem_status_locked(
                    connection, row["poem_id"]
                )
                connection.execute(
                    "UPDATE poems SET status = ?, updated_at = ? WHERE id = ?",
                    (poem_status, now, row["poem_id"]),
                )
                result = self._direction_dict(
                    connection.execute(
                        "SELECT * FROM directions WHERE id = ?", (next_id,)
                    ).fetchone()
                )
                self._audit(
                    connection,
                    actor=actor,
                    action="direction.copied" if copy_source else "direction.revised",
                    target_type="direction",
                    target_id=next_id,
                    before=self._direction_dict(row),
                    after=result,
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def copy_direction(
        self,
        direction_id: str,
        *,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.revise_direction(
            direction_id, {}, copy_source=True, actor=actor
        )

    def disable_direction(
        self,
        direction_id: str,
        *,
        reason: str,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _, actor_role = self._actor(actor)
        if actor_role not in {"art_director", "producer", "system_admin"}:
            raise WorkflowError(
                "ROLE_FORBIDDEN",
                "只有美术指导、制片人或系统管理员可以停用画面方向。",
                status=403,
            )
        reason = str(reason or "").strip()[:500]
        if not reason:
            raise WorkflowError("REASON_REQUIRED", "停用方向时必须填写原因。")
        with self.lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM directions WHERE id = ? AND is_current = 1",
                    (direction_id,),
                ).fetchone()
                if not row:
                    raise WorkflowError(
                        "DIRECTION_NOT_FOUND", "当前方向版本不存在。", status=404
                    )
                if row["status"] == "disabled":
                    connection.execute("COMMIT")
                    return self._direction_dict(row)
                task_count = connection.execute(
                    "SELECT COUNT(*) AS count FROM generation_tasks WHERE poem_id = ?",
                    (row["poem_id"],),
                ).fetchone()["count"]
                if task_count:
                    raise WorkflowError(
                        "DIRECTION_LOCKED_BY_PRODUCTION",
                        "该诗已创建生产任务，不能停用其冻结方向。",
                        status=409,
                    )
                now = utc_now()
                connection.execute(
                    """
                    UPDATE directions
                    SET status = 'disabled', rejection_reason = ?, approved_by = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (reason, now, direction_id),
                )
                poem_status = self._direction_poem_status_locked(
                    connection, row["poem_id"]
                )
                connection.execute(
                    "UPDATE poems SET status = ?, updated_at = ? WHERE id = ?",
                    (poem_status, now, row["poem_id"]),
                )
                result = self._direction_dict(
                    connection.execute(
                        "SELECT * FROM directions WHERE id = ?", (direction_id,)
                    ).fetchone()
                )
                self._audit(
                    connection,
                    actor=actor,
                    action="direction.disabled",
                    target_type="direction",
                    target_id=direction_id,
                    before=self._direction_dict(row),
                    after=result,
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def audit_events(
        self,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where = []
        params: list[Any] = []
        if target_type:
            where.append("target_type = ?")
            params.append(target_type[:40])
        if target_id:
            where.append("target_id = ?")
            params.append(target_id[:100])
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM audit_events
                {clause}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                [*params, max(1, min(int(limit), 500))],
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["before"] = _decode(item.pop("before_json", None), None)
            item["after"] = _decode(item.pop("after_json", None), None)
            items.append(item)
        return items
