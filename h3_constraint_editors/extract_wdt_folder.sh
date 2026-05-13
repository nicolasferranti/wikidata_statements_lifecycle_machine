#!/usr/bin/env bash

set -euo pipefail

if [ "$#" -lt 4 ]; then
  echo "Usage: $0 <input_dir> <output_dir> <cores> <python_script>"
  echo "Example: $0 ./csv_files ./wdt_only 8 ./extract_wdt_triples.py"
  exit 1
fi

INPUT_DIR="$1"
OUTPUT_DIR="$2"
CORES="$3"
PYTHON_SCRIPT="$4"

mkdir -p "$OUTPUT_DIR"

LOG_FILE="$OUTPUT_DIR/completed_files.log"
ERROR_LOG="$OUTPUT_DIR/errors.log"

touch "$LOG_FILE"
touch "$ERROR_LOG"

process_file() {
  local input_file="$1"
  local filename
  local output_file

  filename="$(basename "$input_file")"
  output_file="$OUTPUT_DIR/$filename"

  if grep -Fxq "$filename" "$LOG_FILE"; then
    echo "SKIP already completed: $filename"
    return 0
  fi

  echo "START $filename"

  if python3 "$PYTHON_SCRIPT" --input "$input_file" --output "$output_file"; then
    echo "$filename" >> "$LOG_FILE"
    echo "DONE $filename"
  else
    echo "ERROR $filename" >> "$ERROR_LOG"
    rm -f "$output_file"
    return 1
  fi
}

export OUTPUT_DIR
export LOG_FILE
export ERROR_LOG
export PYTHON_SCRIPT
export -f process_file

find "$INPUT_DIR" -maxdepth 1 -type f -name "*.csv" | sort | \
  xargs -n 1 -P "$CORES" -I {} bash -c 'process_file "$@"' _ {}

echo "All possible files processed."
echo "Completed log: $LOG_FILE"
echo "Error log: $ERROR_LOG"
