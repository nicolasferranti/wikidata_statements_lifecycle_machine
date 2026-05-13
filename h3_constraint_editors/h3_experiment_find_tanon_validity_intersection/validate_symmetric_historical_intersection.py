#!/usr/bin/env python3

import argparse
import csv
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone


def progress(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def pct(done, total):
    if total == 0:
        return "100.0%"
    return f"{(done / total) * 100:.1f}%"


def parse_time(x):
    if x is None:
        return None
    x = str(x).strip()
    if x in {"", "NaT", "nan", "None", "null"}:
        return None
    if x.endswith("Z"):
        x = x[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(x)
    except ValueError:
        return None


def normalize_interval(start, end):
    start = parse_time(start)
    end = parse_time(end)
    if start is None:
        return None
    return (start, end)


def interval_intersection(a, b):
    a_start, a_end = a
    b_start, b_end = b

    if a_start is None or b_start is None:
        return None

    start = max(a_start, b_start)

    if a_end is None and b_end is None:
        end = None
    elif a_end is None:
        end = b_end
    elif b_end is None:
        end = a_end
    else:
        end = min(a_end, b_end)

    if end is not None and start >= end:
        return None

    return (start, end)


def merge_intervals(intervals):
    intervals = [x for x in intervals if x is not None and x[0] is not None]
    if not intervals:
        return []

    max_dt = datetime.max.replace(tzinfo=timezone.utc)

    intervals = sorted(
        intervals,
        key=lambda x: (x[0], x[1] or max_dt),
    )

    merged = [intervals[0]]

    for start, end in intervals[1:]:
        last_start, last_end = merged[-1]
        last_end_cmp = last_end or max_dt

        if start <= last_end_cmp:
            if last_end is None or end is None:
                merged[-1] = (last_start, None)
            else:
                merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))

    return merged


def subtract_one_interval(base, sub):
    inter = interval_intersection(base, sub)
    if inter is None:
        return [base]

    base_start, base_end = base
    inter_start, inter_end = inter

    pieces = []

    if base_start < inter_start:
        pieces.append((base_start, inter_start))

    if inter_end is not None:
        if base_end is None or inter_end < base_end:
            pieces.append((inter_end, base_end))

    return pieces


def subtract_intervals(base_intervals, subtracting_intervals):
    remaining = merge_intervals(base_intervals)

    for sub in merge_intervals(subtracting_intervals):
        next_remaining = []

        for base in remaining:
            next_remaining.extend(subtract_one_interval(base, sub))

        remaining = merge_intervals(next_remaining)

        if not remaining:
            break

    return remaining


def intervals_fully_covered(required_intervals, covering_intervals):
    uncovered = subtract_intervals(required_intervals, covering_intervals)
    return len(uncovered) == 0, uncovered


def wdt_to_wd_property(p):
    p = (p or "").strip()
    if not p.startswith("wdt:"):
        return None
    return "wd:" + p.replace("wdt:", "", 1)


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
                    part_path = os.path.join(index_parent, raw_part_path)

            ranges.append((start_subject, end_subject, part_path))

    return ranges


def find_manifest_part(subject, manifest_ranges):
    for start_subject, end_subject, part_path in manifest_ranges:
        if start_subject <= subject <= end_subject:
            return part_path
    return None


def lookup_subjects_from_index(subjects, index_dir, report_every=10000):
    progress(f"Loading subject lookup manifest from {index_dir}")

    manifest_ranges = load_manifest(index_dir)

    progress(f"Loaded manifest ranges: {len(manifest_ranges):,}")

    subjects = sorted(subjects)
    subject_to_file = {}
    subjects_by_part = defaultdict(set)

    for i, subject in enumerate(subjects, start=1):
        part_path = find_manifest_part(subject, manifest_ranges)

        if part_path is None:
            subject_to_file[subject] = None
        else:
            subjects_by_part[part_path].add(subject)

        if i % report_every == 0 or i == len(subjects):
            progress(
                f"Manifest assignment: {i:,}/{len(subjects):,} "
                f"({pct(i, len(subjects))}); parts={len(subjects_by_part):,}"
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

                subject, history_file = parts[0], parts[1]

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
        f"Finished lookup: found={found:,}; missing={missing:,}; "
        f"total={len(subject_to_file):,}"
    )

    return subject_to_file


def load_symmetric_constraints(path):
    progress(f"Loading symmetric constraints: {path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    constraints = {}

    for prop, prop_data in raw.items():
        active = []
        exceptions = []

        for row in prop_data.get("active_intervals", []):
            active.append({
                "start": parse_time(row.get("start_date")),
                "end": parse_time(row.get("end_date")),
                "constraint_id": row.get("constraint_id"),
            })

        for row in prop_data.get("pq:P2303", []):
            exceptions.append({
                "entity": row.get("entity"),
                "start": parse_time(row.get("start_date")),
                "end": parse_time(row.get("end_date")),
                "constraint_id": row.get("constraint_id"),
            })

        constraints[prop] = {
            "active": active,
            "exceptions": exceptions,
        }

    progress(f"Loaded properties with symmetric constraints: {len(constraints):,}")

    return constraints


def filter_entries_by_constraint_id(entries, constraint_id_filter):
    if constraint_id_filter is None:
        return entries

    return [
        e for e in entries
        if e.get("constraint_id") == constraint_id_filter
    ]


def get_required_intervals_for_row(row, constraints, constraint_id_filter=None):
    prop = wdt_to_wd_property(row.get("p"))

    if prop is None:
        return "non_wdt_property", []

    if prop not in constraints:
        return "no_constraint_for_property", []

    triple_interval = normalize_interval(row.get("cdate"), row.get("ddate"))

    if triple_interval is None:
        return "invalid_dates", []

    subject = row.get("s")

    active_entries = filter_entries_by_constraint_id(
        constraints[prop]["active"],
        constraint_id_filter,
    )

    exception_entries = filter_entries_by_constraint_id(
        constraints[prop]["exceptions"],
        constraint_id_filter,
    )

    if not active_entries:
        return "no_constraint_for_property_instance", []

    active_intersections = []

    for entry in active_entries:
        inter = interval_intersection(
            triple_interval,
            (entry["start"], entry["end"]),
        )

        if inter is not None:
            active_intersections.append(inter)

    active_intersections = merge_intervals(active_intersections)

    if not active_intersections:
        return "no_constraint_lifecycle_intersection", []

    exception_intervals = []

    for entry in exception_entries:
        if entry["entity"] != subject:
            continue

        for active_interval in active_intersections:
            inter = interval_intersection(
                active_interval,
                (entry["start"], entry["end"]),
            )

            if inter is not None:
                exception_intervals.append(inter)

    required_intervals = subtract_intervals(
        active_intersections,
        exception_intervals,
    )

    if not required_intervals:
        return "not_violation_exception", []

    return "needs_reverse_check", required_intervals


def read_prepare_rows(input_csv, constraints, index_dir, lookup_report_every):
    progress(f"Reading input: {input_csv}")

    rows = []
    reverse_subjects = set()

    property_counts = defaultdict(int)
    tanon_id_counts = defaultdict(int)

    with open(input_csv, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for i, row in enumerate(reader, start=1):
            row["_row_id"] = str(i - 1)

            row["_required_intervals_property_level"] = []
            row["_required_intervals_tanon_constraint_id_level"] = []

            if row.get("match_status") != "found":
                property_status = "no_historical_row"
                tanon_id_status = "no_historical_row"
            else:
                property_status, property_required = get_required_intervals_for_row(
                    row=row,
                    constraints=constraints,
                    constraint_id_filter=None,
                )

                tanon_id_status, tanon_required = get_required_intervals_for_row(
                    row=row,
                    constraints=constraints,
                    constraint_id_filter=row.get("constraint_id"),
                )

                row["_required_intervals_property_level"] = property_required
                row["_required_intervals_tanon_constraint_id_level"] = tanon_required

                if (
                    property_status == "needs_reverse_check"
                    or tanon_id_status == "needs_reverse_check"
                ):
                    reverse_subject = row.get("o", "").strip()

                    if reverse_subject:
                        reverse_subjects.add(reverse_subject)
                    else:
                        if property_status == "needs_reverse_check":
                            property_status = "missing_reverse_subject"
                        if tanon_id_status == "needs_reverse_check":
                            tanon_id_status = "missing_reverse_subject"

            row["_pre_status_property_level"] = property_status
            row["_pre_status_tanon_constraint_id_level"] = tanon_id_status

            property_counts[property_status] += 1
            tanon_id_counts[tanon_id_status] += 1

            rows.append(row)

            if i % 100000 == 0:
                progress(
                    f"Read {i:,} rows | "
                    f"property_level={dict(property_counts)} | "
                    f"tanon_id_level={dict(tanon_id_counts)}"
                )

    progress(
        f"Finished reading {len(rows):,} rows | "
        f"property_level={dict(property_counts)} | "
        f"tanon_id_level={dict(tanon_id_counts)}"
    )

    progress(f"Distinct reverse subjects to locate: {len(reverse_subjects):,}")

    reverse_subject_to_file = lookup_subjects_from_index(
        reverse_subjects,
        index_dir=index_dir,
        report_every=lookup_report_every,
    )

    reverse_targets_by_file = defaultdict(set)

    for row in rows:
        needs_reverse = (
            row["_pre_status_property_level"] == "needs_reverse_check"
            or row["_pre_status_tanon_constraint_id_level"] == "needs_reverse_check"
        )

        if not needs_reverse:
            continue

        reverse_subject = row.get("o", "").strip()
        reverse_file = reverse_subject_to_file.get(reverse_subject)

        row["_reverse_history_file"] = reverse_file or ""

        if reverse_file is None:
            if row["_pre_status_property_level"] == "needs_reverse_check":
                row["_pre_status_property_level"] = "missing_reverse_history_file"
            if row["_pre_status_tanon_constraint_id_level"] == "needs_reverse_check":
                row["_pre_status_tanon_constraint_id_level"] = "missing_reverse_history_file"
            continue

        reverse_key = (
            row.get("o", "").strip(),
            row.get("p", "").strip(),
            row.get("s", "").strip(),
        )

        row["_reverse_key"] = reverse_key
        reverse_targets_by_file[reverse_file].add(reverse_key)

    progress(f"Reverse history files to scan: {len(reverse_targets_by_file):,}")

    return rows, reverse_targets_by_file


def scan_reverse_triples(reverse_targets_by_file, wdt_folder):
    progress("Scanning history files for reverse triples")

    reverse_matches = defaultdict(list)
    total_files = len(reverse_targets_by_file)

    for file_idx, (history_file, targets) in enumerate(reverse_targets_by_file.items(), start=1):
        path = os.path.join(wdt_folder, history_file)

        if not os.path.exists(path):
            progress(f"WARN missing history file: {path}")
            continue

        progress(
            f"Scanning {file_idx:,}/{total_files:,} ({pct(file_idx, total_files)}): "
            f"{history_file}; reverse_targets={len(targets):,}"
        )

        file_rows = 0
        file_matches = 0

        with open(path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)

            for hrow in reader:
                file_rows += 1

                key = (
                    hrow.get("s", "").strip(),
                    hrow.get("p", "").strip(),
                    hrow.get("o", "").strip(),
                )

                if key in targets:
                    reverse_matches[key].append({
                        "cdate": hrow.get("cdate", "").strip(),
                        "cuser": hrow.get("cuser", "").strip(),
                        "ddate": hrow.get("ddate", "").strip(),
                        "duser": hrow.get("duser", "").strip(),
                    })
                    file_matches += 1

                if file_rows % 5000000 == 0:
                    progress(
                        f"  scanning {history_file}: rows={file_rows:,}; "
                        f"matches={file_matches:,}"
                    )

        progress(
            f"Finished {history_file}: rows={file_rows:,}; matches={file_matches:,}"
        )

    progress(f"Finished reverse scan. Reverse triples found: {len(reverse_matches):,}")

    return reverse_matches


def reverse_covering_intervals(reverse_hits):
    intervals = []

    for hit in reverse_hits:
        interval = normalize_interval(hit.get("cdate"), hit.get("ddate"))

        if interval is not None:
            intervals.append(interval)

    return merge_intervals(intervals)


def finalize_status(pre_status, required_intervals, reverse_key, reverse_matches):
    if pre_status != "needs_reverse_check":
        return pre_status

    covering_intervals = reverse_covering_intervals(
        reverse_matches.get(reverse_key, [])
    )

    covered, uncovered = intervals_fully_covered(
        required_intervals,
        covering_intervals,
    )

    if covered:
        return "not_violation_reverse_covers_required_lifecycle"

    return "historical_violation"


def write_validated(rows, reverse_matches, output_csv):
    progress(f"Writing output: {output_csv}")

    property_counts = defaultdict(int)
    tanon_id_counts = defaultdict(int)

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
        "reverse_history_file",
        "historical_status_property_level",
        "historical_status_tanon_constraint_id_level",
    ]

    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=output_fields)
        writer.writeheader()

        for i, row in enumerate(rows, start=1):
            reverse_key = row.get("_reverse_key")

            property_status = finalize_status(
                pre_status=row["_pre_status_property_level"],
                required_intervals=row["_required_intervals_property_level"],
                reverse_key=reverse_key,
                reverse_matches=reverse_matches,
            )

            tanon_id_status = finalize_status(
                pre_status=row["_pre_status_tanon_constraint_id_level"],
                required_intervals=row["_required_intervals_tanon_constraint_id_level"],
                reverse_key=reverse_key,
                reverse_matches=reverse_matches,
            )

            property_counts[property_status] += 1
            tanon_id_counts[tanon_id_status] += 1

            writer.writerow({
                "constraint_id": row.get("constraint_id", ""),
                "s": row.get("s", ""),
                "p": row.get("p", ""),
                "o": row.get("o", ""),
                "cdate": row.get("cdate", ""),
                "cuser": row.get("cuser", ""),
                "ddate": row.get("ddate", ""),
                "duser": row.get("duser", ""),
                "history_file": row.get("history_file", ""),
                "match_status": row.get("match_status", ""),
                "reverse_history_file": row.get("_reverse_history_file", ""),
                "historical_status_property_level": property_status,
                "historical_status_tanon_constraint_id_level": tanon_id_status,
            })

            if i % 100000 == 0:
                progress(
                    f"Wrote {i:,} rows | "
                    f"property_level={dict(property_counts)} | "
                    f"tanon_id_level={dict(tanon_id_counts)}"
                )

    summary = {
        "property_level": dict(property_counts),
        "tanon_constraint_id_level": dict(tanon_id_counts),
    }

    progress(f"Finished output | summary={summary}")

    with open(output_csv + ".summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(
        description="Validate symmetric corrections against historical symmetric constraints."
    )

    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)

    parser.add_argument(
        "--constraints",
        default="historical_constraints/symmetric_simplified_constraints.json",
    )

    parser.add_argument(
        "--wdt-folder",
        default="/bigdata-nfs/wd_history_work/h3_constraint_editors/wdt_folder",
    )

    parser.add_argument(
        "--index-dir",
        default="/bigdata-nfs/wd_history_work/index_qid_files/wd_subject_lookup_100",
    )

    parser.add_argument(
        "--lookup-report-every",
        type=int,
        default=10000,
    )

    args = parser.parse_args()

    constraints = load_symmetric_constraints(args.constraints)

    rows, reverse_targets_by_file = read_prepare_rows(
        input_csv=args.input,
        constraints=constraints,
        index_dir=args.index_dir,
        lookup_report_every=args.lookup_report_every,
    )

    reverse_matches = scan_reverse_triples(
        reverse_targets_by_file=reverse_targets_by_file,
        wdt_folder=args.wdt_folder,
    )

    write_validated(
        rows=rows,
        reverse_matches=reverse_matches,
        output_csv=args.output,
    )


if __name__ == "__main__":
    main()
