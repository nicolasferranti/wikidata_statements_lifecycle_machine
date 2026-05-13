#!/bin/bash
set -euo pipefail

# Usage:
#   ./run_filter_wdt_parallel.sh CORES [INPUT_DIR] [P279_OUTPUT_DIR] [P31_OUTPUT_DIR]

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 CORES [INPUT_DIR] [P279_OUTPUT_DIR] [P31_OUTPUT_DIR]" >&2
  exit 1
fi

CORES="$1"
INPUT_DIR="${2:-/bigdata-nfs/wd_history_work/h3_constraint_editors/wdt_folder}"
P279_OUTPUT_DIR="${3:-/bigdata-nfs/wd_history_work/h3_constraint_editors/wdt_P279}"
P31_OUTPUT_DIR="${4:-/bigdata-nfs/wd_history_work/h3_constraint_editors/wdt_P31}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="${SCRIPT_DIR}/filter_wdt_p31_p279.py"

if ! [[ "$CORES" =~ ^[0-9]+$ ]] || [[ "$CORES" -lt 1 ]]; then
  echo "CORES must be a positive integer, got: $CORES" >&2
  exit 1
fi

if [[ ! -d "$INPUT_DIR" ]]; then
  echo "Input directory does not exist: $INPUT_DIR" >&2
  exit 1
fi

if [[ ! -f "$PY_SCRIPT" ]]; then
  echo "Python script not found: $PY_SCRIPT" >&2
  exit 1
fi

mkdir -p "$P279_OUTPUT_DIR" "$P31_OUTPUT_DIR"

echo "Input:      $INPUT_DIR"
echo "P279 out:   $P279_OUTPUT_DIR"
echo "P31 out:    $P31_OUTPUT_DIR"
echo "Cores:      $CORES"

find "$INPUT_DIR" -maxdepth 1 -type f -name '*.csv' -print0 \
  | xargs -0 -P "$CORES" -I {} \
      python3 "$PY_SCRIPT" "{}" "$P279_OUTPUT_DIR" "$P31_OUTPUT_DIR"

echo "All done."
