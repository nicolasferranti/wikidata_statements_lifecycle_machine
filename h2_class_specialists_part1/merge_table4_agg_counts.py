#!/usr/bin/env python3
# Import argparse to parse command-line arguments.
import argparse

# Import glob to find all matching CSV files.
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
    """Normalize a header field."""
    return (name or "").strip().lower()


def safe_get(row: dict, key: str) -> str:
    """Safely read a field from a CSV row."""
    return (row.get(key, "") or "").strip()


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Merge multiple table4 aggregate CSV files into one summed CSV."
    )

    parser.add_argument(
        "--input-dir",
        required=True,
        help="Folder containing per-file table4 aggregate CSV files.",
    )

    parser.add_argument(
        "--output-dir",
        required=True,
        help="Folder where the merged CSV file will be written.",
    )

    return parser.parse_args()


def main() -> int:
    """Merge all per-file table4 aggregate CSVs into one summed CSV."""
    # Parse command-line arguments.
    args = parse_args()

    # Read input and output directories from CLI.
    input_dir = args.input_dir
    output_dir = args.output_dir

    # Validate the input directory.
    if not os.path.isdir(input_dir):
        print(f"Input directory does not exist: {input_dir}", file=sys.stderr)
        return 1

    # Ensure the output directory exists.
    os.makedirs(output_dir, exist_ok=True)

    # Define the output file path.
    output_file = os.path.join(output_dir, "table4_agg_counts_merged.csv")

    # Find all CSV files in the input directory, excluding the output file name if present.
    files = sorted(
        f for f in glob.glob(os.path.join(input_dir, "*.csv"))
        if os.path.abspath(f) != os.path.abspath(output_file)
    )

    # Fail clearly if no files were found.
    if not files:
        print(f"No CSV files found in {input_dir}", file=sys.stderr)
        return 1

    # Aggregation map:
    #   totals[(user, class)] = summed edit_count
    totals = defaultdict(int)

    # Counters for reporting.
    file_count = 0
    row_count = 0

    # Process each input file.
    for i, path in enumerate(files, start=1):
        # Count this file.
        file_count += 1

        # Extract basename for cleaner progress messages.
        name = os.path.basename(path)

        # Print progress.
        print(f"[{i}/{len(files)}] reading {name} ...", flush=True)

        # Open the CSV file.
        with open(path, "r", encoding="utf-8", newline="") as f:
            # Create a DictReader.
            reader = csv.DictReader(f)

            # Validate header existence.
            if reader.fieldnames is None:
                raise ValueError(f"No header found in file: {path}")

            # Normalize and validate header names.
            normalized_fieldnames = [normalize_header(x) for x in reader.fieldnames]
            required = {"user", "class", "edit_count"}

            if not required.issubset(set(normalized_fieldnames)):
                raise ValueError(
                    f"Missing required columns in {path}. "
                    f"Found={normalized_fieldnames}, required={sorted(required)}"
                )

            # Map original header names to normalized names.
            original_to_normalized = dict(zip(reader.fieldnames, normalized_fieldnames))

            # Read all rows from this file.
            for raw_row in reader:
                # Count the input row.
                row_count += 1

                # Normalize row keys.
                row = {
                    original_to_normalized[k]: (v if v is not None else "")
                    for k, v in raw_row.items()
                }

                # Read needed fields.
                user = safe_get(row, "user")
                cls = safe_get(row, "class")
                edit_count_str = safe_get(row, "edit_count")

                # Skip incomplete rows.
                if not user or not cls or not edit_count_str:
                    continue

                # Add this row's count into the aggregate total.
                totals[(user, cls)] += int(edit_count_str)

    # Write merged output.
    with open(output_file, "w", encoding="utf-8", newline="") as f:
        # Create a CSV writer.
        writer = csv.writer(f)

        # Write header.
        writer.writerow(["user", "class", "edit_count"])

        # Write aggregated rows in stable sorted order.
        for user, cls in sorted(totals.keys()):
            writer.writerow([user, cls, totals[(user, cls)]])

    # Print final summary.
    print(
        f"[OK] files={file_count} input_rows={row_count} "
        f"merged_rows={len(totals)} output={output_file}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
