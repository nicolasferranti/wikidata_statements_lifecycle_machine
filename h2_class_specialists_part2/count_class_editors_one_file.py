#!/usr/bin/env python3
import argparse
import csv
import json
import os
from collections import defaultdict
from pathlib import Path
from tempfile import NamedTemporaryFile

EXCLUDED_PROPERTIES = {"wdt:P31", "wdt:P279"}


def output_path_for(p31_csv: Path, output_dir: Path) -> Path:
    name = p31_csv.name
    if name.endswith("-P31.csv"):
        stem = name[:-len("-P31.csv")]
    elif name.endswith(".csv"):
        stem = name[:-len(".csv")]
    else:
        stem = name
    return output_dir / f"{stem}-class-edit-counts.json"


def require_columns(header, required, file_path: Path) -> dict:
    index = {name: i for i, name in enumerate(header)}
    missing = [col for col in required if col not in index]
    if missing:
        raise ValueError(f"{file_path} is missing required columns: {missing}. Header: {header}")
    return index


def cell(row, idx: int) -> str:
    return row[idx] if idx < len(row) else ""


def is_time_in_interval(t: str, start: str, end: str) -> bool:
    # Wikidata timestamps here are ISO UTC strings, so lexical comparison is valid.
    # Interval semantics: [start, end). Empty end means open-ended.
    if not t or not start:
        return False
    return start <= t and (not end or t < end)


def load_p31_intervals(p31_csv: Path) -> dict:
    # subject -> list of (class, class_cdate, class_ddate)
    subject_to_classes = defaultdict(list)

    with p31_csv.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            raise ValueError(f"Empty P31 CSV: {p31_csv}")

        idx = require_columns(header, ["s", "p", "o", "cdate", "ddate"], p31_csv)

        for row in reader:
            if cell(row, idx["p"]) != "wdt:P31":
                continue

            s = cell(row, idx["s"])
            cls = cell(row, idx["o"])
            cdate = cell(row, idx["cdate"])
            ddate = cell(row, idx["ddate"])

            if not s or not cls or not cdate:
                continue

            subject_to_classes[s].append((cls, cdate, ddate))

    for s in subject_to_classes:
        subject_to_classes[s].sort(key=lambda x: (x[1], x[2], x[0]))

    return dict(subject_to_classes)


def count_edits(p31_intervals: dict, wdt_csv: Path) -> dict:
    # key: (class, user, class_cdate, class_ddate) -> count
    counts = defaultdict(int)

    with wdt_csv.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            raise ValueError(f"Empty WDT CSV: {wdt_csv}")

        idx = require_columns(header, ["s", "p", "cdate", "cuser"], wdt_csv)

        for row in reader:
            p = cell(row, idx["p"])
            if p in EXCLUDED_PROPERTIES:
                continue

            s = cell(row, idx["s"])
            intervals = p31_intervals.get(s)
            if not intervals:
                continue

            edit_cdate = cell(row, idx["cdate"])
            user = cell(row, idx["cuser"])

            if not edit_cdate:
                continue

            for cls, class_cdate, class_ddate in intervals:
                if is_time_in_interval(edit_cdate, class_cdate, class_ddate):
                    counts[(cls, user, class_cdate, class_ddate)] += 1

    return dict(counts)


def write_grouped_json(counts: dict, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)

    class_to_edit_counts = defaultdict(list)

    for (cls, user, cdate, ddate), count in counts.items():
        class_to_edit_counts[cls].append({
            "user": user,
            "cdate": cdate,
            "ddate": ddate,
            "count": count,
        })

    output = []
    for cls in sorted(class_to_edit_counts):
        output.append({
            "class": cls,
            "edit_counts": sorted(
                class_to_edit_counts[cls],
                key=lambda x: (x["cdate"], x["ddate"], x["user"]),
            ),
        })

    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(destination.parent),
        delete=False,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    ) as tmp:
        tmp_path = Path(tmp.name)
        json.dump(output, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")

    os.replace(tmp_path, destination)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Count non-P31/non-P279 user edits by valid subject P31 class intervals."
    )
    parser.add_argument("p31_csv", type=Path, help="Filtered P31 CSV file")
    parser.add_argument("wdt_csv", type=Path, help="Corresponding full WDT CSV file")
    parser.add_argument("output_dir", type=Path, help="Folder where output JSON is written")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing output file. Without this, existing output is skipped.",
    )

    args = parser.parse_args()

    if not args.p31_csv.is_file():
        raise FileNotFoundError(f"P31 CSV does not exist: {args.p31_csv}")
    if not args.wdt_csv.is_file():
        raise FileNotFoundError(f"WDT CSV does not exist: {args.wdt_csv}")

    out_path = output_path_for(args.p31_csv, args.output_dir)
    if out_path.exists() and not args.overwrite:
        print(f"SKIP {args.p31_csv} output already exists: {out_path}")
        return

    intervals = load_p31_intervals(args.p31_csv)
    counts = count_edits(intervals, args.wdt_csv)
    write_grouped_json(counts, out_path)

    n_intervals = sum(len(v) for v in intervals.values())
    print(
        f"DONE {args.p31_csv} "
        f"subjects={len(intervals)} "
        f"class_intervals={n_intervals} "
        f"count_rows={len(counts)} "
        f"out={out_path}"
    )


if __name__ == "__main__":
    main()
