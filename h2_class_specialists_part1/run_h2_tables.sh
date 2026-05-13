#!/usr/bin/env bash
# Exit on error, on use of undefined variables, and on pipeline errors.
set -euo pipefail

# -----------------------------
# USAGE
# -----------------------------
# ./run_h2_tables.sh INPUT_DIR PARALLELISM [RESUME_FROM]
#
# INPUT_DIR    = folder containing the CSV files
# PARALLELISM  = number of files to process in parallel
# RESUME_FROM  = optional basename of the file from which to start
#
# EXAMPLES
# Process all CSV files in ./data using 4 parallel workers:
#   ./run_h2_tables.sh ./data 4
#
# Resume starting from a specific file name:
#   ./run_h2_tables.sh ./data 4 20250501-triple-pages-meta-history9-p13886345p13998367.csv
#
# NOTES
# - The script skips files already marked as successfully completed.
# - The Python worker writes outputs atomically, so partial files are not treated as done.
# - Output goes under ./output by default.
# -----------------------------

# Read positional arguments.
INPUT_DIR="${1:-}"
PARALLELISM="${2:-}"
RESUME_FROM="${3:-}"

# Validate required arguments.
if [[ -z "${INPUT_DIR}" || -z "${PARALLELISM}" ]]; then
  echo "Usage: $0 INPUT_DIR PARALLELISM [RESUME_FROM]" >&2
  exit 1
fi

# Validate that the input directory exists.
if [[ ! -d "${INPUT_DIR}" ]]; then
  echo "ERROR: INPUT_DIR does not exist: ${INPUT_DIR}" >&2
  exit 1
fi

# Validate that PARALLELISM is a positive integer.
if ! [[ "${PARALLELISM}" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: PARALLELISM must be a positive integer." >&2
  exit 1
fi

# Resolve this script's directory so we can find the Python worker next to it.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Point to the Python worker script.
PYTHON_WORKER="${SCRIPT_DIR}/build_h2_tables.py"

# Check that the Python worker exists.
if [[ ! -f "${PYTHON_WORKER}" ]]; then
  echo "ERROR: Python worker not found at ${PYTHON_WORKER}" >&2
  exit 1
fi

# Choose a top-level output directory.
OUTPUT_ROOT="${SCRIPT_DIR}/output"

# Create the output subdirectories.
mkdir -p "${OUTPUT_ROOT}/table1"
mkdir -p "${OUTPUT_ROOT}/table1b"
mkdir -p "${OUTPUT_ROOT}/table2"
mkdir -p "${OUTPUT_ROOT}/state"
mkdir -p "${OUTPUT_ROOT}/logs"

# Build a deterministic, sorted list of CSV files.
# Using find + sort keeps the run reproducible.
mapfile -t ALL_FILES < <(find "${INPUT_DIR}" -maxdepth 1 -type f -name '*.csv' | sort)

# Stop early if no CSVs were found.
if [[ "${#ALL_FILES[@]}" -eq 0 ]]; then
  echo "No CSV files found in ${INPUT_DIR}" >&2
  exit 0
fi

# Prepare the work list after applying optional RESUME_FROM.
WORK_LIST=()
START_ADDING=0

# If RESUME_FROM is empty, start from the first file.
if [[ -z "${RESUME_FROM}" ]]; then
  START_ADDING=1
fi

# Iterate through all discovered files in sorted order.
for FILEPATH in "${ALL_FILES[@]}"; do
  # Extract just the basename for easier comparison and naming.
  BASENAME="$(basename "${FILEPATH}")"

  # If RESUME_FROM was provided, start adding only once we reach it.
  if [[ "${START_ADDING}" -eq 0 && "${BASENAME}" == "${RESUME_FROM}" ]]; then
    START_ADDING=1
  fi

  # Skip files before RESUME_FROM.
  if [[ "${START_ADDING}" -eq 0 ]]; then
    continue
  fi

  # Append this file to the work list.
  WORK_LIST+=("${FILEPATH}")
done

# If RESUME_FROM was provided but not found, fail clearly.
if [[ -n "${RESUME_FROM}" && "${#WORK_LIST[@]}" -eq 0 ]]; then
  echo "ERROR: RESUME_FROM file not found in sorted file list: ${RESUME_FROM}" >&2
  exit 1
fi

# Create a temporary file that will hold the list of files to process.
JOB_LIST_FILE="$(mktemp)"

# Ensure the temp file is deleted when the script exits.
cleanup() {
  rm -f "${JOB_LIST_FILE}"
}
trap cleanup EXIT

# Write the list of candidate files, one per line.
printf '%s\n' "${WORK_LIST[@]}" > "${JOB_LIST_FILE}"

# Export variables so the subshell launched by xargs can use them.
export PYTHON_WORKER
export OUTPUT_ROOT

# Process files in parallel.
# xargs -P controls how many Python workers run concurrently.
# Each worker is responsible for its own restart-safe output writing.
cat "${JOB_LIST_FILE}" | xargs -I{} -P "${PARALLELISM}" bash -c '
  # Store the current file path.
  FILEPATH="$1"

  # Get the basename to name logs and state files.
  BASENAME="$(basename "${FILEPATH}")"

  # Remove the .csv extension for cleaner output filenames.
  STEM="${BASENAME%.csv}"

  # Define where we store the success marker.
  DONE_MARKER="${OUTPUT_ROOT}/state/${STEM}.done"

  # Define a log file for stdout/stderr of this worker.
  LOG_FILE="${OUTPUT_ROOT}/logs/${STEM}.log"

  # If already done, skip immediately.
  if [[ -f "${DONE_MARKER}" ]]; then
    echo "SKIP (already done): ${BASENAME}"
    exit 0
  fi

  # Run the Python worker.
  # We redirect both stdout and stderr to a per-file log.
  # The worker itself is idempotent and restart-safe.
  if python3 "${PYTHON_WORKER}" \
      --input "${FILEPATH}" \
      --output-root "${OUTPUT_ROOT}" \
      > "${LOG_FILE}" 2>&1
  then
    # Create a completion marker only if the worker exited successfully.
    touch "${DONE_MARKER}"
    echo "DONE: ${BASENAME}"
  else
    # Print an informative error and leave no done marker.
    echo "FAILED: ${BASENAME} (see ${LOG_FILE})" >&2
    exit 1
  fi
' _ {}

# Final message.
echo "All requested files have been processed or skipped if already completed."
echo "Per-file outputs are under: ${OUTPUT_ROOT}"
echo "Logs are under: ${OUTPUT_ROOT}/logs"
echo "Done markers are under: ${OUTPUT_ROOT}/state"
