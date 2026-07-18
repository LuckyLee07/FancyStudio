import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class IdCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, tag, attrs):
        del tag
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.append(attributes["id"])


class FrontendContractTests(unittest.TestCase):
    def test_html_ids_are_unique_and_javascript_selectors_exist(self):
        html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        parser = IdCollector()
        parser.feed(html)

        self.assertEqual(
            len(parser.ids),
            len(set(parser.ids)),
            "index.html contains duplicate element ids",
        )
        referenced_ids = set(
            re.findall(r'querySelector(?:All)?\\("#([a-zA-Z0-9_-]+)"\\)', script)
        )
        missing = referenced_ids - set(parser.ids)
        self.assertEqual(
            missing,
            set(),
            f"app.js references ids missing from index.html: {sorted(missing)}",
        )

    def test_all_primary_views_have_navigation_entries(self):
        html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        navigation_views = set(re.findall(r'data-view="([^"]+)"', html))
        panel_views = set(re.findall(r'data-view-panel="([^"]+)"', html))
        self.assertEqual(navigation_views, panel_views)
        self.assertEqual(
            navigation_views,
            {
                "overview",
                "instructions",
                "requirements",
                "directions",
                "queue",
                "review",
                "assets",
                "resources",
            },
        )

    def test_queue_pagination_and_prompt_traceability_are_visible(self):
        html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="task-table-body"', html)
        self.assertIn('id="task-page-next"', html)
        self.assertIn("refreshTaskPage", script)
        self.assertIn("生产证据链", script)
        self.assertIn("prompt_template_version", script)
        self.assertIn("prompt_hash", script)
        self.assertIn('id="bulk-approve-directions"', html)
        self.assertIn("bulkDecideDirections", script)
        self.assertIn('id="poem-detail-dialog"', html)
        self.assertIn("openPoemDetail", script)

    def test_role_views_use_selected_actor_without_client_side_role_impersonation(self):
        html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="current-role"', html)
        for role in (
            "producer",
            "content_editor",
            "art_director",
            "ai_operator",
            "system_admin",
        ):
            self.assertIn(f'value="{role}"', html)
        self.assertIn("data-role-allow", html)
        self.assertIn("function applyRoleVisibility()", script)
        self.assertIn('localStorage.setItem("tang-sop-role"', script)
        self.assertNotRegex(script, r"\.\.\.ACTOR,\s*role\s*:")

    def test_requirement_contract_failures_are_visible_and_actionable(self):
        script = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("requirement_generation_failures", script)
        self.assertIn("requirement_schema", script)
        self.assertIn('["failed", "生成异常"]', script)
        self.assertIn("自动修复", script)
        self.assertIn("requirement_generation_runs", script)
        self.assertIn("requirement-contract-line", script)
        self.assertIn("generation-error-note", styles)

    def test_exception_center_exposes_owned_actionable_records(self):
        html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
        for element_id in (
            "open-all-blockers",
            "blocker-dialog",
            "blocker-role-filter",
            "blocker-severity-filter",
            "blocker-query",
            "blocker-summary",
            "blocker-list",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("/api/exceptions?", script)
        self.assertIn("function openBlockerDialog", script)
        self.assertIn("function routeBlocker", script)
        self.assertIn("responsible_role", script)
        self.assertIn("suggested_action", script)
        self.assertIn("blocker-record", styles)
        self.assertIn("poem-blocker-banner", styles)

    def test_direction_contract_diversity_and_failures_are_visible(self):
        html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("direction_generation_failures", script)
        self.assertIn("direction_schema", script)
        self.assertIn("direction_generation_runs", script)
        self.assertIn("direction-contract-line", script)
        self.assertIn("direction-generation-error", styles)
        for field in (
            "visual_thesis",
            "subject_mode",
            "shot_scale",
            "composition",
            "text_safe_area",
            "poem_facts",
            "reasonable_inferences",
            "creative_choices",
        ):
            self.assertIn(f'name="{field}"', html)

    def test_style_lab_exposes_art_bible_benchmark_and_release_gate(self):
        html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
        for element_id in (
            "art-bible-dialog",
            "style-benchmark-dialog",
            "style-evaluation-dialog",
            "benchmark-poem-picker",
        ):
            self.assertIn(f'id="{element_id}"', html)
        for field in (
            "semantic_version",
            "release_notes",
            "art_bible_version_id",
            "positive_examples",
            "negative_examples",
        ):
            self.assertIn(f'name="{field}"', html)
        self.assertIn("style_benchmark_poems", script)
        self.assertIn("STYLE LAB", script)
        self.assertIn("data-start-style-benchmark", script)
        self.assertIn("style-gate-note", styles)
        self.assertIn("benchmark-poem-option", styles)

    def test_visual_qc_scores_evidence_and_calibration_are_visible(self):
        script = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
        for field in (
            "poem_relevance",
            "style_match",
            "historical_plausibility",
            "composition",
            "series_consistency",
        ):
            self.assertIn(field, script)
        self.assertIn("VISUAL QC POLICY", script)
        self.assertIn("qc.evidence", script)
        self.assertIn("data-qc-calibration", script)
        self.assertIn("submitQcCalibration", script)
        self.assertIn("qc-score-grid", styles)
        self.assertIn("qc-calibration-panel", styles)

    def test_poem_import_templates_quality_report_and_source_gate_are_visible(self):
        html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")

        for element_id in (
            "import-format",
            "import-file",
            "source-dialog",
            "source-form",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("/api/templates/poem-import?format=json", html)
        self.assertIn("/api/templates/poem-import?format=csv", html)
        self.assertIn('value="csv"', html)
        for field in (
            "source_type",
            "citation",
            "license",
            "verification_status",
            "verified_at",
        ):
            self.assertIn(f'name="{field}"', html)
        self.assertIn("data_quality", script)
        self.assertIn("poem_import_schema", script)
        self.assertIn("submitPoemSource", script)
        self.assertIn("source_update", script)
        self.assertIn("data-quality-panel", styles)
        self.assertIn("poem-source-summary", styles)

    def test_content_revision_quality_drilldown_and_bulk_approval_are_visible(self):
        html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")

        for element_id in ("content-dialog", "content-form", "content-dialog-title"):
            self.assertIn(f'id="{element_id}"', html)
        for field in (
            "title",
            "author",
            "dynasty",
            "lines",
            "theme",
            "mood",
            "imagery",
            "notes",
            "change_summary",
        ):
            self.assertIn(f'name="{field}"', html)
        self.assertIn('data-action="bulk-approve-content"', html)
        self.assertIn("openContentDialog", script)
        self.assertIn("submitPoemContent", script)
        self.assertIn("bulkApproveContent", script)
        self.assertIn("data-edit-poem-content", script)
        self.assertIn("quality-record-list", styles)
        self.assertIn("content-version-history", styles)


if __name__ == "__main__":
    unittest.main()
