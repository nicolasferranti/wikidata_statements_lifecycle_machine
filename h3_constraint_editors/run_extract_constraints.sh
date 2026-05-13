#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<EOF
Usage:
  $0 INPUT_DIR OUTPUT_DIR PYTHON_SCRIPT [JOBS]

Arguments:
  INPUT_DIR      Folder containing input CSV files
  OUTPUT_DIR     Folder where output CSV files will be written
  PYTHON_SCRIPT  Path to extract_constraints.py
  JOBS           Number of parallel jobs (default: 1)

Example:
  $0 /data/input output ./extract_constraints.py 8
EOF
}

if [[ $# -lt 3 || $# -gt 4 ]]; then
  usage
  exit 1
fi

INPUT_DIR="$1"
OUTPUT_DIR="$2"
PYTHON_SCRIPT="$3"
JOBS="${4:-1}"

if [[ ! -d "$INPUT_DIR" ]]; then
  echo "ERROR: input directory does not exist: $INPUT_DIR" >&2
  exit 1
fi

if [[ ! -f "$PYTHON_SCRIPT" ]]; then
  echo "ERROR: python script does not exist: $PYTHON_SCRIPT" >&2
  exit 1
fi

if ! [[ "$JOBS" =~ ^[0-9]+$ ]] || [[ "$JOBS" -lt 1 ]]; then
  echo "ERROR: JOBS must be a positive integer" >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"
LOG_DIR="$OUTPUT_DIR/logs"
mkdir -p "$LOG_DIR"

export INPUT_DIR OUTPUT_DIR PYTHON_SCRIPT LOG_DIR

process_file() {
  local input_file="$1"
  local base_name
  local output_file
  local log_file
  local tmp_log
  local status

  base_name="$(basename "$input_file")"
  output_file="$OUTPUT_DIR/${base_name%.csv}.constraints_only.csv"
  log_file="$LOG_DIR/${base_name%.csv}.log"
  tmp_log="$LOG_DIR/${base_name%.csv}.tmp.log"

  # Resume support: skip already finished outputs
  if [[ -s "$output_file" ]]; then
    echo "SKIP  | $base_name | output exists"
    return 0
  fi

  echo "START | $base_name"

  if python3 "$PYTHON_SCRIPT" "$input_file" --output "$OUTPUT_DIR" >"$tmp_log" 2>&1; then
    mv "$tmp_log" "$log_file"
    echo "DONE  | $base_name"
    return 0
  else
    status=$?
    mv "$tmp_log" "$log_file" || true
    echo "FAIL  | $base_name | see $log_file" >&2
    return "$status"
  fi
}

export -f process_file

mapfile -d '' FILES < <(find "$INPUT_DIR" -maxdepth 1 -type f -name '*.csv' -print0 | sort -z)

TOTAL="${#FILES[@]}"
if [[ "$TOTAL" -eq 0 ]]; then
  echo "No CSV files found in $INPUT_DIR"
  exit 0
fi

echo "Found $TOTAL CSV files"
echo "Running with $JOBS parallel job(s)"
echo "Outputs: $OUTPUT_DIR"
echo

printf '%s\0' "${FILES[@]}" | xargs -0 -n 1 -P "$JOBS" -I {} bash -c 'process_file "$@"' _ {}

echo
echo "Finished."
echo "Successful files were written to: $OUTPUT_DIR"
echo "Per-file logs are in: $LOG_DIR"
