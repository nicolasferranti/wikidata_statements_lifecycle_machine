#!/usr/bin/env python3

import argparse
import csv
import os
import sys
from typing import Set


def collect_constraint_statement_ids(input_csv: str) -> Set[str]:
    statement_ids = set()

    with open(input_csv, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)

        required_columns = {"s", "p", "o", "cdate", "cuser", "ddate", "duser"}
        missing = required_columns - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"Missing required columns in {input_csv}: {sorted(missing)}"
            )

        for row in reader:
            if row["p"] == "p:P2302":
                statement_id = row["o"].strip()
                if statement_id:
                    statement_ids.add(statement_id)

    return statement_ids


def write_filtered_rows(input_csv: str, output_csv: str, statement_ids: Set[str]) -> int:
    written_rows = 0

    with open(input_csv, "r", encoding="utf-8", newline="") as fin, \
         open(output_csv, "w", encoding="utf-8", newline="") as fout:

        reader = csv.DictReader(fin)
        fieldnames = reader.fieldnames
        if fieldnames is None:
            raise ValueError(f"Could not read header from {input_csv}")

        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()

        for row in reader:
            if row["p"] == "p:P2302" or row["s"] in statement_ids:
                writer.writerow(row)
                written_rows += 1

    return written_rows


def build_default_output_filename(input_csv: str) -> str:
    input_name = os.path.basename(input_csv)
    base, ext = os.path.splitext(input_name)
    if not ext:
        ext = ".csv"
    return f"{base}.constraints_only{ext}"


def build_default_output_path(input_csv: str) -> str:
    input_dir = os.path.dirname(input_csv)
    return os.path.join(input_dir, build_default_output_filename(input_csv))


def resolve_output_path(input_csv: str, output_arg: str | None) -> str:
    if output_arg is None:
        return build_default_output_path(input_csv)

    # If output exists and is a directory -> put generated filename inside it
    if os.path.isdir(output_arg):
        return os.path.join(output_arg, build_default_output_filename(input_csv))

    # If output ends with a path separator, treat it as a directory path to create/use
    if output_arg.endswith(os.sep) or (os.altsep and output_arg.endswith(os.altsep)):
        os.makedirs(output_arg, exist_ok=True)
        return os.path.join(output_arg, build_default_output_filename(input_csv))

    # Otherwise treat as explicit file path
    parent_dir = os.path.dirname(output_arg)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    return output_arg


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Extract rows related to Wikidata property constraint creation. "
            "First collect `o` from rows where p='p:P2302', then write those rows "
            "and all rows whose s matches one of those collected statement IDs."
        )
    )
    parser.add_argument(
        "input_csv",
        help="Path to the input CSV file"
    )
    parser.add_argument(
        "-o",
        "--output",
        help=(
            "Output file path or output directory. "
            "If a directory is given, the default generated filename is used inside it."
        )
    )

    args = parser.parse_args()

    input_csv = args.input_csv

    if not os.path.isfile(input_csv):
        print(f"ERROR: Input file does not exist: {input_csv}", file=sys.stderr)
        return 1

    try:
        output_csv = resolve_output_path(input_csv, args.output)
        statement_ids = collect_constraint_statement_ids(input_csv)
        written_rows = write_filtered_rows(input_csv, output_csv, statement_ids)

        print(f"Input file:              {input_csv}")
        print(f"Output file:             {output_csv}")
        print(f"Collected statement IDs: {len(statement_ids)}")
        print(f"Written rows:            {written_rows}")

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
