#!/usr/bin/env python3

import argparse
import csv
import os
import sys
import time
from collections import defaultdict


def progress(message):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def pct(done, total):
    if total == 0:
        return "100.0%"
    return f"{(done / total) * 100:.1f}%"


def load_manifest(index_dir):
    manifest_path = os.path.join(index_dir, "manifest.csv")
    index_dir = os.path.abspath(index_dir)
    index_parent = os.path.dirname(index_dir)

    ranges = []

    with open(manifest_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)

        for row in reader:
            if len(row) < 4:
                continue

            start_subject = row[0].strip()
            end_subject = row[1].strip()
            raw_part_path = row[3].strip().replace("\r", "")

            if os.path.isabs(raw_part_path):
                part_path = raw_part_path
            else:
                candidates = [
                    os.path.join(index_dir, raw_part_path),
                    os.path.join(index_parent, raw_part_path),
                    raw_part_path,
                ]

                part_path = None
                for candidate in candidates:
                    if os.path.exists(candidate):
                        part_path = candidate
                        break

                if part_path is None:
                    # Keep the most likely path, but it will warn later if missing
                    part_path = os.path.join(index_parent, raw_part_path)

            ranges.append((start_subject, end_subject, part_path))

    return ranges

def find_manifest_part(subject, manifest_ranges):
    for start_subject, end_subject, part_path in manifest_ranges:
        if start_subject <= subject <= end_subject:
            return part_path
    return None


def lookup_subjects_from_index(subjects, index_dir, report_every=10000):
    progress(f"STEP 2/5 Loading subject lookup manifest from {index_dir}")

    manifest_ranges = load_manifest(index_dir)

    progress(f"Loaded manifest ranges: {len(manifest_ranges):,}")

    subjects = sorted(subjects)
    total_subjects = len(subjects)

    subjects_by_part = defaultdict(set)
    subject_to_file = {}

    progress("Assigning subjects to index parts")

    for i, subject in enumerate(subjects, start=1):
        part_path = find_manifest_part(subject, manifest_ranges)

        if part_path is None:
            subject_to_file[subject] = None
        else:
            subjects_by_part[part_path].add(subject)

        if i % report_every == 0 or i == total_subjects:
            progress(
                f"Manifest assignment: {i:,}/{total_subjects:,} "
                f"({pct(i, total_subjects)}); parts={len(subjects_by_part):,}"
            )

    progress(
        f"Scanning index parts: parts={len(subjects_by_part):,}; "
        f"subjects={total_subjects:,}"
    )

    found = 0

    for part_i, (part_path, wanted_subjects) in enumerate(subjects_by_part.items(), start=1):
        progress(
            f"Index part {part_i:,}/{len(subjects_by_part):,}: "
            f"{os.path.basename(part_path)}; wanted={len(wanted_subjects):,}"
        )

        remaining = set(wanted_subjects)

        if not os.path.exists(part_path):
            progress(f"WARN missing index part: {part_path}")
            for subject in remaining:
                subject_to_file[subject] = None
            continue

        with open(part_path, "r", encoding="utf-8", newline="") as f:
            for line in f:
                if not remaining:
                    break

                line = line.rstrip("\n\r")
                if not line:
                    continue

                parts = line.split("\t", 1)
                if len(parts) != 2:
                    continue

                subject = parts[0]
                history_file = parts[1]

                if subject in remaining:
                    subject_to_file[subject] = history_file
                    remaining.remove(subject)
                    found += 1

        for subject in remaining:
            subject_to_file[subject] = None

        progress(
            f"Finished index part {part_i:,}/{len(subjects_by_part):,}; "
            f"found_so_far={found:,}"
        )

    missing = sum(v is None for v in subject_to_file.values())

    progress(
        f"Finished index lookup: found={found:,}; missing={missing:,}; "
        f"total={len(subject_to_file):,}"
    )

    return subject_to_file


def load_targets(input_csv, index_dir, lookup_report_every):
    rows = []
    subjects = set()

    progress(f"STEP 1/5 Reading input CSV: {input_csv}")

    with open(input_csv, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        required = {"constraint_id", "s", "p", "o"}
        if not required.issubset(reader.fieldnames):
            raise ValueError(
                f"Missing required columns in {input_csv}: {reader.fieldnames}"
            )

        for i, row in enumerate(reader, start=1):
            row["_row_id"] = str(i - 1)
            rows.append(row)
            subjects.add(row["s"])

            if i % 1_000_000 == 0:
                progress(f"Read {i:,} rows; distinct_subjects={len(subjects):,}")

    progress(
        f"Finished reading input: rows={len(rows):,}; "
        f"distinct_subjects={len(subjects):,}"
    )

    subject_to_file = lookup_subjects_from_index(
        subjects=subjects,
        index_dir=index_dir,
        report_every=lookup_report_every,
    )

    progress("STEP 3/5 Grouping target triples by history file")

    targets_by_file = defaultdict(set)
    rows_without_file = 0

    for i, row in enumerate(rows, start=1):
        filename = subject_to_file.get(row["s"])
        row["_history_file"] = filename or ""

        if filename is None:
            rows_without_file += 1
        else:
            targets_by_file[filename].add((row["s"], row["p"], row["o"]))

        if i % 1_000_000 == 0 or i == len(rows):
            progress(
                f"Grouping progress: {i:,}/{len(rows):,} "
                f"({pct(i, len(rows))}); files={len(targets_by_file):,}; "
                f"rows_without_file={rows_without_file:,}"
            )

    subjects_without_file = sum(
        filename is None for filename in subject_to_file.values()
    )

    progress(
        f"Finished grouping: unique_history_files={len(targets_by_file):,}; "
        f"subjects_without_file={subjects_without_file:,}; "
        f"rows_without_file={rows_without_file:,}"
    )

    return rows, targets_by_file


def scan_history_files(targets_by_file, wdt_folder, scan_report_every_rows):
    progress("STEP 4/5 Scanning history files")

    matches = defaultdict(list)

    total_files = len(targets_by_file)
    total_targets = sum(len(x) for x in targets_by_file.values())
    matched_rows = 0
    matched_keys = set()

    progress(
        f"History scan setup: files={total_files:,}; "
        f"unique_target_triples={total_targets:,}"
    )

    for file_idx, (filename, targets) in enumerate(targets_by_file.items(), start=1):
        history_path = os.path.join(wdt_folder, filename)

        if not os.path.exists(history_path):
            progress(f"WARN missing history file: {history_path}")
            continue

        progress(
            f"Scanning file {file_idx:,}/{total_files:,} "
            f"({pct(file_idx, total_files)}): {filename}; "
            f"targets={len(targets):,}"
        )

        file_rows = 0
        file_matches = 0

        with open(history_path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)

            required = {"s", "p", "o", "cdate", "cuser", "ddate", "duser"}
            if not required.issubset(reader.fieldnames):
                raise ValueError(
                    f"Missing required columns in {history_path}: {reader.fieldnames}"
                )

            for hrow in reader:
                file_rows += 1

                key = (
                    hrow.get("s", "").strip(),
                    hrow.get("p", "").strip(),
                    hrow.get("o", "").strip(),
                )

                if key in targets:
                    matches[key].append({
                        "cdate": hrow.get("cdate", "").strip(),
                        "cuser": hrow.get("cuser", "").strip(),
                        "ddate": hrow.get("ddate", "").strip(),
                        "duser": hrow.get("duser", "").strip(),
                    })
                    matched_rows += 1
                    file_matches += 1
                    matched_keys.add(key)

                if file_rows % scan_report_every_rows == 0:
                    progress(
                        f"  scanning {filename}: rows={file_rows:,}; "
                        f"file_matches={file_matches:,}; "
                        f"total_matched_rows={matched_rows:,}"
                    )

        progress(
            f"Finished file {file_idx:,}/{total_files:,}: {filename}; "
            f"rows={file_rows:,}; file_matches={file_matches:,}; "
            f"matched_target_triples={len(matched_keys):,}/{total_targets:,}"
        )

    progress(
        f"Finished history scan: matched_rows={matched_rows:,}; "
        f"matched_target_triples={len(matched_keys):,}/{total_targets:,}"
    )

    return matches


def write_enriched(rows, matches, output_csv):
    progress(f"STEP 5/5 Writing enriched output: {output_csv}")

    os.makedirs(os.path.dirname(output_csv) or ".", exist_ok=True)

    output_fields = [
        "constraint_id",
        "s",
        "p",
        "o",
        "cdate",
        "cuser",
        "ddate",
        "duser",
        "history_file",
        "match_status",
    ]

    written = 0
    missing = 0

    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=output_fields)
        writer.writeheader()

        for i, row in enumerate(rows, start=1):
            key = (row["s"], row["p"], row["o"])
            found = matches.get(key, [])

            if not found:
                missing += 1
                writer.writerow({
                    "constraint_id": row["constraint_id"],
                    "s": row["s"],
                    "p": row["p"],
                    "o": row["o"],
                    "cdate": "",
                    "cuser": "",
                    "ddate": "",
                    "duser": "",
                    "history_file": row["_history_file"],
                    "match_status": "not_found",
                })
                written += 1
            else:
                for hit in found:
                    writer.writerow({
                        "constraint_id": row["constraint_id"],
                        "s": row["s"],
                        "p": row["p"],
                        "o": row["o"],
                        "cdate": hit["cdate"],
                        "cuser": hit["cuser"],
                        "ddate": hit["ddate"],
                        "duser": hit["duser"],
                        "history_file": row["_history_file"],
                        "match_status": "found",
                    })
                    written += 1

            if i % 1_000_000 == 0 or i == len(rows):
                progress(
                    f"Write progress: {i:,}/{len(rows):,} "
                    f"({pct(i, len(rows))}); written={written:,}; "
                    f"missing={missing:,}"
                )

    progress(f"Finished writing: written={written:,}; missing={missing:,}")
    return written, missing


def enrich_file(
    input_csv,
    output_csv,
    index_dir,
    wdt_folder,
    lookup_report_every,
    scan_report_every_rows,
):
    start = time.time()

    progress(f"START enrichment for {input_csv}")

    rows, targets_by_file = load_targets(
        input_csv=input_csv,
        index_dir=index_dir,
        lookup_report_every=lookup_report_every,
    )

    matches = scan_history_files(
        targets_by_file=targets_by_file,
        wdt_folder=wdt_folder,
        scan_report_every_rows=scan_report_every_rows,
    )

    written, missing = write_enriched(
        rows=rows,
        matches=matches,
        output_csv=output_csv,
    )

    elapsed = time.time() - start

    progress(f"DONE {input_csv}")
    progress(f"written_rows={written:,}")
    progress(f"missing_input_rows={missing:,}")
    progress(f"elapsed_minutes={elapsed / 60:.2f}")


def main():
    parser = argparse.ArgumentParser(
        description="Enrich Tanon correction CSVs with historical cdate/cuser/ddate/duser."
    )

    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)

    parser.add_argument(
        "--index-dir",
        default="/bigdata-nfs/wd_history_work/index_qid_files/wd_subject_lookup_100",
        help="Directory containing manifest.csv and subject lookup part files.",
    )

    parser.add_argument(
        "--wdt-folder",
        default="/bigdata-nfs/wd_history_work/h3_constraint_editors/wdt_folder",
        help="Directory containing extracted wdt history CSV files.",
    )

    parser.add_argument(
        "--lookup-report-every",
        type=int,
        default=10000,
        help="Report index lookup progress every N subjects.",
    )

    parser.add_argument(
        "--scan-report-every-rows",
        type=int,
        default=5000000,
        help="Report scan progress every N rows inside each history file.",
    )

    args = parser.parse_args()

    enrich_file(
        input_csv=args.input,
        output_csv=args.output,
        index_dir=args.index_dir,
        wdt_folder=args.wdt_folder,
        lookup_report_every=args.lookup_report_every,
        scan_report_every_rows=args.scan_report_every_rows,
    )


if __name__ == "__main__":
    main()
