#!/usr/bin/env python3
"""
Aggregate per-file class edit count JSON files.

Input files look like:
[
  {
    "class": "wd:QID",
    "edit_counts": [
      {"user": "X", "cdate": "Y", "ddate": "Z", "count": J}
    ]
  }
]

Default behavior:
  - group records by class
  - concatenate all edit_counts arrays for the same class

Optional:
  --mode merge-identical
    sums counts for identical (class, user, cdate, ddate) rows.
"""

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path
from tempfile import NamedTemporaryFile


def iter_input_files(input_dir: Path, pattern: str):
    yield from sorted(input_dir.glob(pattern))


def load_json_file(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def atomic_write_json(output, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)

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


def aggregate_concat(files, log_every: int):
    class_to_edit_counts = defaultdict(list)

    n_files = 0
    n_class_records = 0
    n_edit_entries = 0

    for path in files:
        n_files += 1
        data = load_json_file(path)

        if not isinstance(data, list):
            raise ValueError(f"Expected top-level JSON list in {path}")

        for class_record in data:
            cls = class_record.get("class")
            edit_counts = class_record.get("edit_counts", [])

            if not cls:
                continue
            if not isinstance(edit_counts, list):
                raise ValueError(f"'edit_counts' is not a list for class {cls} in {path}")

            class_to_edit_counts[cls].extend(edit_counts)
            n_class_records += 1
            n_edit_entries += len(edit_counts)

        if log_every > 0 and n_files % log_every == 0:
            print(
                f"processed_files={n_files} "
                f"distinct_classes_so_far={len(class_to_edit_counts)} "
                f"class_records={n_class_records} "
                f"edit_entries={n_edit_entries}",
                flush=True,
            )

    output = []
    for cls in sorted(class_to_edit_counts):
        output.append({
            "class": cls,
            "edit_counts": sorted(
                class_to_edit_counts[cls],
                key=lambda x: (
                    x.get("cdate", ""),
                    x.get("ddate", ""),
                    x.get("user", ""),
                    x.get("count", 0),
                ),
            ),
        })

    stats = {
        "files": n_files,
        "distinct_classes": len(class_to_edit_counts),
        "class_records": n_class_records,
        "edit_entries": n_edit_entries,
        "output_edit_entries": n_edit_entries,
    }

    return output, stats


def aggregate_merge_identical(files, log_every: int):
    # key: (class, user, cdate, ddate) -> count
    counts = defaultdict(int)

    n_files = 0
    n_class_records = 0
    n_edit_entries = 0

    for path in files:
        n_files += 1
        data = load_json_file(path)

        if not isinstance(data, list):
            raise ValueError(f"Expected top-level JSON list in {path}")

        for class_record in data:
            cls = class_record.get("class")
            edit_counts = class_record.get("edit_counts", [])

            if not cls:
                continue
            if not isinstance(edit_counts, list):
                raise ValueError(f"'edit_counts' is not a list for class {cls} in {path}")

            n_class_records += 1

            for item in edit_counts:
                user = item.get("user", "")
                cdate = item.get("cdate", "")
                ddate = item.get("ddate", "")
                count = int(item.get("count", 0))
                counts[(cls, user, cdate, ddate)] += count
                n_edit_entries += 1

        if log_every > 0 and n_files % log_every == 0:
            print(
                f"processed_files={n_files} "
                f"distinct_keys_so_far={len(counts)} "
                f"class_records={n_class_records} "
                f"input_edit_entries={n_edit_entries}",
                flush=True,
            )

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

    stats = {
        "files": n_files,
        "distinct_classes": len(class_to_edit_counts),
        "class_records": n_class_records,
        "input_edit_entries": n_edit_entries,
        "output_edit_entries": len(counts),
    }

    return output, stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aggregate class edit count JSON files by class."
    )
    parser.add_argument(
        "input_dir",
        type=Path,
        help="Folder containing *-class-edit-counts.json files",
    )
    parser.add_argument(
        "output_json",
        type=Path,
        help="Output aggregated JSON file",
    )
    parser.add_argument(
        "--pattern",
        default="*-class-edit-counts.json",
        help="Input filename glob pattern. Default: *-class-edit-counts.json",
    )
    parser.add_argument(
        "--mode",
        choices=["concat", "merge-identical"],
        default="concat",
        help=(
            "concat: append edit_counts for the same class. "
            "merge-identical: sum counts for identical class/user/cdate/ddate rows."
        ),
    )
    parser.add_argument(
        "--log-every",
        type=int,
        default=100,
        help="Print progress after every N files. Use 0 to disable. Default: 100",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing output file.",
    )

    args = parser.parse_args()

    if not args.input_dir.is_dir():
        raise NotADirectoryError(f"Input directory does not exist: {args.input_dir}")

    if args.output_json.exists() and not args.overwrite:
        raise FileExistsError(f"Output already exists: {args.output_json}. Use --overwrite.")

    files = list(iter_input_files(args.input_dir, args.pattern))
    if not files:
        raise FileNotFoundError(
            f"No files matched pattern {args.pattern!r} in {args.input_dir}"
        )

    print(f"input_dir={args.input_dir}", flush=True)
    print(f"pattern={args.pattern}", flush=True)
    print(f"files_found={len(files)}", flush=True)
    print(f"mode={args.mode}", flush=True)
    print(f"output_json={args.output_json}", flush=True)

    if args.mode == "concat":
        output, stats = aggregate_concat(files, args.log_every)
    else:
        output, stats = aggregate_merge_identical(files, args.log_every)

    atomic_write_json(output, args.output_json)

    print("DONE", flush=True)
    for key, value in stats.items():
        print(f"{key}={value}", flush=True)


if __name__ == "__main__":
    main()
