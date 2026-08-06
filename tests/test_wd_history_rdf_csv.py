import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import wd_history_rdf_csv as wd


SCRIPT = Path(__file__).resolve().parents[1] / "wd_history_rdf_csv.py"


def entity_json(qid, label=None, claims=True):
    data = {"type": "item", "id": qid, "labels": {}, "claims": {}}
    if label is not None:
        data["labels"]["en"] = {"language": "en", "value": label}
    if claims:
        data["claims"]["P31"] = [
            {
                "id": f"{qid}$A",
                "rank": "normal",
                "mainsnak": {
                    "snaktype": "value",
                    "property": "P31",
                    "datavalue": {
                        "type": "wikibase-entityid",
                        "value": {"entity-type": "item", "numeric-id": 2},
                    },
                },
            }
        ]
    return json.dumps(data, separators=(",", ":"))


def page(title, page_id, ns, revisions):
    rev_xml = []
    for rev_id, parent_id, timestamp, text in revisions:
        text_xml = "" if text is None else f"<text>{text}</text>"
        rev_xml.append(
            f"""
    <revision>
      <id>{rev_id}</id>
      <parentid>{parent_id}</parentid>
      <timestamp>{timestamp}</timestamp>
      <contributor><username>User{rev_id}</username></contributor>
      {text_xml}
    </revision>"""
        )
    return f"""
  <page>
    <title>{title}</title>
    <ns>{ns}</ns>
    <id>{page_id}</id>
    {''.join(rev_xml)}
  </page>"""


def synthetic_xml():
    return (
        '<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/">'
        + page(
            "Q30098502",
            "1",
            "0",
            [
                ("101", "0", "2024-01-01T00:00:00Z", entity_json("Q30098502", "ordinary")),
                ("102", "101", "2024-01-02T00:00:00Z", '{"entity":"Q30098502","redirect":"Q2623901"}'),
            ],
        )
        + page("Qempty", "2", "0", [("201", "0", "2024-01-01T00:00:00Z", entity_json("Qempty", None, False)), ("202", "201", "2024-01-02T00:00:00Z", '{"entity":"Qempty","redirect":"Q1"}')])
        + page("Qbadredirect", "3", "0", [("301", "0", "2024-01-01T00:00:00Z", '{"entity":"Qother","redirect":"Q1"}')])
        + page("Qunknown", "4", "0", [("401", "0", "2024-01-01T00:00:00Z", '{"bar":2,"foo":1}')])
        + page("Qinvalid", "5", "0", [("501", "0", "2024-01-01T00:00:00Z", "{bad json")])
        + page("Qnonobject", "6", "0", [("601", "0", "2024-01-01T00:00:00Z", "[1,2,3]")])
        + page("Translations:Help:Namespaces/23/en", "7", "1198", [("701", "0", "2024-01-01T00:00:00Z", "not json at all")])
        + page("Qgap", "8", "0", [("801", "0", "2024-01-01T00:00:00Z", entity_json("Qgap", "gap")), ("802", "801", "2024-01-02T00:00:00Z", "{bad json")])
        + page("Qhtml", "9", "0", [("901", "0", "2024-01-01T00:00:00Z", entity_json("Qhtml", "Fish &amp;amp; Chips", False))])
        + "</mediawiki>"
    )


class ConverterTest(unittest.TestCase):
    def run_converter(self, tmp, extra_args=None, expect_ok=True):
        extra_args = extra_args or []
        input_path = Path(tmp) / "wikidatawiki-20250501-pages-meta-history1.xml-p1p1"
        input_path.write_text(synthetic_xml(), encoding="utf-8")
        output = Path(tmp) / "lifecycle.csv"
        metrics = Path(tmp) / "metrics.json"
        errors = Path(tmp) / "errors.jsonl"
        cmd = [
            sys.executable,
            str(SCRIPT),
            str(input_path),
            "-o",
            str(output),
            "--metrics-output",
            str(metrics),
            "--errors-output",
            str(errors),
        ] + extra_args
        result = subprocess.run(cmd, cwd=SCRIPT.parent, text=True, capture_output=True)
        if expect_ok and result.returncode != 0:
            self.fail(result.stderr)
        if not expect_ok:
            self.assertNotEqual(result.returncode, 0)
        return output, metrics, errors, result

    def read_csv(self, path):
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_json_classification(self):
        self.assertEqual(wd.classify_revision_json({"id": "Q1"}), "entity")
        self.assertEqual(wd.classify_revision_json({"entity": "Q30098502", "redirect": "Q2623901"}), "redirect")
        self.assertEqual(wd.classify_revision_json(["Q1"]), "non_object")
        self.assertEqual(wd.classify_revision_json({"entity": "Q1"}), "unknown_object")

    def test_extended_schema_redirects_namespaces_and_quality(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, metrics_path, errors_path, _ = self.run_converter(tmp)
            header = output.read_text(encoding="utf-8").splitlines()[0].split(",")
            self.assertEqual(header[:7], ["s", "p", "o", "cdate", "cuser", "ddate", "duser"])
            self.assertIn("crevid", header)
            rows = self.read_csv(output)
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            errors = [json.loads(line) for line in errors_path.read_text(encoding="utf-8").splitlines()]

            self.assertEqual(metrics["output_schema"], "extended")
            self.assertFalse(metrics["emit_events"])
            self.assertEqual(metrics["event_output_path"], None)
            self.assertEqual(metrics["redirect_revisions"], 3)
            self.assertEqual(metrics["redirect_entity_mismatches"], 1)
            self.assertEqual(metrics["pages_skipped_non_entity_namespace"], 1)
            self.assertEqual(metrics["revisions_skipped_non_entity_namespace"], 1)
            self.assertEqual(metrics["revisions_with_unknown_json_structure"], 1)
            self.assertEqual(metrics["revisions_with_invalid_json"], 2)
            self.assertEqual(metrics["revisions_with_non_object_json"], 1)
            self.assertNotIn("missing_entity_id", {e["error_type"] for e in errors})
            self.assertNotIn("not json at all", errors_path.read_text(encoding="utf-8"))

            closed_by_redirect = [r for r in rows if r["entity_id"] == "Q30098502" and r["ddate"] == "2024-01-02T00:00:00Z"]
            self.assertTrue(closed_by_redirect)
            self.assertTrue(all("page_has_revision_gap" not in r["quality_flags"] for r in closed_by_redirect))
            self.assertTrue(any(r["entity_id"] == "Qgap" and r["quality_flags"] == "page_has_revision_gap" for r in rows))
            self.assertTrue(any("Fish &amp; Chips" in r["o"] for r in rows))

            unknown = [e for e in errors if e["error_type"] == "unknown_entity_json_structure"][0]
            self.assertEqual(unknown["json_keys"], ["bar", "foo"])

    def test_legacy_schema_is_exactly_seven_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, metrics_path, _, _ = self.run_converter(tmp, ["--output-schema", "legacy"])
            header = output.read_text(encoding="utf-8").splitlines()[0].split(",")
            self.assertEqual(header, ["s", "p", "o", "cdate", "cuser", "ddate", "duser"])
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            self.assertEqual(metrics["output_schema"], "legacy")

    def test_events_are_optional_and_invariant(self):
        with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
            output1, metrics1, _, _ = self.run_converter(tmp1)
            events = Path(tmp2) / "events.csv"
            output2, metrics2, _, _ = self.run_converter(tmp2, ["--emit-events", "--events-output", str(events)])
            self.assertFalse((Path(tmp1) / "events.csv").exists())
            self.assertTrue(events.exists())
            self.assertEqual(output1.read_bytes(), output2.read_bytes())

            m = json.loads(metrics2.read_text(encoding="utf-8"))
            self.assertTrue(m["emit_events"])
            self.assertEqual(m["event_add_rows"], m["triple_add_events"])
            self.assertEqual(m["event_delete_rows"], m["triple_delete_events"])
            self.assertEqual(m["event_output_rows"], m["event_add_rows"] + m["event_delete_rows"])
            self.assertEqual(m["event_delete_rows"], m["closed_lifecycle_rows"])
            self.assertEqual(m["event_add_rows"], m["closed_lifecycle_rows"] + m["open_lifecycle_rows"])

            with tempfile.TemporaryDirectory() as tmp3:
                self.assertEqual(output1.read_bytes(), self.run_converter(tmp3)[0].read_bytes())
            self.assertEqual(json.loads(metrics1.read_text(encoding="utf-8"))["event_output_rows"], 0)

    def test_event_argument_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_converter(tmp, ["--emit-events"], expect_ok=False)
        with tempfile.TemporaryDirectory() as tmp:
            self.run_converter(tmp, ["--events-output", str(Path(tmp) / "events.csv")], expect_ok=False)


if __name__ == "__main__":
    unittest.main()
