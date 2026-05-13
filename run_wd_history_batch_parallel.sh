#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   nohup ./run_wd_history_batch_parallel.sh \
#     --input-dir /bigdata-nfs/wikidata/pages-history/wikidata_pages_meta_history \
#     --out-dir plain_edit_history_triples \
#     --script wd_history_rdf_csv.py \
#     --jobs 4 \
#     [--first-only] \
#     > batch_run.log 2>&1 &

INPUT_DIR=""
OUT_DIR=""
PY_SCRIPT=""
JOBS=1
FIRST_ONLY=0

die() { echo "ERROR: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --input-dir) INPUT_DIR="${2:-}"; shift 2;;
    --out-dir)   OUT_DIR="${2:-}"; shift 2;;
    --script)    PY_SCRIPT="${2:-}"; shift 2;;
    --jobs)      JOBS="${2:-}"; shift 2;;
    --first-only) FIRST_ONLY=1; shift 1;;
    -h|--help)
      echo "See header in script for usage."
      exit 0
      ;;
    *) die "Unknown arg: $1";;
  esac
done

[[ -n "$INPUT_DIR" ]] || die "Missing --input-dir"
[[ -n "$OUT_DIR" ]]   || die "Missing --out-dir"
[[ -n "$PY_SCRIPT" ]] || die "Missing --script"
[[ -d "$INPUT_DIR" ]] || die "Input dir not found: $INPUT_DIR"
[[ -f "$PY_SCRIPT" ]] || die "Python script not found: $PY_SCRIPT"
[[ "$JOBS" =~ ^[0-9]+$ ]] || die "--jobs must be an integer"
[[ "$JOBS" -ge 1 ]] || die "--jobs must be >= 1"

mkdir -p "$OUT_DIR"

# 1) list files
mapfile -d '' FILES < <(find "$INPUT_DIR" -maxdepth 1 -type f \
  -name 'wikidatawiki-20250501-pages-meta-history*.bz2' -print0 | sort -z)

TOTAL="${#FILES[@]}"
[[ "$TOTAL" -gt 0 ]] || die "No matching .bz2 files found in: $INPUT_DIR"

echo "Found $TOTAL input files"
echo "Output directory: $OUT_DIR"
echo "Python script: $PY_SCRIPT"
echo "Parallel jobs: $JOBS"
echo "First-only mode: $FIRST_ONLY"
echo

if [[ "$FIRST_ONLY" -eq 1 ]]; then
  FILES=("${FILES[0]}")
  TOTAL=1
fi

# Create a temp list with one file per line (safe: newline-separated, filenames here normally have no newlines)
LIST_FILE="$(mktemp)"
trap 'rm -f "$LIST_FILE"' EXIT
printf "%s\n" "${FILES[@]}" > "$LIST_FILE"

# Worker: compute output name, skip if exists, run python, log timing + sizes.
worker() {
  local f="$1"
  local base tmp x yz out
  base="$(basename "$f")"

  tmp="${base#wikidatawiki-20250501-pages-meta-history}"   # "6.xml-p6051690p6052571.bz2"
  x="${tmp%%.xml-p*}"                                     # "6"
  yz="${tmp#*.xml-p}"                                     # "6051690p6052571.bz2"
  yz="${yz%.bz2}"                                         # "6051690p6052571"

  out="${OUT_DIR}/20250501-triple-pages-meta-history${x}-p${yz}.csv"

  # 2) skip if output exists and is non-empty
  if [[ -s "$out" ]]; then
    echo "[SKIP] $base -> $(basename "$out") (already exists, non-empty)"
    return 0
  fi

  local size_human size_bytes start end elapsed
  size_bytes="$(stat -c '%s' "$f" 2>/dev/null || echo 0)"
  size_human="$(du -h "$f" | awk '{print $1}')"

  echo "[START] $base | input=${size_human} (${size_bytes} bytes) | out=$(basename "$out")"
  start="$(date +%s)"

  # Write to a temp file then atomically move into place (prevents partial outputs on interruption)
  local tmpout="${out}.tmp.$$"
  rm -f "$tmpout"

  /usr/bin/time -p python3 "$PY_SCRIPT" "$f" -o "$tmpout"

  mv -f "$tmpout" "$out"

  end="$(date +%s)"
  elapsed=$(( end - start ))

  local out_size out_lines
  out_size="$(du -h "$out" | awk '{print $1}')"
  out_lines="$(wc -l < "$out" | tr -d ' ')"
  echo "[DONE ] $base | ${elapsed}s | out=${out_size} | lines=${out_lines}"
}

export -f worker
export OUT_DIR PY_SCRIPT

overall_start="$(date +%s)"

# 3) parallel execution
# NOTE: xargs -P runs multiple independent python processes (best speedup vs threads).
# The -n 1 passes one filename per worker invocation.
cat "$LIST_FILE" | xargs -n 1 -P "$JOBS" -I {} bash -lc 'worker "$@"' _ {}

overall_end="$(date +%s)"
echo
echo "All done. Total elapsed: $(( overall_end - overall_start ))s"

