#!/usr/bin/env bash
# Exit on error, on use of undefined variables, and on pipeline errors.
set -euo pipefail
# -----------------------------
# USAGE
# -----------------------------
# ./run_user_class_edit_counts.sh OUTPUT_ROOT PARALLELISM [RESUME_FROM]
#
# OUTPUT_ROOT  = folder containing the table1b/ and table2/ subdirectories
# PARALLELISM  = number of file pairs to process in parallel
# RESUME_FROM  = optional stem (without extension) of the file pair from which to start
#                e.g. 20250501-triple-pages-meta-history1-p1003p1106
#
# EXAMPLES
# Process all file pairs under ./output using 4 parallel workers:
#   ./run_user_class_edit_counts.sh ./output 4
#
# Resume starting from a specific stem:
#   ./run_user_class_edit_counts.sh ./output 4 20250501-triple-pages-meta-history9-p13886345p13998367
#
# NOTES
# - The script skips pairs already marked as successfully completed.
# - A pair is matched by shared stem: the filename without its table-suffix and extension.
# - Output goes under OUTPUT_ROOT/user_class_edit_counts by default.
# -----------------------------

INPUT_ROOT="${1:-}"
PARALLELISM="${2:-}"
RESUME_FROM="${3:-}"

if [[ -z "${INPUT_ROOT}" || -z "${PARALLELISM}" ]]; then
  echo "Usage: $0 OUTPUT_ROOT PARALLELISM [RESUME_FROM]" >&2
  exit 1
fi

if [[ ! -d "${INPUT_ROOT}" ]]; then
  echo "ERROR: OUTPUT_ROOT does not exist: ${INPUT_ROOT}" >&2
  exit 1
fi

if ! [[ "${PARALLELISM}" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: PARALLELISM must be a positive integer." >&2
  exit 1
fi

TABLE1B_DIR="${INPUT_ROOT}/table1b"
TABLE2_DIR="${INPUT_ROOT}/table2"

if [[ ! -d "${TABLE1B_DIR}" ]]; then
  echo "ERROR: table1b directory not found: ${TABLE1B_DIR}" >&2
  exit 1
fi

if [[ ! -d "${TABLE2_DIR}" ]]; then
  echo "ERROR: table2 directory not found: ${TABLE2_DIR}" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_WORKER="${SCRIPT_DIR}/build_user_class_edit_counts.py"

if [[ ! -f "${PYTHON_WORKER}" ]]; then
  echo "ERROR: Python worker not found at ${PYTHON_WORKER}" >&2
  exit 1
fi

OUTPUT_SUBDIR="${INPUT_ROOT}/user_class_edit_counts"
STATE_DIR="${INPUT_ROOT}/state_ucec"
LOGS_DIR="${INPUT_ROOT}/logs_ucec"

mkdir -p "${OUTPUT_SUBDIR}"
mkdir -p "${STATE_DIR}"
mkdir -p "${LOGS_DIR}"

# Build a sorted list of stems from table1b files.
# The stem is the filename with the .table1b.csv suffix stripped.
mapfile -t ALL_STEMS < <(
  find "${TABLE1B_DIR}" -maxdepth 1 -type f -name '*.table1b.csv' \
    | sort \
    | xargs -I{} basename {} .table1b.csv
)

if [[ "${#ALL_STEMS[@]}" -eq 0 ]]; then
  echo "No .table1b.csv files found in ${TABLE1B_DIR}" >&2
  exit 0
fi

# Validate that every stem also has a matching table2 file.
VALID_STEMS=()
for STEM in "${ALL_STEMS[@]}"; do
  T2="${TABLE2_DIR}/${STEM}.table2.csv"
  if [[ -f "${T2}" ]]; then
    VALID_STEMS+=("${STEM}")
  else
    echo "WARNING: No matching table2 file for stem '${STEM}', skipping." >&2
  fi
done

if [[ "${#VALID_STEMS[@]}" -eq 0 ]]; then
  echo "No valid stem pairs found. Exiting." >&2
  exit 1
fi

# Apply RESUME_FROM filtering.
WORK_LIST=()
START_ADDING=0

if [[ -z "${RESUME_FROM}" ]]; then
  START_ADDING=1
fi

for STEM in "${VALID_STEMS[@]}"; do
  if [[ "${START_ADDING}" -eq 0 && "${STEM}" == "${RESUME_FROM}" ]]; then
    START_ADDING=1
  fi
  if [[ "${START_ADDING}" -eq 0 ]]; then
    continue
  fi
  WORK_LIST+=("${STEM}")
done

if [[ -n "${RESUME_FROM}" && "${#WORK_LIST[@]}" -eq 0 ]]; then
  echo "ERROR: RESUME_FROM stem not found in sorted stem list: ${RESUME_FROM}" >&2
  exit 1
fi

JOB_LIST_FILE="$(mktemp)"
cleanup() {
  rm -f "${JOB_LIST_FILE}"
}
trap cleanup EXIT

printf '%s\n' "${WORK_LIST[@]}" > "${JOB_LIST_FILE}"

export PYTHON_WORKER
export INPUT_ROOT
export TABLE1B_DIR
export TABLE2_DIR
export OUTPUT_SUBDIR
export STATE_DIR
export LOGS_DIR

cat "${JOB_LIST_FILE}" | xargs -I{} -P "${PARALLELISM}" bash -c '
  STEM="$1"
  DONE_MARKER="${STATE_DIR}/${STEM}.ucec.done"
  LOG_FILE="${LOGS_DIR}/${STEM}.ucec.log"
  TABLE1B_FILE="${TABLE1B_DIR}/${STEM}.table1b.csv"
  TABLE2_FILE="${TABLE2_DIR}/${STEM}.table2.csv"

  if [[ -f "${DONE_MARKER}" ]]; then
    echo "SKIP (already done): ${STEM}"
    exit 0
  fi

  if python3 "${PYTHON_WORKER}" \
      --table1b "${TABLE1B_FILE}" \
      --table2  "${TABLE2_FILE}" \
      > "${LOG_FILE}" 2>&1
  then
    touch "${DONE_MARKER}"
    echo "DONE: ${STEM}"
  else
    echo "FAILED: ${STEM} (see ${LOG_FILE})" >&2
    exit 1
  fi
' _ {}

echo "All requested pairs have been processed or skipped if already completed."
echo "Outputs are under:    ${OUTPUT_SUBDIR}"
echo "Logs are under:       ${LOGS_DIR}"
echo "Done markers are under: ${STATE_DIR}"
