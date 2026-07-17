import json
import unittest

from poem_import_schema import (
    POEM_IMPORT_SCHEMA_VERSION,
    PoemImportContractError,
    csv_template_text,
    json_template_document,
    normalize_source,
    parse_import_document,
    schema_document,
)


class PoemImportSchemaTests(unittest.TestCase):
    def test_versioned_json_template_and_schema_share_the_contract(self):
        schema = schema_document()
        template = json_template_document()

        self.assertEqual(template["schema_version"], POEM_IMPORT_SCHEMA_VERSION)
        self.assertEqual(schema["title"], "PoemImportDocument v1")
        records = parse_import_document(
            json.dumps(template, ensure_ascii=False), "json"
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["source"]["verification_status"], "verified")

    def test_csv_template_parses_lists_and_structured_source(self):
        records = parse_import_document(csv_template_text(), "csv")

        self.assertEqual(len(records), 1)
        self.assertEqual(len(records[0]["lines"]), 4)
        self.assertEqual(records[0]["imagery"], ["香炉峰", "瀑布", "银河"])
        self.assertEqual(records[0]["source"]["source_type"], "public_domain")

    def test_contract_rejects_unknown_csv_columns_and_schema_versions(self):
        with self.assertRaises(PoemImportContractError) as csv_error:
            parse_import_document("id,title,author,lines,magic\na,b,c,d,e\n", "csv")
        self.assertEqual(csv_error.exception.code, "IMPORT_CSV_COLUMNS_UNKNOWN")

        with self.assertRaises(PoemImportContractError) as json_error:
            parse_import_document(
                json.dumps({"schema_version": "poem-import/v9", "records": [{}]}),
                "json",
            )
        self.assertEqual(
            json_error.exception.code, "IMPORT_SCHEMA_VERSION_UNSUPPORTED"
        )

    def test_source_normalization_keeps_legacy_data_but_never_marks_it_verified(self):
        source, errors, warnings = normalize_source("旧版来源说明")

        self.assertEqual(errors, [])
        self.assertTrue(warnings)
        self.assertEqual(source["verification_status"], "unverified")
        self.assertEqual(source["license"], "needs-review")

        verified, errors, warnings = normalize_source(
            {
                "source_type": "public_domain",
                "citation": "公共领域底本",
                "license": "Public Domain",
                "verification_status": "verified",
                "verified_at": "2026-07-18",
            }
        )
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        self.assertEqual(verified["verification_status"], "verified")

    def test_verified_source_requires_complete_evidence_and_a_real_date(self):
        _, errors, _ = normalize_source(
            {
                "source_type": "public_domain",
                "citation": "",
                "license": "",
                "verification_status": "verified",
                "verified_at": "",
            }
        )

        self.assertIn("已核验来源必须填写来源引文。", errors)
        self.assertIn("已核验来源必须填写可用许可。", errors)
        self.assertIn("已核验来源必须填写 verified_at。", errors)

        _, errors, _ = normalize_source(
            {
                "source_type": "public_domain",
                "citation": "公共领域底本",
                "license": "Public Domain",
                "verification_status": "verified",
                "verified_at": "2026-02-30",
            }
        )
        self.assertIn("source.verified_at 不是有效日期。", errors)


if __name__ == "__main__":
    unittest.main()
