#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys
import tempfile
from collections import Counter
from datetime import datetime
from typing import Iterable

WDT_PROP_RE = re.compile(r"^wdt:(P[1-9][0-9]*)$")


def atomic_write_text(path: str, text: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_csv_counts(path: str, counts: Counter[tuple[str, str]]) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["user", "property", "count"])
            for (user, prop), cnt in sorted(counts.items()):
                w.writerow([user, prop, cnt])
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def append_error(
    error_log_path: str,
    *,
    file_path: str,
    row_number: int,
    error_type: str,
    error_message: str,
    row_data: dict | None,
) -> None:
    exists = os.path.exists(error_log_path)
    with open(error_log_path, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(
                [
                    "timestamp_utc",
                    "file",
                    "row_number",
                    "error_type",
                    "error_message",
                    "row_json",
                ]
            )
        w.writerow(
            [
                datetime.utcnow().isoformat(timespec="seconds") + "Z",
                file_path,
                row_number,
                error_type,
                error_message,
                json.dumps(row_data, ensure_ascii=False, sort_keys=True)
                if row_data is not None
                else "",
            ]
        )


def load_checkpoint(path: str) -> tuple[Counter[tuple[str, str]], set[str]]:
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)

    counts = Counter()
    for k, v in obj.get("counts", {}).items():
        user, prop = k.split("\t", 1)
        counts[(user, prop)] = int(v)

    completed_files = set(obj.get("completed_files", []))
    return counts, completed_files


def save_checkpoint(
    path: str,
    counts: Counter[tuple[str, str]],
    completed_files: set[str],
    current_file: str | None,
) -> None:
    payload = {
        "completed_files": sorted(completed_files),
        "current_file": current_file,
        "counts": {f"{u}\t{p}": c for (u, p), c in counts.items()},
    }
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, sort_keys=True))


def iter_input_files(input_dir: str, pattern: str) -> Iterable[str]:
    search = os.path.join(input_dir, pattern)
    yield from sorted(glob.glob(search))


def process_file(
    file_path: str,
    counts: Counter[tuple[str, str]],
    error_log_path: str,
    progress_log_handle,
    row_progress_every: int,
) -> tuple[int, int]:
    rows_seen = 0
    rows_counted = 0

    with open(file_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)

        required = {"s", "p", "o", "cdate", "cuser", "ddate", "duser"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"Missing required columns {sorted(missing)} in file {file_path}"
            )

        for row_number, row in enumerate(reader, start=2):
            rows_seen += 1
            try:
                pred = row["p"]
                cuser = row["cuser"]

                if not cuser:
                    continue

                m = WDT_PROP_RE.match(pred)
                if not m:
                    continue

                prop = m.group(1)
                counts[(cuser, prop)] += 1
                rows_counted += 1

            except Exception as e:
                append_error(
                    error_log_path,
                    file_path=file_path,
                    row_number=row_number,
                    error_type=type(e).__name__,
                    error_message=str(e),
                    row_data=row,
                )

            if row_progress_every > 0 and rows_seen % row_progress_every == 0:
                progress_log_handle.write(
                    f"[ROWPROGRESS] {file_path} rows_seen={rows_seen} rows_counted={rows_counted} unique_user_property={len(counts)}\n"
                )
                progress_log_handle.flush()

    return rows_seen, rows_counted


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Count (user, property) creation events for wdt:P* predicates across a folder of CSV files."
    )
    ap.add_argument(
        "--input-dir",
        required=True,
        help="Folder containing input CSV files.",
    )
    ap.add_argument(
        "--pattern",
        default="*.csv",
        help='Glob pattern for input files inside input-dir. Default: "*.csv"',
    )
    ap.add_argument(
        "--output-counts",
        required=True,
        help="Final CSV output path for counts: user,property,count",
    )
    ap.add_argument(
        "--checkpoint",
        required=True,
        help="Checkpoint JSON path.",
    )
    ap.add_argument(
        "--checkpoint-counts-csv",
        default="",
        help="Optional CSV mirror of current counts when checkpoint is written.",
    )
    ap.add_argument(
        "--checkpoint-every",
        type=int,
        default=20,
        help="Write checkpoint every N successfully processed files. Default: 20",
    )
    ap.add_argument(
        "--error-log",
        required=True,
        help="CSV file for row-level errors.",
    )
    ap.add_argument(
        "--progress-log",
        required=True,
        help="Text log file for per-file progress and summaries.",
    )
    ap.add_argument(
        "--row-progress-every",
        type=int,
        default=1000000,
        help="Write an in-file progress line every N rows. Use 0 to disable. Default: 1000000",
    )
    ap.add_argument(
        "--resume",
        action="store_true",
        help="Resume from existing checkpoint if present.",
    )
    ap.add_argument(
        "--first-only",
        action="store_true",
        help="Process only the first input file (for testing).",
    )
    args = ap.parse_args()

    if args.checkpoint_every < 1:
        print("--checkpoint-every must be >= 1", file=sys.stderr)
        return 1

    if args.row_progress_every < 0:
        print("--row-progress-every must be >= 0", file=sys.stderr)
        return 1

    input_files = list(iter_input_files(args.input_dir, args.pattern))
    if args.first_only:
        input_files = input_files[:1]

    if not input_files:
        print(
            f"No input files matched in {args.input_dir!r} with pattern {args.pattern!r}",
            file=sys.stderr,
        )
        return 1

    counts: Counter[tuple[str, str]] = Counter()
    completed_files: set[str] = set()

    if args.resume and os.path.exists(args.checkpoint):
        counts, completed_files = load_checkpoint(args.checkpoint)

    processed_since_checkpoint = 0

    with open(args.progress_log, "a", encoding="utf-8") as plog:
        plog.write(
            f"[{datetime.utcnow().isoformat(timespec='seconds')}Z] START files={len(input_files)} completed={len(completed_files)} checkpoint_every={args.checkpoint_every} row_progress_every={args.row_progress_every}\n"
        )
        plog.flush()

        for idx, file_path in enumerate(input_files, start=1):
            if file_path in completed_files:
                plog.write(f"[SKIP] {idx}/{len(input_files)} {file_path}\n")
                plog.flush()
                continue

            plog.write(f"[START] {idx}/{len(input_files)} {file_path}\n")
            plog.flush()

            try:
                rows_seen, rows_counted = process_file(
                    file_path=file_path,
                    counts=counts,
                    error_log_path=args.error_log,
                    progress_log_handle=plog,
                    row_progress_every=args.row_progress_every,
                )

                completed_files.add(file_path)
                processed_since_checkpoint += 1

                if processed_since_checkpoint >= args.checkpoint_every:
                    save_checkpoint(
                        path=args.checkpoint,
                        counts=counts,
                        completed_files=completed_files,
                        current_file=file_path,
                    )

                    if args.checkpoint_counts_csv:
                        atomic_write_csv_counts(args.checkpoint_counts_csv, counts)

                    plog.write(
                        f"[CHECKPOINT] file={file_path} completed_files={len(completed_files)} unique_user_property={len(counts)}\n"
                    )
                    plog.flush()
                    processed_since_checkpoint = 0

                plog.write(
                    f"[DONE ] {idx}/{len(input_files)} {file_path} rows_seen={rows_seen} rows_counted={rows_counted} unique_user_property={len(counts)}\n"
                )
                plog.flush()

            except Exception as e:
                append_error(
                    args.error_log,
                    file_path=file_path,
                    row_number=0,
                    error_type=type(e).__name__,
                    error_message=f"FILE_LEVEL_ERROR: {e}",
                    row_data=None,
                )
                plog.write(
                    f"[ERROR] {idx}/{len(input_files)} {file_path} error={type(e).__name__}: {e}\n"
                )
                plog.flush()

        if processed_since_checkpoint > 0:
            save_checkpoint(
                path=args.checkpoint,
                counts=counts,
                completed_files=completed_files,
                current_file=None,
            )

            if args.checkpoint_counts_csv:
                atomic_write_csv_counts(args.checkpoint_counts_csv, counts)

            plog.write(
                f"[CHECKPOINT] final_flush completed_files={len(completed_files)} unique_user_property={len(counts)}\n"
            )
            plog.flush()

        atomic_write_csv_counts(args.output_counts, counts)
        plog.write(
            f"[{datetime.utcnow().isoformat(timespec='seconds')}Z] FINISH completed={len(completed_files)} unique_user_property={len(counts)}\n"
        )
        plog.flush()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
