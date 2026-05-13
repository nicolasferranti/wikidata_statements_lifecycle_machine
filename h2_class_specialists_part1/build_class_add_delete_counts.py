#!/usr/bin/env python3
# Import argparse to parse command-line arguments.
import argparse

# Import glob to find CSV files in input folders.
import glob

# Import csv for CSV reading and writing.
import csv

# Import os for path handling.
import os

# Import sys for exit codes and stderr output.
import sys

# Import defaultdict for aggregation.
from collections import defaultdict


def normalize_header(name: str) -> str:
    """Normalize a header field by stripping spaces and lowercasing."""
    return (name or "").strip().lower()


def safe_get(row: dict, key: str) -> str:
    """Safely read a field from a CSV row and strip surrounding whitespace."""
    return (row.get(key, "") or "").strip()


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Count per-class additions from table1 files and deletions from table1b files, "
            "then write one merged CSV."
        )
    )

    parser.add_argument(
        "--table1-dir",
        required=True,
        help="Folder containing table1 CSV files (class additions).",
    )

    parser.add_argument(
        "--table1b-dir",
        required=True,
        help="Folder containing table1b CSV files (class deletions).",
    )

    parser.add_argument(
        "--output",
        required=True,
        help="Path to output CSV file.",
    )

    return parser.parse_args()


def process_table1_files(table1_dir: str, counts: dict[str, dict[str, int]]) -> tuple[int, int]:
    """
    Read all table1 CSV files and increment added counts by class.

    Returns:
      (file_count, row_count)
    """
    # Find all CSV files in the table1 directory.
    files = sorted(glob.glob(os.path.join(table1_dir, "*.csv")))

    # Fail clearly if no files were found.
    if not files:
        raise ValueError(f"No CSV files found in table1 directory: {table1_dir}")

    # Track processed file and row counts.
    file_count = 0
    row_count = 0

    # Process each table1 file.
    for i, path in enumerate(files, start=1):
        file_count += 1
        name = os.path.basename(path)

        # Print progress.
        print(f"[table1 {i}/{len(files)}] reading {name} ...", flush=True)

        # Open the current CSV file.
        with open(path, "r", encoding="utf-8", newline="") as f:
            # Create a DictReader.
            reader = csv.DictReader(f)

            # Validate header existence.
            if reader.fieldnames is None:
                raise ValueError(f"No header found in file: {path}")

            # Normalize and validate header names.
            normalized_fieldnames = [normalize_header(x) for x in reader.fieldnames]
            required = {"item", "class", "cuser"}

            if not required.issubset(set(normalized_fieldnames)):
                raise ValueError(
                    f"Missing required columns in {path}. "
                    f"Found={normalized_fieldnames}, required={sorted(required)}"
                )

            # Map original header names to normalized names.
            original_to_normalized = dict(zip(reader.fieldnames, normalized_fieldnames))

            # Read rows.
            for raw_row in reader:
                row_count += 1

                # Normalize row keys.
                row = {
                    original_to_normalized[k]: (v if v is not None else "")
                    for k, v in raw_row.items()
                }

                # Read the class field.
                cls = safe_get(row, "class")

                # Skip incomplete rows.
                if not cls:
                    continue

                # Increment added count for this class.
                counts[cls]["n_instances_added"] += 1

    # Return summary counters.
    return file_count, row_count


def process_table1b_files(table1b_dir: str, counts: dict[str, dict[str, int]]) -> tuple[int, int]:
    """
    Read all table1b CSV files and increment deleted counts by class.

    Returns:
      (file_count, row_count)
    """
    # Find all CSV files in the table1b directory.
    files = sorted(glob.glob(os.path.join(table1b_dir, "*.csv")))

    # Fail clearly if no files were found.
    if not files:
        raise ValueError(f"No CSV files found in table1b directory: {table1b_dir}")

    # Track processed file and row counts.
    file_count = 0
    row_count = 0

    # Process each table1b file.
    for i, path in enumerate(files, start=1):
        file_count += 1
        name = os.path.basename(path)

        # Print progress.
        print(f"[table1b {i}/{len(files)}] reading {name} ...", flush=True)

        # Open the current CSV file.
        with open(path, "r", encoding="utf-8", newline="") as f:
            # Create a DictReader.
            reader = csv.DictReader(f)

            # Validate header existence.
            if reader.fieldnames is None:
                raise ValueError(f"No header found in file: {path}")

            # Normalize and validate header names.
            normalized_fieldnames = [normalize_header(x) for x in reader.fieldnames]
            required = {"item", "class", "duser", "ddate"}

            if not required.issubset(set(normalized_fieldnames)):
                raise ValueError(
                    f"Missing required columns in {path}. "
                    f"Found={normalized_fieldnames}, required={sorted(required)}"
                )

            # Map original header names to normalized names.
            original_to_normalized = dict(zip(reader.fieldnames, normalized_fieldnames))

            # Read rows.
            for raw_row in reader:
                row_count += 1

                # Normalize row keys.
                row = {
                    original_to_normalized[k]: (v if v is not None else "")
                    for k, v in raw_row.items()
                }

                # Read the class field.
                cls = safe_get(row, "class")

                # Skip incomplete rows.
                if not cls:
                    continue

                # Increment deleted count for this class.
                counts[cls]["n_instances_deleted"] += 1

    # Return summary counters.
    return file_count, row_count


def write_output(output_path: str, counts: dict[str, dict[str, int]]) -> int:
    """
    Write the merged class-level add/delete counts.

    Output schema:
      class,n_instances_added,n_instances_deleted

    Returns:
      number of output rows written
    """
    # Ensure output directory exists if needed.
    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    # Count written rows.
    out_rows = 0

    # Open output CSV.
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        # Create a CSV writer.
        writer = csv.writer(f)

        # Write header.
        writer.writerow(["class", "n_instances_added", "n_instances_deleted"])

        # Write one row per class in sorted order.
        for cls in sorted(counts.keys()):
            writer.writerow([
                cls,
                counts[cls]["n_instances_added"],
                counts[cls]["n_instances_deleted"],
            ])
            out_rows += 1

    # Return row count.
    return out_rows


def main() -> int:
    """Program entry point."""
    # Parse command-line arguments.
    args = parse_args()

    # Validate input directories.
    if not os.path.isdir(args.table1_dir):
        print(f"table1 directory does not exist: {args.table1_dir}", file=sys.stderr)
        return 1

    if not os.path.isdir(args.table1b_dir):
        print(f"table1b directory does not exist: {args.table1b_dir}", file=sys.stderr)
        return 1

    # Create the nested count structure:
    #   counts[class]["n_instances_added"]
    #   counts[class]["n_instances_deleted"]
    counts = defaultdict(lambda: {
        "n_instances_added": 0,
        "n_instances_deleted": 0,
    })

    try:
        # Process all table1 files for class additions.
        table1_files, table1_rows = process_table1_files(args.table1_dir, counts)

        # Process all table1b files for class deletions.
        table1b_files, table1b_rows = process_table1b_files(args.table1b_dir, counts)

        # Write merged output.
        output_rows = write_output(args.output, counts)

        # Print final summary.
        print(
            f"[OK] "
            f"table1_files={table1_files} table1_rows={table1_rows} | "
            f"table1b_files={table1b_files} table1b_rows={table1b_rows} | "
            f"classes={output_rows} | "
            f"output={args.output}"
        )
        return 0

    except Exception as exc:
        # Report failure clearly.
        print(f"[ERROR] Failed to build class add/delete counts: {exc}", file=sys.stderr)
        return 1


# Run main() when called as a script.
if __name__ == "__main__":
    sys.exit(main())
