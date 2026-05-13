#!/bin/bash
set -euo pipefail

# Usage:
#   ./run_class_edit_counts_parallel.sh CORES P31_DIR WDT_DIR OUTPUT_DIR
#
# Example:
#   ./run_class_edit_counts_parallel.sh 16 \
#     /bigdata-nfs/wd_history_work/h2_class_specialists_part2/wdt_P31 \
#     /bigdata-nfs/wd_history_work/h3_constraint_editors/wdt_folder \
#     /bigdata-nfs/wd_history_work/h2_class_specialists_part2/class_edit_counts

if [[ $# -ne 4 ]]; then
  echo "Usage: $0 CORES P31_DIR WDT_DIR OUTPUT_DIR" >&2
  exit 1
fi

CORES="$1"
P31_DIR="$2"
WDT_DIR="$3"
OUTPUT_DIR="$4"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="${SCRIPT_DIR}/count_class_editors_one_file.py"

if ! [[ "$CORES" =~ ^[0-9]+$ ]] || [[ "$CORES" -lt 1 ]]; then
  echo "CORES must be a positive integer, got: $CORES" >&2
  exit 1
fi

if [[ ! -d "$P31_DIR" ]]; then
  echo "P31 directory does not exist: $P31_DIR" >&2
  exit 1
fi

if [[ ! -d "$WDT_DIR" ]]; then
  echo "WDT directory does not exist: $WDT_DIR" >&2
  exit 1
fi

if [[ ! -f "$PY_SCRIPT" ]]; then
  echo "Python script not found: $PY_SCRIPT" >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"

echo "P31 input:  $P31_DIR"
echo "WDT input:  $WDT_DIR"
echo "Output:     $OUTPUT_DIR"
echo "Cores:      $CORES"

find "$P31_DIR" -maxdepth 1 -type f -name '*-P31.csv' -print0 \
  | xargs -0 -P "$CORES" -I {} bash -c '
      p31_file="$1"
      py_script="$2"
      wdt_dir="$3"
      output_dir="$4"

      base="$(basename "$p31_file")"
      stem="${base%-P31.csv}"
      wdt_file="${wdt_dir}/${stem}.csv"

      if [[ ! -f "$wdt_file" ]]; then
        echo "MISSING matching WDT file for $p31_file: $wdt_file" >&2
        exit 1
      fi

      python3 "$py_script" "$p31_file" "$wdt_file" "$output_dir"
    ' _ {} "$PY_SCRIPT" "$WDT_DIR" "$OUTPUT_DIR"

echo "All done."
