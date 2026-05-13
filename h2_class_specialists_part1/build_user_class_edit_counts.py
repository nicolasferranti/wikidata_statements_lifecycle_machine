#!/usr/bin/env python3
"""
Stream one table2 CSV together with its matching table1b CSV and build:

    user,class,edit_count

Definitions used in this script:

- table2 columns:
    user,item,property,object,cdate

- table1b columns:
    item,class,duser,ddate

Temporal logic agreed for this script:

1. table1b provides deletion times for class assignments:
       deleted[item][class] = sorted list of ddate

2. table2 provides class creation/start times through rows where:
       property == "wdt:P31"
   In those rows:
       object = class
       cdate  = start time

3. When we see a P31 row in table2, we instantiate a class interval:
       [start=cdate, end=matching_ddate)
   where matching_ddate is the first unused deletion time for the same
   (item, class) such that ddate > cdate.

4. If there is no matching future deletion, the class interval is open-ended:
       [start=cdate, +infinity)

5. For every row in table2, including P31 rows themselves, we count +1 for each
   class interval active at that row's cdate:
       start <= cdate < end
   and if end is None:
       start <= cdate

6. If an item has no active class at a row's cdate, that row is not counted.

7. To avoid duplicate overlapping intervals for the same class, if a P31 row is
   encountered while that same class is already active for the item at cdate,
   the script does not create a new interval.
"""

# Import argparse to parse command-line arguments.
import argparse

# Import csv for CSV reading and writing.
import csv

# Import os for path handling.
import os

# Import sys for exit codes and stderr output.
import sys

# Import collections helpers for nested dictionaries.
from collections import defaultdict


def normalize_header(name: str) -> str:
    """
    Normalize a header field to lowercase and strip surrounding whitespace.
    """
    return (name or "").strip().lower()


def safe_get(row: dict, key: str) -> str:
    """
    Safely get a value from a row dictionary and strip surrounding whitespace.
    """
    return (row.get(key, "") or "").strip()


def is_active(start: str, end: str | None, t: str) -> bool:
    """
    Return True if the interval [start, end) is active at time t.

    Since all timestamps are ISO-8601 strings of the same general format,
    lexicographic comparison is sufficient for chronological comparison.
    """
    # If the interval has no end time, it is active from start onward.
    if end is None:
        return start <= t

    # Otherwise the interval is active on [start, end).
    return start <= t < end


def build_output_path(output_path: str | None, table2_path: str) -> str:
    """
    Build the output path.

    If --output was provided, use it directly.
    Otherwise create:
        output/table4_agg_counts/<base>.table4_agg_counts.csv
    based on the input table2 filename.
    """
    # If user supplied an explicit output path, use it.
    if output_path:
        return output_path

    # Get the table2 basename.
    basename = os.path.basename(table2_path)

    # Remove .csv suffix if present.
    stem = basename[:-4] if basename.lower().endswith(".csv") else basename

    # Replace trailing ".table2" with ".table4_agg_counts" if present.
    if stem.endswith(".table2"):
        stem = stem[:-7] + ".table4_agg_counts"
    else:
        stem = stem + ".table4_agg_counts"

    # Write under output/table4_agg_counts.
    out_dir = os.path.join("output", "table4_agg_counts")

    # Ensure the directory exists.
    os.makedirs(out_dir, exist_ok=True)

    # Return the final output path.
    return os.path.join(out_dir, f"{stem}.csv")

def read_table1b(table1b_path: str):
    """
    Read table1b and build a nested dictionary of deletion dates:

        deleted[item][class] = sorted list of ddate values

    We also validate that the expected columns exist.
    """
    # Create a nested structure:
    #   deleted[item][class] -> list of ddate strings
    deleted = defaultdict(lambda: defaultdict(list))

    # Count rows for progress/debug printing.
    row_count = 0

    # Open the table1b CSV.
    with open(table1b_path, "r", encoding="utf-8", newline="") as f:
        # Create a DictReader for row-wise access.
        reader = csv.DictReader(f)

        # Ensure a header exists.
        if reader.fieldnames is None:
            raise ValueError(f"Input file has no header row: {table1b_path}")

        # Normalize and validate headers.
        normalized_fieldnames = [normalize_header(x) for x in reader.fieldnames]
        required = {"item", "class", "duser", "ddate"}

        if not required.issubset(set(normalized_fieldnames)):
            raise ValueError(
                f"Missing required columns in {table1b_path}. "
                f"Found={normalized_fieldnames}, required={sorted(required)}"
            )

        # Map original fieldnames to normalized ones.
        original_to_normalized = dict(zip(reader.fieldnames, normalized_fieldnames))

        # Iterate through table1b rows.
        for raw_row in reader:
            # Count this row.
            row_count += 1

            # Normalize the row keys.
            row = {
                original_to_normalized[k]: (v if v is not None else "")
                for k, v in raw_row.items()
            }

            # Read the fields we need.
            item = safe_get(row, "item")
            cls = safe_get(row, "class")
            ddate = safe_get(row, "ddate")

            # Ignore incomplete rows with missing essential values.
            if not item or not cls or not ddate:
                continue

            # Store the deletion time under its matching (item, class).
            deleted[item][cls].append(ddate)

    # Sort deletion times for each (item, class) ascending so we can
    # later match the earliest unused future deletion.
    for item in deleted:
        for cls in deleted[item]:
            deleted[item][cls].sort()

    # Return the structure plus a row count for reporting.
    return deleted, row_count


def is_class_already_active(intervals: list[tuple[str, str, str | None]], cls: str, t: str) -> bool:
    """
    Return True if class cls is already active at time t among the known intervals.
    """
    # Check each known interval for this item.
    for known_cls, start, end in intervals:
        # If the class matches and the interval is active at time t, return True.
        if known_cls == cls and is_active(start, end, t):
            return True

    # Otherwise it is not already active.
    return False


def choose_matching_deletion(
    deletion_dates: list[str],
    next_index: int,
    start_time: str,
) -> tuple[str | None, int]:
    """
    Choose the first unused deletion date strictly after start_time.

    Inputs:
      - deletion_dates: sorted list of ddate strings for one (item, class)
      - next_index: pointer to the next candidate deletion in that list
      - start_time: cdate from the P31 add row

    Returns:
      - matched deletion date, or None if none exists
      - updated next_index pointer
    """
    # Start scanning from the current next unused deletion.
    i = next_index

    # Advance until we find a deletion strictly after the start time.
    while i < len(deletion_dates) and deletion_dates[i] <= start_time:
        i += 1

    # If we found a valid future deletion, consume it and return it.
    if i < len(deletion_dates):
        return deletion_dates[i], i + 1

    # Otherwise there is no matching future deletion.
    return None, i


def process_table2(table2_path: str, deleted: dict):
    """
    Read table2 row by row and produce user/class edit counts.

    The table2 file is assumed to be sorted by:
      - item
      - cdate ascending

    We process one item block at a time, so memory stays bounded.

    Returns:
      counts[user][class] = edit_count
      plus row counters for reporting
    """
    # Aggregate counts:
    #   counts[user][class] -> integer count
    counts = defaultdict(lambda: defaultdict(int))

    # Track row counters for reporting.
    table2_rows = 0
    counted_rows = 0
    p31_rows = 0
    skipped_no_class_rows = 0

    # Open the table2 CSV.
    with open(table2_path, "r", encoding="utf-8", newline="") as f:
        # Create a DictReader.
        reader = csv.DictReader(f)

        # Ensure header exists.
        if reader.fieldnames is None:
            raise ValueError(f"Input file has no header row: {table2_path}")

        # Normalize and validate headers.
        normalized_fieldnames = [normalize_header(x) for x in reader.fieldnames]
        required = {"user", "item", "property", "object", "cdate"}

        if not required.issubset(set(normalized_fieldnames)):
            raise ValueError(
                f"Missing required columns in {table2_path}. "
                f"Found={normalized_fieldnames}, required={sorted(required)}"
            )

        # Map original headers to normalized versions.
        original_to_normalized = dict(zip(reader.fieldnames, normalized_fieldnames))

        # State for current item only.
        current_item = None

        # For the current item, store class intervals as:
        #   (class, start, end)
        current_intervals: list[tuple[str, str, str | None]] = []

        # For the current item, store deletion pointer per class so each deletion
        # is matched at most once.
        current_delete_index: dict[str, int] = {}

        # Iterate through table2 rows in sorted order.
        for raw_row in reader:
            # Count input rows.
            table2_rows += 1

            # Normalize row keys.
            row = {
                original_to_normalized[k]: (v if v is not None else "")
                for k, v in raw_row.items()
            }

            # Read needed fields.
            user = safe_get(row, "user")
            item = safe_get(row, "item")
            prop = safe_get(row, "property")
            obj = safe_get(row, "object")
            cdate = safe_get(row, "cdate")

            # If the item changes, reset the per-item interval state.
            if item != current_item:
                current_item = item
                current_intervals = []
                current_delete_index = {}

            # ------------------------------------------------------------
            # Step A: if this row is a P31 row, instantiate a class interval
            # ------------------------------------------------------------
            if prop.lower() == "wdt:p31":
                # Count this as a P31 row for reporting.
                p31_rows += 1

                # In table2, the object of a P31 row is the class.
                cls = obj

                # Only try to instantiate if the row has a non-empty class and time.
                if cls and cdate:
                    # Protect against duplicate overlapping starts:
                    # if this class is already active at cdate for the item,
                    # do not create another interval.
                    if not is_class_already_active(current_intervals, cls, cdate):
                        # Get the sorted deletion list for this exact (item, class).
                        deletion_dates = deleted.get(item, {}).get(cls, [])

                        # Get the next unused deletion pointer for this class.
                        next_idx = current_delete_index.get(cls, 0)

                        # Choose the first unused deletion strictly after cdate.
                        end_time, updated_idx = choose_matching_deletion(
                            deletion_dates=deletion_dates,
                            next_index=next_idx,
                            start_time=cdate,
                        )

                        # Save the updated pointer so the deletion cannot be reused.
                        current_delete_index[cls] = updated_idx

                        # Add the interval to the current item's known class intervals.
                        current_intervals.append((cls, cdate, end_time))

            # ------------------------------------------------------------
            # Step B: count this row for all classes active at cdate
            # ------------------------------------------------------------
            # Find all active classes for this item at the current row time.
            active_classes = []

            # Check each known class interval for activity at cdate.
            for cls, start, end in current_intervals:
                if is_active(start, end, cdate):
                    active_classes.append(cls)

            # If no classes are active, do not count this row.
            if not active_classes:
                skipped_no_class_rows += 1
                continue

            # Otherwise count +1 for each active class.
            for cls in active_classes:
                counts[user][cls] += 1

            # Track how many table2 rows contributed at least one count.
            counted_rows += 1

    # Return counts and counters.
    return counts, table2_rows, counted_rows, p31_rows, skipped_no_class_rows


def write_output(output_path: str, counts: dict):
    """
    Write the aggregated user/class counts to CSV with header:

        user,class,edit_count

    For stable output, rows are sorted by:
      1. user
      2. class
    """
    # Ensure output directory exists if a parent directory was given.
    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    # Count rows written for reporting.
    out_rows = 0

    # Open output CSV.
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        # Create a CSV writer.
        writer = csv.writer(f)

        # Write header.
        writer.writerow(["user", "class", "edit_count"])

        # Write sorted rows for deterministic output.
        for user in sorted(counts.keys()):
            for cls in sorted(counts[user].keys()):
                writer.writerow([user, cls, counts[user][cls]])
                out_rows += 1

    # Return number of output rows.
    return out_rows


def parse_args():
    """
    Parse command-line arguments.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Build per-file user/class edit counts from one table2 file and one matching table1b file."
        )
    )

    parser.add_argument(
        "--table1b",
        required=True,
        help="Path to one input table1b CSV file.",
    )

    parser.add_argument(
        "--table2",
        required=True,
        help="Path to one input table2 CSV file.",
    )

    parser.add_argument(
        "--output",
        required=False,
        default=None,
        help=(
            "Path to output CSV. "
            "If omitted, a default '<stem>.user_class_edit_counts.csv' is created next to table2."
        ),
    )

    return parser.parse_args()


def main():
    """
    Program entry point.
    """
    # Parse CLI arguments.
    args = parse_args()

    # Validate table1b path.
    if not os.path.isfile(args.table1b):
        print(f"[ERROR] table1b file does not exist: {args.table1b}", file=sys.stderr)
        sys.exit(1)

    # Validate table2 path.
    if not os.path.isfile(args.table2):
        print(f"[ERROR] table2 file does not exist: {args.table2}", file=sys.stderr)
        sys.exit(1)

    # Derive output path.
    output_path = build_output_path(args.output, args.table2)

    try:
        # ------------------------------------------------------------
        # Step 1: read table1b and build deletion dictionary
        # ------------------------------------------------------------
        deleted, table1b_rows = read_table1b(args.table1b)

        # ------------------------------------------------------------
        # Step 2: read table2 and build user/class counts
        # ------------------------------------------------------------
        (
            counts,
            table2_rows,
            counted_rows,
            p31_rows,
            skipped_no_class_rows,
        ) = process_table2(args.table2, deleted)

        # ------------------------------------------------------------
        # Step 3: write output
        # ------------------------------------------------------------
        output_rows = write_output(output_path, counts)

        # Print success summary.
        print(
            f"[OK] table1b_rows={table1b_rows} | "
            f"table2_rows={table2_rows} | "
            f"p31_rows={p31_rows} | "
            f"counted_rows={counted_rows} | "
            f"skipped_no_class_rows={skipped_no_class_rows} | "
            f"output_rows={output_rows} | "
            f"output={output_path}"
        )

    except Exception as exc:
        # Report any failure clearly.
        print(f"[ERROR] Failed to build user/class edit counts: {exc}", file=sys.stderr)
        sys.exit(1)


# Run main() when executed as a script.
if __name__ == "__main__":
    main()
