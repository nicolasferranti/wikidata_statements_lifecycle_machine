#!/usr/bin/env bash
set -uo pipefail

export TZ=UTC
export LC_ALL=C
export PYTHONHASHSEED=0

SCHEMA_VERSION="2.0.0"
OUTPUT_SCHEMA="extended"
EMIT_EVENTS=0
SNAPSHOT_ID=""
RUN_ID=""
RUN_DIR=""
OUT_DIR=""
INPUT_DIR=""
PY_SCRIPT=""
JOBS=1
FIRST_ONLY=0
FORCE=0
RETRY_FAILED=0
COMMAND_LINE="$(printf '%q ' "$0" "$@")"

die() { echo "ERROR: $*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage:
  ./run_wd_history_batch_parallel.sh \
    --input-dir /path/to/wikidata_pages_meta_history \
    --script wd_history_rdf_csv.py \
    --snapshot-id 20250501 \
    --run-id NAME \
    --run-dir /path/to/run_dir \
    --jobs 4

Options:
  --input-dir PATH       Directory containing Wikidata pages-meta-history shards.
  --out-dir PATH         Backward-compatible alias for --run-dir.
  --script PATH          Python converter script.
  --jobs N              Parallel workers. Default: 1.
  --snapshot-id ID      Snapshot id used in filenames, e.g. 20250501.
  --run-id NAME         Auditable run identifier. Default: snapshot timestamp id.
  --run-dir PATH        Run directory for manifest, attempts, artifacts, metrics, errors.
  --schema-version VER  Expected lifecycle/metrics schema. Default: 2.0.0.
  --output-schema MODE  Lifecycle schema: extended or legacy. Default: extended.
  --emit-events         Also publish per-revision ADD/DELETE event CSVs.
  --first-only          Process only the first matching shard.
  --retry-failed        Run shards without a valid completion marker.
  --force               Run shards again even when completion is verified.
EOF
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required dependency: $1"
}

abs_path() {
  local p="$1"
  if [[ "$p" = /* ]]; then
    printf '%s\n' "$p"
  elif [[ -d "$p" ]]; then
    (cd "$p" && pwd -P)
  else
    local d b
    d="$(dirname "$p")"
    b="$(basename "$p")"
    if [[ -d "$d" ]]; then
      printf '%s/%s\n' "$(cd "$d" && pwd -P)" "$b"
    else
      printf '%s/%s\n' "$(pwd -P)" "$p"
    fi
  fi
}

utc_now() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

sha256_of() {
  sha256sum "$1" | awk '{print $1}'
}

file_size() {
  stat -c '%s' "$1"
}

file_mtime_utc() {
  date -u -d "@$(stat -c '%Y' "$1")" +"%Y-%m-%dT%H:%M:%SZ"
}

sanitize_id() {
  local s="$1"
  s="${s%.bz2}"
  s="${s%.gz}"
  s="${s%.xml}"
  printf '%s\n' "$s" | sed 's/[^A-Za-z0-9._-]/_/g'
}

expected_output_basename() {
  local base="$1"
  local prefix="wikidatawiki-${SNAPSHOT_ID}-pages-meta-history"
  local rest no_compress x yz
  rest="${base#"$prefix"}"
  no_compress="${rest%.bz2}"
  no_compress="${no_compress%.gz}"
  if [[ "$no_compress" == *.xml-p* ]]; then
    x="${no_compress%%.xml-p*}"
    yz="${no_compress#*.xml-p}"
    printf '%s-triple-pages-meta-history%s-p%s.csv\n' "$SNAPSHOT_ID" "$x" "$yz"
  else
    printf '%s-triple-%s.csv\n' "$SNAPSHOT_ID" "$(sanitize_id "$base")"
  fi
}

write_manifest() {
  local status="$1"
  local finished="$2"
  local successful="$3"
  local failed="$4"
  local skipped="$5"
  local input_count="$6"
  local input_bytes="$7"
  local python_version cpu_count total_memory os_name kernel host runner_sha py_sha
  python_version="$(python3 --version 2>&1)"
  cpu_count="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 0)"
  total_memory="$(awk '/MemTotal:/ {print $2 * 1024}' /proc/meminfo 2>/dev/null || echo 0)"
  os_name="$(uname -s)"
  kernel="$(uname -r)"
  host="$(hostname)"
  runner_sha="$(sha256_of "$RUNNER_SCRIPT")"
  py_sha="$(sha256_of "$PY_SCRIPT")"
  python3 - "$RUN_DIR/manifest.json" \
    "$RUN_ID" "$SNAPSHOT_ID" "$SCHEMA_VERSION" "$status" "$STARTED_AT_UTC" "$finished" \
    "$INPUT_DIR" "$PY_SCRIPT" "$py_sha" "$RUNNER_SCRIPT" "$runner_sha" "$COMMAND_LINE" \
    "$JOBS" "$host" "$os_name" "$kernel" "$python_version" "$cpu_count" "$total_memory" \
    "$input_count" "$input_bytes" "$successful" "$failed" "$skipped" "$TZ" "$LC_ALL" "$PYTHONHASHSEED" \
    "$OUTPUT_SCHEMA" "$EMIT_EVENTS" <<'PY'
import json
import os
import sys

(
    path, run_id, snapshot_id, schema_version, status, started, finished,
    input_dir, py_script, py_sha, runner_script, runner_sha, command_line,
    jobs, host, os_name, kernel, python_version, cpu_count, total_memory,
    input_count, input_bytes, successful, failed, skipped, tz, lc_all, py_hash_seed,
    output_schema, emit_events
) = sys.argv[1:]

payload = {
    "run_id": run_id,
    "snapshot_id": snapshot_id,
    "schema_version": schema_version,
    "output_schema": output_schema,
    "emit_events": emit_events == "1",
    "status": status,
    "started_at_utc": started,
    "finished_at_utc": None if not finished else finished,
    "input_directory": input_dir,
    "python_script": py_script,
    "python_script_sha256": py_sha,
    "runner_script": runner_script,
    "runner_script_sha256": runner_sha,
    "command_line": command_line.strip(),
    "jobs": int(jobs),
    "hostname": host,
    "operating_system": os_name,
    "kernel": kernel,
    "python_version": python_version,
    "cpu_count": int(cpu_count or 0),
    "total_memory_bytes": int(float(total_memory or 0)),
    "input_file_count": int(input_count),
    "input_total_compressed_bytes": int(input_bytes),
    "successful_shards": int(successful),
    "failed_shards": int(failed),
    "skipped_verified_shards": int(skipped),
    "environment": {
        "TZ": tz,
        "LC_ALL": lc_all,
        "PYTHONHASHSEED": py_hash_seed,
    },
}
tmp = f"{path}.tmp.{os.getpid()}"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
    f.write("\n")
os.replace(tmp, path)
PY
}

write_input_manifest() {
  local path="$RUN_DIR/input_manifest.csv"
  printf 'shard_id,input_path,input_basename,input_size_bytes,input_mtime_utc,input_sha256,expected_output_basename\n' > "$path.tmp"
  local f base shard_id size mtime sha expected
  for f in "${FILES[@]}"; do
    base="$(basename "$f")"
    shard_id="$(sanitize_id "$base")"
    size="$(file_size "$f")"
    mtime="$(file_mtime_utc "$f")"
    sha="$(sha256_of "$f")"
    expected="$(expected_output_basename "$base")"
    printf '%s,%s,%s,%s,%s,%s,%s\n' "$shard_id" "$f" "$base" "$size" "$mtime" "$sha" "$expected" >> "$path.tmp"
  done
  mv -f "$path.tmp" "$path"
}

validate_lifecycle_artifact() {
  local lifecycle="$1"
  local metrics="$2"
  local expected_schema="$3"
  local expected_header="$4"
  local expected_output_schema="$5"
  local emit_events="$6"
  local events="$7"
  local expected_event_header="$8"
  python3 - "$lifecycle" "$metrics" "$expected_schema" "$expected_header" "$expected_output_schema" "$emit_events" "$events" "$expected_event_header" <<'PY'
import json
import os
import sys
import csv

lifecycle, metrics_path, expected_schema, expected_header, expected_output_schema, emit_events, events_path, expected_event_header = sys.argv[1:]
emit_events = emit_events == "1"
if not os.path.exists(lifecycle):
    print("temporary lifecycle CSV does not exist")
    sys.exit(1)
with open(lifecycle, "r", encoding="utf-8", newline="") as f:
    header = f.readline().rstrip("\n\r")
    rows = sum(1 for _ in f)
if header != expected_header:
    print("unexpected lifecycle CSV header")
    sys.exit(1)
if not os.path.exists(metrics_path):
    print("metrics JSON does not exist")
    sys.exit(1)
try:
    with open(metrics_path, "r", encoding="utf-8") as f:
        metrics = json.load(f)
except Exception as e:
    print(f"metrics JSON is invalid: {e}")
    sys.exit(1)
if metrics.get("schema_version") != expected_schema:
    print("metrics schema_version mismatch")
    sys.exit(1)
if metrics.get("output_schema") != expected_output_schema:
    print("metrics output_schema mismatch")
    sys.exit(1)
if metrics.get("completed_successfully") is not True:
    print("metrics completed_successfully is not true")
    sys.exit(1)
if int(metrics.get("output_data_rows", -1)) != rows:
    print("metrics output_data_rows does not match actual row count")
    sys.exit(1)
actual_size = os.path.getsize(lifecycle)
if int(metrics.get("output_bytes", -1)) != actual_size:
    metrics["output_bytes"] = actual_size
    with open(metrics_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
if int(metrics.get("output_data_rows", -1)) != int(metrics.get("closed_lifecycle_rows", -2)) + int(metrics.get("open_lifecycle_rows", -3)):
    print("lifecycle row invariant failed")
    sys.exit(1)
if int(metrics.get("triple_delete_events", -1)) != int(metrics.get("closed_lifecycle_rows", -2)):
    print("transition invariant failed")
    sys.exit(1)
if bool(metrics.get("emit_events")) != emit_events:
    print("metrics emit_events mismatch")
    sys.exit(1)
if emit_events:
    if not os.path.exists(events_path):
        print("event CSV does not exist")
        sys.exit(1)
    add_rows = 0
    delete_rows = 0
    with open(events_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        event_header = ",".join(next(reader, []))
        for parts in reader:
            action = parts[6] if len(parts) > 6 else ""
            if action == "ADD":
                add_rows += 1
            elif action == "DELETE":
                delete_rows += 1
    if event_header != expected_event_header:
        print("unexpected event CSV header")
        sys.exit(1)
    event_rows = add_rows + delete_rows
    if int(metrics.get("event_output_rows", -1)) != event_rows:
        print("event_output_rows mismatch")
        sys.exit(1)
    if int(metrics.get("event_add_rows", -1)) != add_rows:
        print("event_add_rows mismatch")
        sys.exit(1)
    if int(metrics.get("event_delete_rows", -1)) != delete_rows:
        print("event_delete_rows mismatch")
        sys.exit(1)
    if add_rows != int(metrics.get("triple_add_events", -1)):
        print("ADD events do not match triple_add_events")
        sys.exit(1)
    if delete_rows != int(metrics.get("triple_delete_events", -1)):
        print("DELETE events do not match triple_delete_events")
        sys.exit(1)
    if delete_rows != int(metrics.get("closed_lifecycle_rows", -1)):
        print("DELETE events do not match closed_lifecycle_rows")
        sys.exit(1)
    if add_rows != int(metrics.get("closed_lifecycle_rows", -1)) + int(metrics.get("open_lifecycle_rows", -1)):
        print("ADD events do not match closed+open lifecycle rows")
        sys.exit(1)
else:
    if metrics.get("event_output_path") is not None:
        print("event_output_path should be null when events are disabled")
        sys.exit(1)
    if any(int(metrics.get(k, 0)) != 0 for k in ("event_output_rows", "event_add_rows", "event_delete_rows")):
        print("event metrics should be zero when events are disabled")
        sys.exit(1)
print(rows)
PY
}

write_attempt_status() {
  local path="$1"
  local run_id="$2"
  local shard_id="$3"
  local attempt="$4"
  local started="$5"
  local finished="$6"
  local elapsed="$7"
  local exit_code="$8"
  local status="$9"
  local input_sha="${10}"
  local py_sha="${11}"
  local tmp_output="${12}"
  local final_output="${13}"
  local output_size="${14}"
  local output_rows="${15}"
  local output_sha="${16}"
  python3 - "$path" "$run_id" "$shard_id" "$attempt" "$started" "$finished" "$elapsed" \
    "$exit_code" "$status" "$input_sha" "$py_sha" "$SCHEMA_VERSION" "$tmp_output" "$final_output" \
    "$output_size" "$output_rows" "$output_sha" <<'PY'
import json
import os
import sys

(
    path, run_id, shard_id, attempt, started, finished, elapsed, exit_code, status,
    input_sha, py_sha, schema_version, tmp_output, final_output, output_size,
    output_rows, output_sha
) = sys.argv[1:]
payload = {
    "run_id": run_id,
    "shard_id": shard_id,
    "attempt": int(attempt),
    "started_at_utc": started,
    "finished_at_utc": finished,
    "elapsed_wall_seconds": float(elapsed),
    "exit_code": int(exit_code),
    "status": status,
    "input_sha256": input_sha,
    "python_script_sha256": py_sha,
    "schema_version": schema_version,
    "temporary_output_path": tmp_output,
    "final_output_path": final_output,
    "output_size_bytes": int(output_size or 0),
    "output_data_rows": int(output_rows or 0),
    "output_sha256": output_sha,
}
tmp = f"{path}.tmp.{os.getpid()}"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
    f.write("\n")
os.replace(tmp, path)
PY
}

write_completion() {
  local path="$1"
  local shard_id="$2"
  local attempt="$3"
  local input_sha="$4"
  local py_sha="$5"
  local output_sha="$6"
  local output_size="$7"
  local output_rows="$8"
  local metrics_sha="$9"
  local errors_sha="${10}"
  local emit_events="${11}"
  local event_path="${12}"
  local event_rows="${13}"
  local event_bytes="${14}"
  local event_sha="${15}"
  python3 - "$path" "$RUN_ID" "$SNAPSHOT_ID" "$SCHEMA_VERSION" "$shard_id" "$attempt" \
    "$input_sha" "$py_sha" "$output_sha" "$output_size" "$output_rows" "$metrics_sha" "$errors_sha" "$(utc_now)" \
    "$OUTPUT_SCHEMA" "$emit_events" "$event_path" "$event_rows" "$event_bytes" "$event_sha" <<'PY'
import json
import os
import sys

(
    path, run_id, snapshot_id, schema_version, shard_id, attempt, input_sha,
    py_sha, output_sha, output_size, output_rows, metrics_sha, errors_sha, completed,
    output_schema, emit_events, event_path, event_rows, event_bytes, event_sha
) = sys.argv[1:]
emit_events_bool = emit_events == "1"
payload = {
    "status": "success",
    "run_id": run_id,
    "snapshot_id": snapshot_id,
    "schema_version": schema_version,
    "output_schema": output_schema,
    "emit_events": emit_events_bool,
    "shard_id": shard_id,
    "successful_attempt": int(attempt),
    "input_sha256": input_sha,
    "python_script_sha256": py_sha,
    "output_sha256": output_sha,
    "output_size_bytes": int(output_size),
    "output_data_rows": int(output_rows),
    "metrics_sha256": metrics_sha,
    "errors_sha256": errors_sha,
    "completed_at_utc": completed,
}
if emit_events_bool:
    payload.update({
        "event_output_path": event_path,
        "event_output_rows": int(event_rows),
        "event_output_bytes": int(event_bytes),
        "event_output_sha256": event_sha,
    })
tmp = f"{path}.tmp.{os.getpid()}"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
    f.write("\n")
os.replace(tmp, path)
PY
}

verify_completion() {
  local completion="$1"
  local output="$2"
  local metrics="$3"
  local errors="$4"
  local run_id="$5"
  local schema="$6"
  local input_sha="$7"
  local py_sha="$8"
  local expected_header="$9"
  local expected_output_schema="${10}"
  local emit_events="${11}"
  local events="${12}"
  local expected_event_header="${13}"
  python3 - "$completion" "$output" "$metrics" "$errors" "$run_id" "$schema" "$input_sha" "$py_sha" "$expected_header" \
    "$expected_output_schema" "$emit_events" "$events" "$expected_event_header" <<'PY'
import json
import os
import sys
import csv

completion, output, metrics_path, errors_path, run_id, schema, input_sha, py_sha, expected_header, expected_output_schema, emit_events, events_path, expected_event_header = sys.argv[1:]
emit_events = emit_events == "1"
try:
    with open(completion, "r", encoding="utf-8") as f:
        c = json.load(f)
except Exception:
    sys.exit(1)
if c.get("status") != "success":
    sys.exit(1)
if c.get("run_id") != run_id or c.get("schema_version") != schema:
    sys.exit(1)
if c.get("output_schema") != expected_output_schema or bool(c.get("emit_events")) != emit_events:
    sys.exit(1)
if c.get("input_sha256") != input_sha or c.get("python_script_sha256") != py_sha:
    sys.exit(1)
for p in (output, metrics_path, errors_path):
    if not os.path.exists(p):
        sys.exit(1)
if emit_events and not os.path.exists(events_path):
    sys.exit(1)
if (not emit_events) and os.path.exists(events_path):
    sys.exit(1)
import hashlib
def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
if sha(output) != c.get("output_sha256"):
    sys.exit(1)
with open(output, "r", encoding="utf-8", newline="") as f:
    header = f.readline().rstrip("\n\r")
    rows = sum(1 for _ in f)
if header != expected_header:
    sys.exit(1)
if rows != int(c.get("output_data_rows", -1)):
    sys.exit(1)
try:
    with open(metrics_path, "r", encoding="utf-8") as f:
        m = json.load(f)
except Exception:
    sys.exit(1)
if m.get("completed_successfully") is not True:
    sys.exit(1)
if int(m.get("output_data_rows", -1)) != rows:
    sys.exit(1)
if int(m.get("output_data_rows", -1)) != int(m.get("closed_lifecycle_rows", -2)) + int(m.get("open_lifecycle_rows", -3)):
    sys.exit(1)
if int(m.get("triple_delete_events", -1)) != int(m.get("closed_lifecycle_rows", -2)):
    sys.exit(1)
if m.get("output_schema") != expected_output_schema or bool(m.get("emit_events")) != emit_events:
    sys.exit(1)
if emit_events:
    if sha(events_path) != c.get("event_output_sha256"):
        sys.exit(1)
    with open(events_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        event_header = ",".join(next(reader, []))
        add_rows = 0
        delete_rows = 0
        for row in reader:
            if len(row) > 6 and row[6] == "ADD":
                add_rows += 1
            elif len(row) > 6 and row[6] == "DELETE":
                delete_rows += 1
    if event_header != expected_event_header:
        sys.exit(1)
    if add_rows != int(m.get("event_add_rows", -1)) or delete_rows != int(m.get("event_delete_rows", -1)):
        sys.exit(1)
    if add_rows + delete_rows != int(m.get("event_output_rows", -1)):
        sys.exit(1)
else:
    if m.get("event_output_path") is not None:
        sys.exit(1)
sys.exit(0)
PY
}

next_attempt_dir() {
  local shard_id="$1"
  local shard_dir="$RUN_DIR/attempts/$shard_id"
  mkdir -p "$shard_dir"
  local n=1
  while true; do
    local name
    name="$(printf 'attempt-%03d' "$n")"
    if mkdir "$shard_dir/$name" 2>/dev/null; then
      printf '%s %s\n' "$shard_dir/$name" "$n"
      return 0
    fi
    n=$((n + 1))
  done
}

run_worker() {
  local input="$1"
  local base shard_id expected input_sha py_sha input_bytes
  base="$(basename "$input")"
  shard_id="$(sanitize_id "$base")"
  expected="$(expected_output_basename "$base")"
  input_sha="$(sha256_of "$input")"
  py_sha="$(sha256_of "$PY_SCRIPT")"
  input_bytes="$(file_size "$input")"

  local final_output="$RUN_DIR/artifacts/lifecycle/$expected"
  local final_metrics="$RUN_DIR/metrics/${shard_id}.metrics.json"
  local final_errors="$RUN_DIR/errors/${shard_id}.errors.jsonl"
  local final_events="$RUN_DIR/artifacts/events/${expected%.csv}.events.csv"
  local completion="$RUN_DIR/completion/${shard_id}.completion.json"
  local expected_header="s,p,o,cdate,cuser,ddate,duser,entity_id,page_id,crevid,cparentid,drevid,dparentid,source_shard,schema_version,quality_flags"
  if [[ "$OUTPUT_SCHEMA" == "legacy" ]]; then
    expected_header="s,p,o,cdate,cuser,ddate,duser"
  fi
  local expected_event_header="entity_id,page_id,revision_id,parent_revision_id,timestamp,contributor,action,s,p,o,source_shard,schema_version,quality_flags"

  if [[ "$FORCE" -eq 0 ]] && verify_completion "$completion" "$final_output" "$final_metrics" "$final_errors" "$RUN_ID" "$SCHEMA_VERSION" "$input_sha" "$py_sha" "$expected_header" "$OUTPUT_SCHEMA" "$EMIT_EVENTS" "$final_events" "$expected_event_header"; then
    echo "[SKIP VERIFIED] $base"
    printf 'skipped\n' > "$RUN_DIR/checksums/${shard_id}.worker_status"
    return 0
  fi

  local attempt_info attempt_dir attempt
  attempt_info="$(next_attempt_dir "$shard_id")"
  attempt_dir="${attempt_info% *}"
  attempt="${attempt_info##* }"

  local stdout_log="$attempt_dir/stdout.log"
  local stderr_log="$attempt_dir/stderr.log"
  local system_time="$attempt_dir/system_time.txt"
  local command_file="$attempt_dir/command.txt"
  local status_file="$attempt_dir/status.json"
  local tmp_output="$attempt_dir/lifecycle.tmp.csv"
  local tmp_metrics="$attempt_dir/metrics.tmp.json"
  local tmp_errors="$attempt_dir/errors.tmp.jsonl"
  local tmp_events="$attempt_dir/events.tmp.csv"
  local started finished start_s end_s elapsed exit_code status output_size output_rows output_sha

  echo "[START] $base | attempt=$attempt | input_bytes=$input_bytes"

  local cmd=(python3 "$PY_SCRIPT" "$input" -o "$tmp_output" --metrics-output "$tmp_metrics" --errors-output "$tmp_errors" --output-schema "$OUTPUT_SCHEMA")
  if [[ "$EMIT_EVENTS" -eq 1 ]]; then
    cmd+=(--emit-events --events-output "$tmp_events")
  fi
  printf '/usr/bin/time -v -o %q ' "$system_time" > "$command_file"
  printf '%q ' "${cmd[@]}" >> "$command_file"
  printf '\n' >> "$command_file"

  started="$(utc_now)"
  start_s="$(date -u +%s)"
  /usr/bin/time -v -o "$system_time" "${cmd[@]}" >"$stdout_log" 2>"$stderr_log"
  exit_code=$?
  end_s="$(date -u +%s)"
  finished="$(utc_now)"
  elapsed=$((end_s - start_s))

  output_size=0
  output_rows=0
  output_sha=""

  if [[ "$exit_code" -ne 0 ]]; then
    status="failed"
    echo "[FAIL] $base | attempt=$attempt | exit_code=$exit_code"
    write_attempt_status "$status_file" "$RUN_ID" "$shard_id" "$attempt" "$started" "$finished" "$elapsed" \
      "$exit_code" "$status" "$input_sha" "$py_sha" "$tmp_output" "$final_output" "$output_size" "$output_rows" "$output_sha"
    printf 'failed\n' > "$RUN_DIR/checksums/${shard_id}.worker_status"
    return 1
  fi

  local validation_output validation_rc
  validation_output="$(validate_lifecycle_artifact "$tmp_output" "$tmp_metrics" "$SCHEMA_VERSION" "$expected_header" "$OUTPUT_SCHEMA" "$EMIT_EVENTS" "$tmp_events" "$expected_event_header" 2>&1)"
  validation_rc=$?
  if [[ "$validation_rc" -ne 0 ]]; then
    status="validation_failed"
    echo "[INVALID] $base | attempt=$attempt | reason=$validation_output"
    write_attempt_status "$status_file" "$RUN_ID" "$shard_id" "$attempt" "$started" "$finished" "$elapsed" \
      0 "$status" "$input_sha" "$py_sha" "$tmp_output" "$final_output" "$output_size" "$output_rows" "$output_sha"
    printf 'failed\n' > "$RUN_DIR/checksums/${shard_id}.worker_status"
    return 1
  fi

  output_rows="$validation_output"
  output_size="$(file_size "$tmp_output")"
  output_sha="$(sha256_of "$tmp_output")"
  local metrics_sha errors_sha event_sha event_size event_rows
  metrics_sha="$(sha256_of "$tmp_metrics")"
  if [[ ! -f "$tmp_errors" ]]; then
    : > "$tmp_errors"
  fi
  errors_sha="$(sha256_of "$tmp_errors")"
  event_sha=""
  event_size=0
  event_rows=0
  if [[ "$EMIT_EVENTS" -eq 1 ]]; then
    event_sha="$(sha256_of "$tmp_events")"
    event_size="$(file_size "$tmp_events")"
    event_rows="$(python3 - "$tmp_metrics" <<'PY'
import json
import sys
with open(sys.argv[1], "r", encoding="utf-8") as f:
    print(json.load(f).get("event_output_rows", 0))
PY
)"
  fi

  mv -f "$tmp_output" "$final_output"
  mv -f "$tmp_metrics" "$final_metrics"
  mv -f "$tmp_errors" "$final_errors"
  if [[ "$EMIT_EVENTS" -eq 1 ]]; then
    mv -f "$tmp_events" "$final_events"
  fi
  write_completion "$completion" "$shard_id" "$attempt" "$input_sha" "$py_sha" "$output_sha" "$output_size" "$output_rows" "$metrics_sha" "$errors_sha" "$EMIT_EVENTS" "$final_events" "$event_rows" "$event_size" "$event_sha"

  status="success"
  write_attempt_status "$status_file" "$RUN_ID" "$shard_id" "$attempt" "$started" "$finished" "$elapsed" \
    0 "$status" "$input_sha" "$py_sha" "$tmp_output" "$final_output" "$output_size" "$output_rows" "$output_sha"
  echo "[DONE] $base | attempt=$attempt | seconds=$elapsed | rows=$output_rows | sha256=$output_sha"
  printf 'success\n' > "$RUN_DIR/checksums/${shard_id}.worker_status"
  return 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --input-dir) INPUT_DIR="${2:-}"; shift 2;;
    --out-dir) OUT_DIR="${2:-}"; shift 2;;
    --script) PY_SCRIPT="${2:-}"; shift 2;;
    --jobs) JOBS="${2:-}"; shift 2;;
    --snapshot-id) SNAPSHOT_ID="${2:-}"; shift 2;;
    --run-id) RUN_ID="${2:-}"; shift 2;;
    --run-dir) RUN_DIR="${2:-}"; shift 2;;
    --schema-version) SCHEMA_VERSION="${2:-}"; shift 2;;
    --output-schema) OUTPUT_SCHEMA="${2:-}"; shift 2;;
    --emit-events) EMIT_EVENTS=1; shift 1;;
    --first-only) FIRST_ONLY=1; shift 1;;
    --retry-failed) RETRY_FAILED=1; shift 1;;
    --force) FORCE=1; shift 1;;
    -h|--help) usage; exit 0;;
    *) die "Unknown arg: $1";;
  esac
done

require_cmd bash
require_cmd python3
require_cmd sha256sum
require_cmd stat
require_cmd find
require_cmd sort
require_cmd wc
require_cmd awk
require_cmd sed
require_cmd date
[[ -x /usr/bin/time ]] || die "Missing required dependency: /usr/bin/time"
stat -c '%s' "$0" >/dev/null 2>&1 || die "stat does not support GNU -c syntax"
/usr/bin/time -v -o /tmp/wd_history_runner_time_check.$$ true >/dev/null 2>&1 || die "/usr/bin/time does not support -v -o"
rm -f /tmp/wd_history_runner_time_check.$$

[[ -n "$INPUT_DIR" ]] || die "Missing --input-dir"
[[ -n "$PY_SCRIPT" ]] || die "Missing --script"
[[ -n "$SNAPSHOT_ID" ]] || die "Missing --snapshot-id"
[[ "$SNAPSHOT_ID" =~ ^[0-9]{8}$ ]] || die "--snapshot-id must look like YYYYMMDD"
[[ "$JOBS" =~ ^[0-9]+$ ]] || die "--jobs must be an integer"
[[ "$JOBS" -ge 1 ]] || die "--jobs must be >= 1"
[[ "$SCHEMA_VERSION" == "2.0.0" ]] || die "This runner expects schema version 2.0.0"
[[ "$OUTPUT_SCHEMA" == "extended" || "$OUTPUT_SCHEMA" == "legacy" ]] || die "--output-schema must be extended or legacy"
[[ -d "$INPUT_DIR" ]] || die "Input dir not found: $INPUT_DIR"
[[ -f "$PY_SCRIPT" ]] || die "Python script not found: $PY_SCRIPT"

INPUT_DIR="$(abs_path "$INPUT_DIR")"
PY_SCRIPT="$(abs_path "$PY_SCRIPT")"
RUNNER_SCRIPT="$(abs_path "$0")"
if [[ -z "$RUN_DIR" ]]; then
  RUN_DIR="$OUT_DIR"
fi
if [[ -z "$RUN_DIR" ]]; then
  RUN_DIR="runs/${SNAPSHOT_ID}-$(date -u +%Y%m%dT%H%M%SZ)"
fi
RUN_DIR="$(abs_path "$RUN_DIR")"
if [[ -z "$RUN_ID" ]]; then
  RUN_ID="${SNAPSHOT_ID}-$(date -u +%Y%m%dT%H%M%SZ)"
fi

mkdir -p "$RUN_DIR"/attempts "$RUN_DIR"/artifacts/lifecycle "$RUN_DIR"/metrics "$RUN_DIR"/errors "$RUN_DIR"/completion "$RUN_DIR"/checksums
if [[ "$EMIT_EVENTS" -eq 1 ]]; then
  mkdir -p "$RUN_DIR"/artifacts/events
fi

mapfile -d '' FILES < <(find "$INPUT_DIR" -maxdepth 1 -type f \
  -name "wikidatawiki-${SNAPSHOT_ID}-pages-meta-history*" -print0 | sort -z)

if [[ "$FIRST_ONLY" -eq 1 && "${#FILES[@]}" -gt 1 ]]; then
  FILES=("${FILES[0]}")
fi

TOTAL="${#FILES[@]}"
[[ "$TOTAL" -gt 0 ]] || die "No matching files found for snapshot $SNAPSHOT_ID in: $INPUT_DIR"

INPUT_TOTAL_BYTES=0
for f in "${FILES[@]}"; do
  INPUT_TOTAL_BYTES=$((INPUT_TOTAL_BYTES + $(file_size "$f")))
done

STARTED_AT_UTC="$(utc_now)"
write_input_manifest
write_manifest "running" "" 0 0 0 "$TOTAL" "$INPUT_TOTAL_BYTES"

echo "Run ID: $RUN_ID"
echo "Snapshot: $SNAPSHOT_ID"
echo "Run directory: $RUN_DIR"
echo "Found $TOTAL input files"
echo "Python script: $PY_SCRIPT"
echo "Parallel jobs: $JOBS"
echo "Output schema: $OUTPUT_SCHEMA"
echo "Emit events: $EMIT_EVENTS"
echo "First-only mode: $FIRST_ONLY"
echo

active_jobs=0
for f in "${FILES[@]}"; do
  run_worker "$f" &
  active_jobs=$((active_jobs + 1))
  if [[ "$active_jobs" -ge "$JOBS" ]]; then
    wait -n || true
    active_jobs=$((active_jobs - 1))
  fi
done

while [[ "$active_jobs" -gt 0 ]]; do
  wait -n || true
  active_jobs=$((active_jobs - 1))
done

successful=0
failed=0
skipped=0
for f in "${FILES[@]}"; do
  shard_id="$(sanitize_id "$(basename "$f")")"
  status_file="$RUN_DIR/checksums/${shard_id}.worker_status"
  if [[ ! -f "$status_file" ]]; then
    failed=$((failed + 1))
    continue
  fi
  status_value="$(cat "$status_file")"
  case "$status_value" in
    success) successful=$((successful + 1));;
    skipped) skipped=$((skipped + 1));;
    *) failed=$((failed + 1));;
  esac
done

final_status="completed"
if [[ "$failed" -gt 0 ]]; then
  final_status="completed_with_failures"
fi
write_manifest "$final_status" "$(utc_now)" "$successful" "$failed" "$skipped" "$TOTAL" "$INPUT_TOTAL_BYTES"

echo
echo "Run finished: status=$final_status successful=$successful failed=$failed skipped_verified=$skipped"

if [[ "$failed" -gt 0 ]]; then
  exit 1
fi
exit 0
