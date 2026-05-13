#!/usr/bin/env python3
"""
Stream one large CSV file and build three per-file output subtables:

Table 1:
  all (s, o, cuser) where p = wdt:P31 and there is no ddate or duser

Table 1b:
  all (s, o, duser, ddate) where p = wdt:P31 and there is a ddate

Table 2:
  all (cuser, s, p, o, cdate) where p starts with "wdt:"

Design goals:
- stream input row-by-row (do not load the whole file in memory)
- write outputs safely using temp files + atomic rename
- make restart behavior reliable
- preserve one subtable per input file

Expected input columns:
  s, p, o, cdate, cuser, ddate, duser
"""

import argparse
import csv
import os
import sys
import tempfile


def normalize_header(name: str) -> str:
    """Normalize column names to lowercase without surrounding spaces."""
    return (name or "").strip().lower()


def is_nonempty(value: str) -> bool:
    """Return True if the value is not empty after stripping whitespace."""
    return value is not None and value.strip() != ""


def safe_get(row: dict, key: str) -> str:
    """Safely read a field from a row and strip whitespace."""
    return (row.get(key, "") or "").strip()


def build_output_paths(output_root: str, input_path: str):
    """Build output file paths for the three result tables."""
    basename = os.path.basename(input_path)
    stem = basename[:-4] if basename.lower().endswith(".csv") else basename

    table1_path = os.path.join(output_root, "table1", f"{stem}.table1.csv")
    table1b_path = os.path.join(output_root, "table1b", f"{stem}.table1b.csv")
    table2_path = os.path.join(output_root, "table2", f"{stem}.table2.csv")

    return stem, table1_path, table1b_path, table2_path


def open_temp_csv(final_path: str, header):
    """Open a temporary CSV file in the destination directory and write its header."""
    os.makedirs(os.path.dirname(final_path), exist_ok=True)

    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        newline="",
        encoding="utf-8",
        dir=os.path.dirname(final_path),
        prefix=".tmp.",
        suffix=".csv",
        delete=False,
    )

    writer = csv.writer(tmp)
    writer.writerow(header)

    return tmp, writer


def atomic_replace(src_tmp_path: str, final_path: str):
    """Atomically replace the final file with the temporary file."""
    os.replace(src_tmp_path, final_path)


def process_file(input_path: str, output_root: str) -> int:
    """Stream one input CSV and produce table1, table1b, and table2."""
    stem, table1_path, table1b_path, table2_path = build_output_paths(output_root, input_path)

    # Table 1 includes cuser.
    t1_file, t1_writer = open_temp_csv(table1_path, ["item", "class", "cuser"])

    # Table 1b keeps deleted P31 assignments.
    t1b_file, t1b_writer = open_temp_csv(table1b_path, ["item", "class", "duser", "ddate"])

    # Table 2 now also includes object.
    t2_file, t2_writer = open_temp_csv(table2_path, ["user", "item", "property", "object", "cdate"])

    input_rows = 0
    table1_rows = 0
    table1b_rows = 0
    table2_rows = 0

    try:
        with open(input_path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)

            if reader.fieldnames is None:
                raise ValueError(f"Input file has no header row: {input_path}")

            normalized_fieldnames = [normalize_header(x) for x in reader.fieldnames]
            original_to_normalized = dict(zip(reader.fieldnames, normalized_fieldnames))

            required = {"s", "p", "o", "cdate", "cuser", "ddate", "duser"}

            if not required.issubset(set(normalized_fieldnames)):
                raise ValueError(
                    f"Missing required columns in {input_path}. "
                    f"Found={normalized_fieldnames}, required={sorted(required)}"
                )

            for raw_row in reader:
                input_rows += 1

                row = {
                    original_to_normalized[k]: (v if v is not None else "")
                    for k, v in raw_row.items()
                }

                s = safe_get(row, "s")
                p = safe_get(row, "p")
                o = safe_get(row, "o")
                cdate = safe_get(row, "cdate")
                cuser = safe_get(row, "cuser")
                ddate = safe_get(row, "ddate")
                duser = safe_get(row, "duser")

                # Table 2: all direct-property edits where p starts with "wdt:"
                if p.startswith("wdt:"):
                    t2_writer.writerow([cuser, s, p, o, cdate])
                    table2_rows += 1

                # Table 1 and Table 1b: only P31 rows
                if p.lower() == "wdt:p31":
                    # Table 1: active class assertions, including cuser
                    if not is_nonempty(ddate) and not is_nonempty(duser):
                        t1_writer.writerow([s, o, cuser])
                        table1_rows += 1
                    # Table 1b: deleted class assertions
                    elif is_nonempty(ddate):
                        t1b_writer.writerow([s, o, duser, ddate])
                        table1b_rows += 1

        t1_file.flush()
        t1b_file.flush()
        t2_file.flush()

        t1_file.close()
        t1b_file.close()
        t2_file.close()

        atomic_replace(t1_file.name, table1_path)
        atomic_replace(t1b_file.name, table1b_path)
        atomic_replace(t2_file.name, table2_path)

        print(
            f"[OK] {stem} | input_rows={input_rows} | "
            f"table1={table1_rows} | table1b={table1b_rows} | table2={table2_rows}"
        )
        return 0

    except Exception as exc:
        try:
            t1_file.close()
        except Exception:
            pass
        try:
            t1b_file.close()
        except Exception:
            pass
        try:
            t2_file.close()
        except Exception:
            pass

        for tmp_path in [
            getattr(t1_file, "name", None),
            getattr(t1b_file, "name", None),
            getattr(t2_file, "name", None),
        ]:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

        print(f"[ERROR] Failed to process {input_path}: {exc}", file=sys.stderr)
        return 1


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Stream one CSV file and build Table 1, Table 1b, and Table 2."
    )

    parser.add_argument(
        "--input",
        required=True,
        help="Path to one input CSV file."
    )

    parser.add_argument(
        "--output-root",
        required=True,
        help="Root directory under which table1/, table1b/, and table2/ will be created."
    )

    return parser.parse_args()


def main():
    """Program entry point."""
    args = parse_args()

    if not os.path.isfile(args.input):
        print(f"[ERROR] Input file does not exist: {args.input}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.output_root, exist_ok=True)

    rc = process_file(args.input, args.output_root)
    sys.exit(rc)


if __name__ == "__main__":
    main()
