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


if __name__ == "__main__":
    unittest.main()
