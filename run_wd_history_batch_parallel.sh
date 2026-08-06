#!/usr/bin/env bash
set -uo pipefail

export TZ=UTC
export LC_ALL=C
export PYTHONHASHSEED=0

INPUT_DIR=""
RUN_DIR=""
OUT_DIR=""
PY_SCRIPT=""
SNAPSHOT_ID=""
RUN_ID=""
JOBS=1
OUTPUT_SCHEMA="extended"
EMIT_EVENTS=0
FORCE=0
FIRST_ONLY=0
COMMAND_LINE="$(printf '%q ' "$0" "$@")"

die() { echo "ERROR: $*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage:
  ./run_wd_history_batch_parallel.sh \
    --input-dir /path/to/wikidata_pages_meta_history \
    --script wd_history_rdf_csv.py \
    --snapshot-id 20250501 \
    --run-dir /path/to/run_dir \
    --jobs 16

Options:
  --input-dir PATH       Directory containing .bz2 pages-meta-history shards.
  --script PATH          Python converter script.
  --snapshot-id ID      Snapshot id used in filenames, e.g. 20250501.
  --run-dir PATH        Run directory for lifecycle outputs, logs, metrics, errors.
  --out-dir PATH         Backward-compatible alias for --run-dir.
  --run-id NAME         Run identifier. Default: snapshot timestamp id.
  --jobs N              Maximum concurrent Python workers. Default: 1.
  --output-schema MODE  Lifecycle schema: extended or legacy. Default: extended.
  --emit-events         Also write optional per-shard event CSVs.
  --force               Rerun shards even when output + successful metrics exist.
  --first-only          Process only the first matching shard.
EOF
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required dependency: $1"
}

utc_now() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
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

file_size() {
  stat -c '%s' "$1"
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

header_for_schema() {
  if [[ "$OUTPUT_SCHEMA" == "legacy" ]]; then
    printf 's,p,o,cdate,cuser,ddate,duser\n'
  else
    printf 's,p,o,cdate,cuser,ddate,duser,entity_id,page_id,crevid,cparentid,drevid,dparentid,source_shard,schema_version,quality_flags\n'
  fi
}

metrics_completed_successfully() {
  local metrics_file="$1"
  [[ -f "$metrics_file" ]] || return 1
  python3 - "$metrics_file" <<'PY'
import json
import sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    sys.exit(1)
sys.exit(0 if data.get("completed_successfully") is True else 1)
PY
}

validate_success_artifacts() {
  local temp_output="$1"
  local expected_header="$2"
  local metrics_file="$3"
  local errors_file="$4"
  validate_temp_header "$temp_output" "$expected_header" || return 1
  metrics_completed_successfully "$metrics_file" || return 1
  [[ -f "$errors_file" ]] || return 1
}

metric_value() {
  local metrics_file="$1"
  local key="$2"
  [[ -f "$metrics_file" ]] || return 0
  python3 - "$metrics_file" "$key" <<'PY'
import json
import sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    print("")
    sys.exit(0)
value = data.get(sys.argv[2], "")
if value is None:
    value = ""
print(value)
PY
}

write_machine_info() {
  {
    echo "date_utc=$(date -u)"
    echo
    echo "hostname"
    hostname || true
    echo
    echo "uname -a"
    uname -a || true
    echo
    echo "lscpu"
    lscpu || true
    echo
    echo "free -h"
    free -h || true
    echo
    echo "df -hT"
    df -hT || true
    echo
    echo "python3 --version"
    python3 --version || true
    echo
    echo "bash --version"
    bash --version || true
  } > "$RUN_DIR/machine_info.txt"
}

next_attempt_dir() {
  local shard_log_dir="$1"
  mkdir -p "$shard_log_dir"
  local n=1
  local name
  while true; do
    name="$(printf 'attempt-%03d' "$n")"
    if mkdir "$shard_log_dir/$name" 2>/dev/null; then
      printf '%s\t%s\n' "$shard_log_dir/$name" "$n"
      return 0
    fi
    n=$((n + 1))
  done
}

write_run_config_start() {
  {
    echo "run_id=$RUN_ID"
    echo "snapshot_id=$SNAPSHOT_ID"
    echo "input_directory=$INPUT_DIR"
    echo "run_directory=$RUN_DIR"
    echo "python_script=$PY_SCRIPT"
    echo "jobs=$JOBS"
    echo "output_schema=$OUTPUT_SCHEMA"
    echo "emit_events=$EMIT_EVENTS"
    echo "start_timestamp_utc=$STARTED_AT_UTC"
    echo "command_line=$COMMAND_LINE"
  } > "$RUN_DIR/run_config.txt"
}

append_run_config_finish() {
  local successful="$1"
  local failed="$2"
  local skipped="$3"
  local elapsed="$4"
  {
    echo "finish_timestamp_utc=$(utc_now)"
    echo "total_elapsed_seconds=$elapsed"
    echo "successful_shards=$successful"
    echo "failed_shards=$failed"
    echo "skipped_shards=$skipped"
  } >> "$RUN_DIR/run_config.txt"
}

write_status() {
  local status_file="$1"
  local shard_id="$2"
  local input_path="$3"
  local output_path="$4"
  local status="$5"
  local exit_code="$6"
  local started="$7"
  local finished="$8"
  local wall_seconds="$9"
  local attempt="${10}"
  {
    printf 'shard_id\tinput_path\toutput_path\tstatus\texit_code\tstarted_at_utc\tfinished_at_utc\twall_seconds\tattempt\n'
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$shard_id" "$input_path" "$output_path" "$status" "$exit_code" "$started" "$finished" "$wall_seconds" "$attempt"
  } > "$status_file"
}

validate_temp_header() {
  local temp_output="$1"
  local expected_header="$2"
  [[ -f "$temp_output" ]] || return 1
  local actual_header
  actual_header="$(head -n 1 "$temp_output" | tr -d '\r')"
  [[ "$actual_header" == "$expected_header" ]]
}

run_worker() {
  local input="$1"
  local base shard_id output_base log_dir attempt_info attempt_dir attempt stdout_log stderr_log system_time status_file latest_status_file
  local final_output temp_output attempt_metrics attempt_errors attempt_events final_metrics final_errors events_file started finished start_s end_s wall_seconds
  local exit_code=0 rows="" publish_failed=0

  base="$(basename "$input")"
  shard_id="$(sanitize_id "$base")"
  output_base="$(expected_output_basename "$base")"
  log_dir="$RUN_DIR/logs/$shard_id"
  mkdir -p "$log_dir"

  latest_status_file="$log_dir/status.tsv"
  final_output="$RUN_DIR/lifecycle/$output_base"
  temp_output="${final_output}.tmp.$$"
  final_metrics="$RUN_DIR/metrics/${shard_id}.metrics.json"
  final_errors="$RUN_DIR/errors/${shard_id}.errors.jsonl"
  events_file="$RUN_DIR/events/${output_base%.csv}.events.csv"

  if [[ "$FORCE" -eq 0 && -s "$final_output" && -f "$final_errors" ]] && metrics_completed_successfully "$final_metrics"; then
    started="$(utc_now)"
    finished="$started"
    echo "[SKIP] $base"
    write_status "$latest_status_file" "$shard_id" "$input" "$final_output" "skipped" 0 "$started" "$finished" 0 0
    return 0
  fi

  attempt_info="$(next_attempt_dir "$log_dir")"
  attempt_dir="${attempt_info%%$'\t'*}"
  attempt="${attempt_info##*$'\t'}"
  stdout_log="$attempt_dir/stdout.log"
  stderr_log="$attempt_dir/stderr.log"
  system_time="$attempt_dir/system_time.txt"
  status_file="$attempt_dir/status.tsv"
  attempt_metrics="$attempt_dir/metrics.json"
  attempt_errors="$attempt_dir/errors.jsonl"
  attempt_events="$attempt_dir/events.csv"

  echo "[START] $base"
  started="$(utc_now)"
  start_s="$(date -u +%s)"

  local cmd=(python3 "$PY_SCRIPT" "$input" --output "$temp_output" --metrics-output "$attempt_metrics" --errors-output "$attempt_errors" --output-schema "$OUTPUT_SCHEMA")
  if [[ "$EMIT_EVENTS" -eq 1 ]]; then
    cmd+=(--emit-events --events-output "$attempt_events")
  fi

  /usr/bin/time -v -o "$system_time" "${cmd[@]}" >"$stdout_log" 2>"$stderr_log"
  exit_code=$?

  end_s="$(date -u +%s)"
  finished="$(utc_now)"
  wall_seconds=$((end_s - start_s))

  if [[ "$exit_code" -eq 0 ]] && validate_success_artifacts "$temp_output" "$(header_for_schema)" "$attempt_metrics" "$attempt_errors"; then
    publish_failed=0
    if ! mv -f "$temp_output" "$final_output"; then
      echo "Failed to publish lifecycle CSV: $temp_output -> $final_output" >> "$stderr_log"
      publish_failed=1
    elif ! cp -f "$attempt_metrics" "$final_metrics"; then
      echo "Failed to publish metrics JSON: $attempt_metrics -> $final_metrics" >> "$stderr_log"
      publish_failed=1
    elif ! cp -f "$attempt_errors" "$final_errors"; then
      echo "Failed to publish errors JSONL: $attempt_errors -> $final_errors" >> "$stderr_log"
      publish_failed=1
    elif [[ "$EMIT_EVENTS" -eq 1 && -f "$attempt_events" ]] && ! cp -f "$attempt_events" "$events_file"; then
      echo "Failed to publish events CSV: $attempt_events -> $events_file" >> "$stderr_log"
      publish_failed=1
    fi

    if [[ "$publish_failed" -eq 0 ]]; then
      rows="$(metric_value "$attempt_metrics" "output_data_rows")"
      if ! write_status "$status_file" "$shard_id" "$input" "$final_output" "success" 0 "$started" "$finished" "$wall_seconds" "$attempt"; then
        echo "Failed to write attempt success status: $status_file" >> "$stderr_log"
        publish_failed=1
      elif ! cp -f "$status_file" "$latest_status_file"; then
        echo "Failed to publish latest status: $status_file -> $latest_status_file" >> "$stderr_log"
        publish_failed=1
      else
        echo "[DONE] $base | seconds=$wall_seconds | rows=$rows"
        return 0
      fi
    fi

    if [[ "$publish_failed" -ne 0 ]]; then
      exit_code=98
      echo "[FAIL] $base | exit_code=$exit_code"
      write_status "$status_file" "$shard_id" "$input" "$final_output" "failed" "$exit_code" "$started" "$finished" "$wall_seconds" "$attempt"
      cp -f "$status_file" "$latest_status_file" 2>/dev/null || true
      return 1
    fi
  fi

  if [[ "$exit_code" -eq 0 ]]; then
    exit_code=99
    echo "Temporary lifecycle output, metrics JSON, or errors JSONL is missing/invalid: $temp_output $attempt_metrics $attempt_errors" >> "$stderr_log"
  fi
  echo "[FAIL] $base | exit_code=$exit_code"
  write_status "$status_file" "$shard_id" "$input" "$final_output" "failed" "$exit_code" "$started" "$finished" "$wall_seconds" "$attempt"
  cp -f "$status_file" "$latest_status_file"
  return 1
}

write_shard_summary() {
  local summary="$RUN_DIR/shard_summary.tsv"
  python3 - "$RUN_DIR" "$SNAPSHOT_ID" "$summary" "${FILES[@]}" <<'PY'
import csv
import json
import os
import sys
from pathlib import Path

run_dir = Path(sys.argv[1])
snapshot_id = sys.argv[2]
summary_path = Path(sys.argv[3])
inputs = [Path(p) for p in sys.argv[4:]]

metric_keys = [
    "pages_processed",
    "pages_skipped_non_entity_namespace",
    "revisions_seen",
    "revisions_with_valid_entity_json",
    "redirect_revisions",
    "triples_extracted_total",
    "triple_add_events",
    "triple_delete_events",
    "output_data_rows",
    "open_lifecycle_rows",
    "closed_lifecycle_rows",
    "warnings",
    "errors",
    "output_bytes",
]

header = [
    "shard_id",
    "input_basename",
    "input_size_bytes",
    "status",
    "exit_code",
    "wall_seconds",
    "maximum_resident_set_kb",
] + metric_keys


def sanitize_id(name: str) -> str:
    for suffix in (".bz2", ".gz", ".xml"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in name)


def read_status(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    return rows[0] if rows else {}


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def read_time_value(path: Path, label: str) -> str:
    if not path.exists():
        return ""
    prefix = label + ":"
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(prefix):
                    return stripped.split(":", 1)[1].strip()
    except Exception:
        return ""
    return ""


def attempt_metrics_path(run_dir: Path, shard_id: str, status: dict) -> Path:
    attempt = status.get("attempt", "")
    if attempt and attempt != "0":
        try:
            return run_dir / "logs" / shard_id / f"attempt-{int(attempt):03d}" / "metrics.json"
        except ValueError:
            pass
    return run_dir / "metrics" / f"{shard_id}.metrics.json"


def attempt_time_path(run_dir: Path, shard_id: str, status: dict) -> Path:
    attempt = status.get("attempt", "")
    if attempt and attempt != "0":
        try:
            return run_dir / "logs" / shard_id / f"attempt-{int(attempt):03d}" / "system_time.txt"
        except ValueError:
            pass
    return run_dir / "logs" / shard_id / "system_time.txt"


with summary_path.open("w", encoding="utf-8", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=header, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    for input_path in inputs:
        base = input_path.name
        shard_id = sanitize_id(base)
        status = read_status(run_dir / "logs" / shard_id / "status.tsv")
        metrics = read_json(attempt_metrics_path(run_dir, shard_id, status))
        time_path = attempt_time_path(run_dir, shard_id, status)
        row = {
            "shard_id": shard_id,
            "input_basename": base,
            "input_size_bytes": str(input_path.stat().st_size) if input_path.exists() else "",
            "status": status.get("status", ""),
            "exit_code": status.get("exit_code", ""),
            "wall_seconds": status.get("wall_seconds", ""),
            "maximum_resident_set_kb": read_time_value(time_path, "Maximum resident set size (kbytes)"),
        }
        for key in metric_keys:
            value = metrics.get(key, "")
            row[key] = "" if value is None else value
        writer.writerow(row)
PY
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --input-dir) INPUT_DIR="${2:-}"; shift 2;;
    --run-dir) RUN_DIR="${2:-}"; shift 2;;
    --out-dir) OUT_DIR="${2:-}"; shift 2;;
    --script) PY_SCRIPT="${2:-}"; shift 2;;
    --snapshot-id) SNAPSHOT_ID="${2:-}"; shift 2;;
    --run-id) RUN_ID="${2:-}"; shift 2;;
    --jobs) JOBS="${2:-}"; shift 2;;
    --output-schema) OUTPUT_SCHEMA="${2:-}"; shift 2;;
    --emit-events) EMIT_EVENTS=1; shift 1;;
    --force) FORCE=1; shift 1;;
    --first-only) FIRST_ONLY=1; shift 1;;
    -h|--help) usage; exit 0;;
    *) die "Unknown arg: $1";;
  esac
done

require_cmd bash
require_cmd python3
require_cmd stat
require_cmd find
require_cmd sort
require_cmd wc
require_cmd awk
require_cmd sed
require_cmd date
[[ -x /usr/bin/time ]] || die "Missing required dependency: /usr/bin/time"
stat -c '%s' "$0" >/dev/null 2>&1 || die "stat does not support GNU -c syntax"

[[ -n "$INPUT_DIR" ]] || die "Missing --input-dir"
[[ -n "$PY_SCRIPT" ]] || die "Missing --script"
[[ -n "$SNAPSHOT_ID" ]] || die "Missing --snapshot-id"
[[ "$SNAPSHOT_ID" =~ ^[0-9]{8}$ ]] || die "--snapshot-id must look like YYYYMMDD"
[[ "$JOBS" =~ ^[0-9]+$ ]] || die "--jobs must be an integer"
[[ "$JOBS" -ge 1 ]] || die "--jobs must be >= 1"
[[ "$OUTPUT_SCHEMA" == "extended" || "$OUTPUT_SCHEMA" == "legacy" ]] || die "--output-schema must be extended or legacy"
[[ -d "$INPUT_DIR" ]] || die "Input dir not found: $INPUT_DIR"
[[ -f "$PY_SCRIPT" ]] || die "Python script not found: $PY_SCRIPT"

INPUT_DIR="$(abs_path "$INPUT_DIR")"
PY_SCRIPT="$(abs_path "$PY_SCRIPT")"
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

mkdir -p "$RUN_DIR/lifecycle" "$RUN_DIR/logs" "$RUN_DIR/metrics" "$RUN_DIR/errors"
if [[ "$EMIT_EVENTS" -eq 1 ]]; then
  mkdir -p "$RUN_DIR/events"
fi

mapfile -d '' FILES < <(find "$INPUT_DIR" -maxdepth 1 -type f -name "wikidatawiki-${SNAPSHOT_ID}-pages-meta-history*.bz2" -print0 | sort -z)
if [[ "$FIRST_ONLY" -eq 1 && "${#FILES[@]}" -gt 1 ]]; then
  FILES=("${FILES[0]}")
fi
TOTAL="${#FILES[@]}"
[[ "$TOTAL" -gt 0 ]] || die "No matching .bz2 files found for snapshot $SNAPSHOT_ID in: $INPUT_DIR"

STARTED_AT_UTC="$(utc_now)"
START_SECONDS="$(date -u +%s)"
write_machine_info
write_run_config_start

echo "Run ID: $RUN_ID"
echo "Snapshot: $SNAPSHOT_ID"
echo "Run directory: $RUN_DIR"
echo "Input shards: $TOTAL"
echo "Python script: $PY_SCRIPT"
echo "Jobs: $JOBS"
echo "Output schema: $OUTPUT_SCHEMA"
echo "Emit events: $EMIT_EVENTS"
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

write_shard_summary

successful="$(awk -F'\t' 'NR > 1 && $4 == "success" {n++} END {print n+0}' "$RUN_DIR/shard_summary.tsv")"
skipped="$(awk -F'\t' 'NR > 1 && $4 == "skipped" {n++} END {print n+0}' "$RUN_DIR/shard_summary.tsv")"
failed="$(awk -F'\t' 'NR > 1 && $4 != "success" && $4 != "skipped" {n++} END {print n+0}' "$RUN_DIR/shard_summary.tsv")"
FINISH_SECONDS="$(date -u +%s)"
elapsed=$((FINISH_SECONDS - START_SECONDS))
append_run_config_finish "$successful" "$failed" "$skipped" "$elapsed"

echo
echo "Run finished: success=$successful failed=$failed skipped=$skipped elapsed_seconds=$elapsed"
echo "Summary: $RUN_DIR/shard_summary.tsv"

if [[ "$failed" -gt 0 ]]; then
  exit 1
fi
exit 0
