#!/usr/bin/env python3
"""
Filter one Wikidata WDT triple-history CSV into two CSVs:
  - rows where p == wdt:P279
  - rows where p == wdt:P31

Input columns are expected to include:
s,p,o,cdate,cuser,ddate,duser
"""

import argparse
import csv
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def output_path(input_csv: Path, out_dir: Path, pid: str) -> Path:
    stem = input_csv.name
    if stem.endswith(".csv"):
        stem = stem[:-4]
    return out_dir / f"{stem}-{pid}.csv"


def atomic_write_csv(rows_iter, header, destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        dir=str(destination.parent),
        delete=False,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    ) as tmp:
        tmp_path = Path(tmp.name)
        writer = csv.writer(tmp)
        writer.writerow(header)

        for row in rows_iter:
            writer.writerow(row)
            count += 1

    os.replace(tmp_path, destination)
    return count


def filter_file(input_csv: Path, p279_dir: Path, p31_dir: Path) -> tuple[int, int]:
    p279_out = output_path(input_csv, p279_dir, "P279")
    p31_out = output_path(input_csv, p31_dir, "P31")

    with input_csv.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            raise ValueError(f"Empty CSV file: {input_csv}")

        try:
            p_idx = header.index("p")
        except ValueError:
            raise ValueError(f"CSV file has no 'p' column: {input_csv}")

        p279_rows = []
        p31_rows = []

        # Stream input once. Store only matching rows; if matches are huge and memory is a concern,
        # switch to the commented streaming variant below.
        for row in reader:
            if len(row) <= p_idx:
                continue
            if row[p_idx] == "wdt:P279":
                p279_rows.append(row)
            elif row[p_idx] == "wdt:P31":
                p31_rows.append(row)

    n279 = atomic_write_csv(iter(p279_rows), header, p279_out)
    n31 = atomic_write_csv(iter(p31_rows), header, p31_out)
    return n279, n31


def filter_file_streaming(input_csv: Path, p279_dir: Path, p31_dir: Path) -> tuple[int, int]:
    """
    Fully streaming version: reads input once and writes both outputs at the same time.
    This is better for very large files.
    """
    p279_out = output_path(input_csv, p279_dir, "P279")
    p31_out = output_path(input_csv, p31_dir, "P31")

    p279_out.parent.mkdir(parents=True, exist_ok=True)
    p31_out.parent.mkdir(parents=True, exist_ok=True)

    with input_csv.open("r", encoding="utf-8", newline="") as fin:
        reader = csv.reader(fin)
        try:
            header = next(reader)
        except StopIteration:
            raise ValueError(f"Empty CSV file: {input_csv}")

        try:
            p_idx = header.index("p")
        except ValueError:
            raise ValueError(f"CSV file has no 'p' column: {input_csv}")

        with NamedTemporaryFile(
            "w", encoding="utf-8", newline="", dir=str(p279_out.parent),
            delete=False, prefix=f".{p279_out.name}.", suffix=".tmp"
        ) as f279, NamedTemporaryFile(
            "w", encoding="utf-8", newline="", dir=str(p31_out.parent),
            delete=False, prefix=f".{p31_out.name}.", suffix=".tmp"
        ) as f31:
            p279_tmp = Path(f279.name)
            p31_tmp = Path(f31.name)

            w279 = csv.writer(f279)
            w31 = csv.writer(f31)
            w279.writerow(header)
            w31.writerow(header)

            n279 = 0
            n31 = 0

            for row in reader:
                if len(row) <= p_idx:
                    continue
                if row[p_idx] == "wdt:P279":
                    w279.writerow(row)
                    n279 += 1
                elif row[p_idx] == "wdt:P31":
                    w31.writerow(row)
                    n31 += 1

    os.replace(p279_tmp, p279_out)
    os.replace(p31_tmp, p31_out)
    return n279, n31


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Split one Wikidata WDT triple-history CSV into P279 and P31 CSV files."
    )
    parser.add_argument("input_csv", type=Path, help="Input CSV file path")
    parser.add_argument("p279_output_dir", type=Path, help="Output folder for p=wdt:P279 rows")
    parser.add_argument("p31_output_dir", type=Path, help="Output folder for p=wdt:P31 rows")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing output files. Without this, existing outputs are skipped.",
    )

    args = parser.parse_args()

    input_csv = args.input_csv
    p279_out = output_path(input_csv, args.p279_output_dir, "P279")
    p31_out = output_path(input_csv, args.p31_output_dir, "P31")

    if not input_csv.is_file():
        raise FileNotFoundError(f"Input CSV does not exist: {input_csv}")

    if not args.overwrite and p279_out.exists() and p31_out.exists():
        print(f"SKIP {input_csv} outputs already exist")
        return

    n279, n31 = filter_file_streaming(input_csv, args.p279_output_dir, args.p31_output_dir)
    print(f"DONE {input_csv} P279={n279} P31={n31}")


if __name__ == "__main__":
    main()
