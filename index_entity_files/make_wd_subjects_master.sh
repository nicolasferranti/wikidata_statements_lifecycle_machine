#!/usr/bin/env bash
set -Eeuo pipefail

INPUT_DIR="${1:-../plain_edit_history_triples}"
OUT_DIR="${2:-wd_subjects_by_history_file}"
JOBS="${3:-4}"
LOG_DIR="${4:-logs_wd_subjects}"

mkdir -p "$OUT_DIR" "$LOG_DIR"

MASTER_LOG="$LOG_DIR/master.log"
FAILED_LIST="$LOG_DIR/failed_files.txt"

: > "$MASTER_LOG"
: > "$FAILED_LIST"

echo "Input dir: $INPUT_DIR" | tee -a "$MASTER_LOG"
echo "Output dir: $OUT_DIR" | tee -a "$MASTER_LOG"
echo "Jobs: $JOBS" | tee -a "$MASTER_LOG"
echo "Log dir: $LOG_DIR" | tee -a "$MASTER_LOG"

find "$INPUT_DIR" -maxdepth 1 -type f -name "*.csv" | sort > "$LOG_DIR/input_files.txt"

TOTAL=$(wc -l < "$LOG_DIR/input_files.txt")
echo "Total input files: $TOTAL" | tee -a "$MASTER_LOG"

worker_one() {
    file="$1"
    base="$(basename "$file")"
    log_file="$LOG_DIR/${base}.log"

    if python3 -u extract_wd_subjects_worker.py \
        --input "$file" \
        --out-dir "$OUT_DIR" \
        > "$log_file" 2>&1
    then
        echo "OK $base"
    else
        echo "FAIL $base"
        echo "$file" >> "$FAILED_LIST"
        return 1
    fi
}

export -f worker_one
export OUT_DIR LOG_DIR

cat "$LOG_DIR/input_files.txt" | xargs -P "$JOBS" -n 1 -I {} bash -c 'worker_one "$@"' _ {}

echo "Finished master run." | tee -a "$MASTER_LOG"
echo "Failed files:" | tee -a "$MASTER_LOG"
wc -l "$FAILED_LIST" | tee -a "$MASTER_LOG"
