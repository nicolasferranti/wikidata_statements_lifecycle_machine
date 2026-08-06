import csv
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "run_wd_history_batch_parallel.sh"


FAKE_WORKER = r"""#!/usr/bin/env python3
import argparse
import fcntl
import json
import os
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("input")
ap.add_argument("--output", "-o", required=True)
ap.add_argument("--metrics-output", required=True)
ap.add_argument("--errors-output", required=True)
ap.add_argument("--output-schema", default="extended")
ap.add_argument("--emit-events", action="store_true")
ap.add_argument("--events-output")
args = ap.parse_args()

state = Path(os.environ["FAKE_RUNNER_STATE"])
state.mkdir(parents=True, exist_ok=True)
lock_path = state / "lock"
active_path = state / "active"
max_path = state / "max_active"
starts_path = state / "starts.tsv"
runs_path = state / "runs.tsv"

base = Path(args.input).name
with open(lock_path, "a+") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    active = int(active_path.read_text() if active_path.exists() else "0") + 1
    active_path.write_text(str(active))
    max_active = int(max_path.read_text() if max_path.exists() else "0")
    max_path.write_text(str(max(max_active, active)))
    with open(starts_path, "a", encoding="utf-8") as f:
        f.write(f"{base}\t{time.time()}\n")
    with open(runs_path, "a", encoding="utf-8") as f:
        f.write(f"{base}\n")
    fcntl.flock(lock, fcntl.LOCK_UN)

time.sleep(float(os.environ.get("FAKE_WORKER_SLEEP", "0.2")))

is_fail = "fail" in base
is_nometrics = "nometrics" in base
header = "s,p,o,cdate,cuser,ddate,duser"
if args.output_schema == "extended":
    header += ",entity_id,page_id,crevid,cparentid,drevid,dparentid,source_shard,schema_version,quality_flags"

metrics = {
    "input_basename": base,
    "input_compressed_bytes": os.path.getsize(args.input),
    "pages_seen": 1,
    "pages_processed": 1 if not is_fail else 0,
    "pages_skipped_non_entity_namespace": 2 if not is_fail else 0,
    "revisions_seen": 5 if not is_fail else 0,
    "revisions_processed": 3 if not is_fail else 0,
    "revisions_skipped_non_entity_namespace": 2 if not is_fail else 0,
    "revisions_with_valid_entity_json": 3 if not is_fail else 0,
    "redirect_revisions": 1 if not is_fail else 0,
    "triples_extracted_total": 7 if not is_fail else 0,
    "triple_add_events": 2 if not is_fail else 0,
    "triple_delete_events": 1 if not is_fail else 0,
    "output_data_rows": 1 if not is_fail else 0,
    "open_lifecycle_rows": 0 if not is_fail else 0,
    "closed_lifecycle_rows": 1 if not is_fail else 0,
    "warnings": 4 if not is_fail else 0,
    "errors": 0 if not is_fail else 1,
    "total_seconds": 0.2,
    "output_bytes": 0,
    "completed_successfully": not is_fail,
}

Path(args.errors_output).write_text("", encoding="utf-8")
if is_nometrics:
    Path(args.output).write_text(header + "\n", encoding="utf-8")
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        active = int(active_path.read_text()) - 1
        active_path.write_text(str(active))
        fcntl.flock(lock, fcntl.LOCK_UN)
    raise SystemExit(0)
if is_fail:
    Path(args.metrics_output).write_text(json.dumps(metrics), encoding="utf-8")
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        active = int(active_path.read_text()) - 1
        active_path.write_text(str(active))
        fcntl.flock(lock, fcntl.LOCK_UN)
    raise SystemExit(7)

row = "wd:Q1,rdfs:label,\"\"\"One\"\"@en,2024-01-01T00:00:00Z,A,,,,Q1,1,1,0,,,,test,2.0.0,"
if args.output_schema == "legacy":
    row = "wd:Q1,rdfs:label,\"\"\"One\"\"@en,2024-01-01T00:00:00Z,A,"
Path(args.output).write_text(header + "\n" + row + "\n", encoding="utf-8")
metrics["output_bytes"] = os.path.getsize(args.output)
Path(args.metrics_output).write_text(json.dumps(metrics), encoding="utf-8")
if args.emit_events and args.events_output:
    Path(args.events_output).write_text("entity_id,page_id,revision_id,parent_revision_id,timestamp,contributor,action,s,p,o,source_shard,schema_version,quality_flags\n", encoding="utf-8")

with open(lock_path, "a+") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    active = int(active_path.read_text()) - 1
    active_path.write_text(str(active))
    fcntl.flock(lock, fcntl.LOCK_UN)
"""


class RunnerTest(unittest.TestCase):
    def make_workspace(self, tmp):
        root = Path(tmp) / "path with spaces"
        input_dir = root / "input shards"
        run_dir = root / "run dir"
        state_dir = root / "state dir"
        input_dir.mkdir(parents=True)
        script = root / "fake worker.py"
        script.write_text(FAKE_WORKER, encoding="utf-8")
        script.chmod(0o755)
        return input_dir, run_dir, state_dir, script

    def touch_shard(self, input_dir, name):
        path = input_dir / name
        path.write_text("dummy", encoding="utf-8")
        return path

    def run_runner(self, input_dir, run_dir, state_dir, script, extra=None):
        extra = extra or []
        env = os.environ.copy()
        env["FAKE_RUNNER_STATE"] = str(state_dir)
        env["FAKE_WORKER_SLEEP"] = "0.5"
        cmd = [
            "bash",
            str(RUNNER),
            "--input-dir",
            str(input_dir),
            "--script",
            str(script),
            "--snapshot-id",
            "20250501",
            "--run-id",
            "test-run",
            "--run-dir",
            str(run_dir),
            "--jobs",
            "2",
        ] + extra
        return subprocess.run(cmd, cwd=ROOT, env=env, text=True, capture_output=True)

    def summary_rows(self, run_dir):
        with open(run_dir / "shard_summary.tsv", newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f, delimiter="\t"))

    def test_success_failure_summary_logs_concurrency_and_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_dir, run_dir, state_dir, script = self.make_workspace(tmp)
            self.touch_shard(input_dir, "wikidatawiki-20250501-pages-meta-history1.xml-p1p2.bz2")
            self.touch_shard(input_dir, "wikidatawiki-20250501-pages-meta-history2.xml-pfailp3.bz2")

            start = time.time()
            result = self.run_runner(input_dir, run_dir, state_dir, script)
            elapsed = time.time() - start
            self.assertNotEqual(result.returncode, 0)
            self.assertLess(elapsed, 3.0)
            self.assertIn("[DONE]", result.stdout)
            self.assertIn("[FAIL]", result.stdout)

            rows = self.summary_rows(run_dir)
            self.assertEqual({row["status"] for row in rows}, {"success", "failed"})
            success = [row for row in rows if row["status"] == "success"][0]
            failed = [row for row in rows if row["status"] == "failed"][0]
            self.assertEqual(success["pages_processed"], "1")
            self.assertEqual(success["pages_skipped_non_entity_namespace"], "2")
            self.assertEqual(success["revisions_seen"], "5")
            self.assertEqual(success["revisions_with_valid_entity_json"], "3")
            self.assertEqual(success["redirect_revisions"], "1")
            self.assertEqual(success["output_data_rows"], "1")
            self.assertEqual(success["warnings"], "4")
            self.assertEqual(failed["exit_code"], "7")

            success_output = run_dir / "lifecycle" / "20250501-triple-pages-meta-history1-p1p2.csv"
            fail_output = run_dir / "lifecycle" / "20250501-triple-pages-meta-history2-pfailp3.csv"
            self.assertTrue(success_output.exists())
            self.assertFalse(fail_output.exists())

            for row in rows:
                log_dir = run_dir / "logs" / row["shard_id"]
                attempt_dir = log_dir / "attempt-001"
                self.assertTrue((attempt_dir / "stdout.log").exists())
                self.assertTrue((attempt_dir / "stderr.log").exists())
                self.assertTrue((attempt_dir / "system_time.txt").exists())
                self.assertTrue((attempt_dir / "status.tsv").exists())
                self.assertTrue((log_dir / "status.tsv").exists())

            self.assertLessEqual(int((state_dir / "max_active").read_text()), 2)
            self.assertIn("path with spaces", (run_dir / "run_config.txt").read_text(encoding="utf-8"))

    def test_existing_success_is_skipped_and_force_reruns(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_dir, run_dir, state_dir, script = self.make_workspace(tmp)
            self.touch_shard(input_dir, "wikidatawiki-20250501-pages-meta-history1.xml-p1p2.bz2")

            first = self.run_runner(input_dir, run_dir, state_dir, script)
            self.assertEqual(first.returncode, 0, first.stderr)
            second = self.run_runner(input_dir, run_dir, state_dir, script)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("[SKIP]", second.stdout)
            self.assertEqual(self.summary_rows(run_dir)[0]["status"], "skipped")

            before_force_runs = (state_dir / "runs.tsv").read_text(encoding="utf-8").count("\n")
            forced = self.run_runner(input_dir, run_dir, state_dir, script, ["--force"])
            self.assertEqual(forced.returncode, 0, forced.stderr)
            after_force_runs = (state_dir / "runs.tsv").read_text(encoding="utf-8").count("\n")
            self.assertEqual(after_force_runs, before_force_runs + 1)
            self.assertEqual(self.summary_rows(run_dir)[0]["status"], "success")

    def test_exit_zero_without_valid_metrics_is_failure_and_not_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_dir, run_dir, state_dir, script = self.make_workspace(tmp)
            self.touch_shard(input_dir, "wikidatawiki-20250501-pages-meta-history3.xml-pnometricsp4.bz2")

            result = self.run_runner(input_dir, run_dir, state_dir, script)
            self.assertNotEqual(result.returncode, 0)
            row = self.summary_rows(run_dir)[0]
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["exit_code"], "99")
            final_output = run_dir / "lifecycle" / "20250501-triple-pages-meta-history3-pnometricsp4.csv"
            self.assertFalse(final_output.exists())

    def test_failed_rerun_preserves_attempt_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_dir, run_dir, state_dir, script = self.make_workspace(tmp)
            self.touch_shard(input_dir, "wikidatawiki-20250501-pages-meta-history2.xml-pfailp3.bz2")

            first = self.run_runner(input_dir, run_dir, state_dir, script)
            self.assertNotEqual(first.returncode, 0)
            second = self.run_runner(input_dir, run_dir, state_dir, script)
            self.assertNotEqual(second.returncode, 0)
            shard_id = "wikidatawiki-20250501-pages-meta-history2.xml-pfailp3"
            log_dir = run_dir / "logs" / shard_id
            self.assertTrue((log_dir / "attempt-001" / "stderr.log").exists())
            self.assertTrue((log_dir / "attempt-002" / "stderr.log").exists())

    def test_runner_does_not_use_checksums(self):
        text = RUNNER.read_text(encoding="utf-8")
        self.assertNotIn("sha256sum", text)
        self.assertNotIn("checksum", text.lower())
        self.assertIn('$4 != "success" && $4 != "skipped"', text)


if __name__ == "__main__":
    unittest.main()
