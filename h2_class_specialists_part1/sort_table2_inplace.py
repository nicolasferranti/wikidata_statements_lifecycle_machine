#!/usr/bin/env python3
# Import glob to find all CSV files in the folder.
import glob

# Import csv for correct CSV parsing and writing.
import csv

# Import os for path operations and atomic replacement.
import os

# Import sys for exit codes and error printing.
import sys

# Import tempfile to safely write a temporary file before overwriting.
import tempfile


# Define the folder containing the table2 CSV files.
TABLE2_DIR = "output/table2"


def sort_one_file_in_place(path: str) -> int:
    """Read one CSV, sort rows by item and cdate, and overwrite it atomically."""
    # Open the input CSV.
    with open(path, "r", encoding="utf-8", newline="") as f:
        # Create a CSV reader.
        reader = csv.DictReader(f)

        # Validate header existence.
        if reader.fieldnames is None:
            raise ValueError(f"No header found in file: {path}")

        # Save the header order from the input file.
        fieldnames = reader.fieldnames

        # Load all rows from this one file.
        rows = list(reader)

    # Validate that required columns exist.
    required = {"item", "cdate"}
    missing = required - set(fieldnames)
    if missing:
        raise ValueError(f"Missing required columns {sorted(missing)} in file: {path}")

    # Sort rows first by item, then by cdate ascending.
    rows.sort(key=lambda r: (r["item"], r["cdate"]))

    # Create a temporary file in the same directory for atomic overwrite.
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=os.path.dirname(path) or ".",
        prefix=".tmp_sort_",
        suffix=".csv",
        delete=False,
    ) as tmp:
        # Save the temporary file name.
        tmp_path = tmp.name

        # Create the CSV writer using the same header order as the original.
        writer = csv.DictWriter(tmp, fieldnames=fieldnames)

        # Write the header row.
        writer.writeheader()

        # Write all sorted rows.
        writer.writerows(rows)

    # Atomically replace the original file with the sorted temp file.
    os.replace(tmp_path, path)

    # Return the number of data rows processed.
    return len(rows)


def main() -> int:
    """Sort all CSV files in output/table2 in place."""
    # Find all CSV files in the table2 directory in sorted filename order.
    files = sorted(glob.glob(os.path.join(TABLE2_DIR, "*.csv")))

    # Fail clearly if no files were found.
    if not files:
        print(f"No CSV files found in {TABLE2_DIR}", file=sys.stderr)
        return 1

    # Store total file count.
    total = len(files)

    # Initialize counters.
    ok_count = 0
    fail_count = 0
    total_rows = 0

    # Print startup summary.
    print(f"Found {total} file(s) in {TABLE2_DIR}")

    # Process each file one by one.
    for i, path in enumerate(files, start=1):
        # Get just the basename for cleaner logs.
        name = os.path.basename(path)

        # Print progress before processing this file.
        print(f"[{i}/{total}] sorting {name} ...", flush=True)

        try:
            # Sort this file in place and get row count.
            row_count = sort_one_file_in_place(path)

            # Update counters.
            ok_count += 1
            total_rows += row_count

            # Print success message.
            print(f"[{i}/{total}] done   {name} | rows={row_count}", flush=True)

        except Exception as exc:
            # Update failure counter.
            fail_count += 1

            # Report the file-level error and continue.
            print(f"[{i}/{total}] ERROR  {name} | {exc}", file=sys.stderr, flush=True)

    # Print final summary.
    print(
        f"Finished. success={ok_count} failed={fail_count} "
        f"files={total} rows={total_rows}"
    )

    # Return nonzero only if at least one file failed.
    return 0 if fail_count == 0 else 2


# Run the program when called as a script.
if __name__ == "__main__":
    sys.exit(main())
