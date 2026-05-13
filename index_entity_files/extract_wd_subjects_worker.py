#!/usr/bin/env python3

import argparse
import bz2
import csv
import gzip
import lzma
import os
import re
import sys
from pathlib import Path


WD_SUBJECT_RE = re.compile(r"^wd:.+$")
OUTPUT_SUFFIX = "_wd_subjects.csv"


def raise_csv_field_limit():
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


def open_text(path):
    path = str(path)

    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="")
    if path.endswith(".bz2"):
        return bz2.open(path, "rt", encoding="utf-8", errors="replace", newline="")
    if path.endswith(".xz"):
        return lzma.open(path, "rt", encoding="utf-8", errors="replace", newline="")

    return open(path, "r", encoding="utf-8", errors="replace", newline="")


def output_name(input_path):
    name = Path(input_path).name

    if name.endswith(".csv"):
        stem = name[:-4]
    else:
        stem = Path(name).stem

    return stem + OUTPUT_SUFFIX


def main():
    parser = argparse.ArgumentParser(
        description="Extract distinct wd:* subjects from one Wikidata history CSV."
    )

    parser.add_argument("--input", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--force", action="store_true")

    args = parser.parse_args()

    raise_csv_field_limit()

    input_path = Path(args.input)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    out_path = out_dir / output_name(input_path)
    done_path = out_path.with_suffix(out_path.suffix + ".done")
    tmp_path = out_path.with_name(out_path.name + f".tmp.{os.getpid()}")

    if out_path.exists() and done_path.exists() and not args.force:
        print(f"SKIP already done: {input_path.name}")
        return

    subjects = set()
    rows = 0
    wd_subject_rows = 0

    with open_text(input_path) as f:
        reader = csv.reader(f)

        for row in reader:
            if not row:
                continue

            if rows == 0 and row[0] == "s":
                rows += 1
                continue

            rows += 1

            s = row[0].strip().replace("\r", "")

            if WD_SUBJECT_RE.match(s):
                subjects.add(s)
                wd_subject_rows += 1

    with open(tmp_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["subject"])

        for subject in sorted(subjects):
            writer.writerow([subject])

    os.replace(tmp_path, out_path)

    with open(done_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"input={input_path}\n")
        f.write(f"rows={rows}\n")
        f.write(f"wd_subject_rows={wd_subject_rows}\n")
        f.write(f"distinct_wd_subjects={len(subjects)}\n")

    print(
        f"DONE {input_path.name} "
        f"rows={rows} "
        f"wd_subject_rows={wd_subject_rows} "
        f"distinct_wd_subjects={len(subjects)} "
        f"output={out_path.name}"
    )


if __name__ == "__main__":
    main()
