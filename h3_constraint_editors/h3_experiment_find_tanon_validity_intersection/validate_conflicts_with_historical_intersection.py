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
    intervals = sorted(intervals, key=lambda x: (x[0], x[1] or max_dt))

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


def wdt_to_wd_property(p):
    p = (p or "").strip()
    if not p.startswith("wdt:"):
        return None
    return "wd:" + p.replace("wdt:", "", 1)


def wd_to_wdt_property(p):
    p = (p or "").strip()
    if not p.startswith("wd:P"):
        return None
    return "wdt:" + p.replace("wd:", "", 1)


def load_conflicts_constraints(path):
    progress(f"Loading conflicts-with constraints: {path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    constraints = {}

    for prop, prop_data in raw.items():
        conflicts = []
        exceptions = []

        for row in prop_data.get("conflicts", []):
            conflicts.append({
                "pq:P2306": row.get("pq:P2306"),
                "pq:P2305": row.get("pq:P2305"),
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
            "conflicts": conflicts,
            "pq:P2303": exceptions,
        }

    progress(f"Loaded properties with conflicts-with constraints: {len(constraints):,}")

    return constraints


def filter_entries_by_constraint_id(entries, constraint_id_filter):
    if constraint_id_filter is None:
        return entries

    return [
        e for e in entries
        if e.get("constraint_id") == constraint_id_filter
    ]


def get_conflict_tests_for_row(row, constraints, constraint_id_filter=None):
    prop = wdt_to_wd_property(row.get("p"))

    if prop is None:
        return "non_wdt_property", []

    if prop not in constraints:
        return "no_constraint_for_property", []

    triple_interval = normalize_interval(row.get("cdate"), row.get("ddate"))

    if triple_interval is None:
        return "invalid_dates", []

    subject = row.get("s", "").strip()

    conflict_entries = filter_entries_by_constraint_id(
        constraints[prop]["conflicts"],
        constraint_id_filter,
    )

    exception_entries = filter_entries_by_constraint_id(
        constraints[prop]["pq:P2303"],
        constraint_id_filter,
    )

    if not conflict_entries:
        return "no_constraint_for_property_instance", []

    tests = []

    any_lifecycle_overlap = False

    for entry in conflict_entries:
        conflict_property_wd = entry.get("pq:P2306")
        conflict_property_wdt = wd_to_wdt_property(conflict_property_wd)
        conflict_value = entry.get("pq:P2305")

        if conflict_property_wdt is None:
            continue

        required_interval = interval_intersection(
            triple_interval,
            (entry["start"], entry["end"]),
        )

        if required_interval is None:
            continue

        any_lifecycle_overlap = True

        exception_intervals = []

        for exception in exception_entries:
            if exception.get("entity") != subject:
                continue

            exception_intersection = interval_intersection(
                required_interval,
                (exception["start"], exception["end"]),
            )

            if exception_intersection is not None:
                exception_intervals.append(exception_intersection)

        remaining_required = subtract_intervals(
            [required_interval],
            exception_intervals,
        )

        if not remaining_required:
            continue

        conflict_key = (
            subject,
            conflict_property_wdt,
            conflict_value,
        )

        tests.append({
            "conflict_key": conflict_key,
            "required_intervals": remaining_required,
            "conflict_property": conflict_property_wdt,
            "conflict_value": conflict_value,
        })

    if not tests:
        if not any_lifecycle_overlap:
            return "no_constraint_lifecycle_intersection", []

        return "not_violation_exception", []

    return "needs_conflict_check", tests


def read_prepare_rows(input_csv, constraints):
    progress(f"Reading input: {input_csv}")

    rows = []
    conflict_targets_by_file = defaultdict(set)

    property_counts = defaultdict(int)
    tanon_id_counts = defaultdict(int)

    with open(input_csv, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for i, row in enumerate(reader, start=1):
            row["_row_id"] = str(i - 1)

            row["_conflict_tests_property_level"] = []
            row["_conflict_tests_tanon_constraint_id_level"] = []

            if row.get("match_status") != "found":
                property_status = "no_historical_row"
                tanon_id_status = "no_historical_row"
            else:
                property_status, property_tests = get_conflict_tests_for_row(
                    row=row,
                    constraints=constraints,
                    constraint_id_filter=None,
                )

                tanon_id_status, tanon_tests = get_conflict_tests_for_row(
                    row=row,
                    constraints=constraints,
                    constraint_id_filter=row.get("constraint_id"),
                )

                row["_conflict_tests_property_level"] = property_tests
                row["_conflict_tests_tanon_constraint_id_level"] = tanon_tests

                if (
                    property_status == "needs_conflict_check"
                    or tanon_id_status == "needs_conflict_check"
                ):
                    history_file = row.get("history_file", "").strip()

                    if history_file:
                        for test in property_tests:
                            conflict_targets_by_file[history_file].add(test["conflict_key"])

                        for test in tanon_tests:
                            conflict_targets_by_file[history_file].add(test["conflict_key"])
                    else:
                        if property_status == "needs_conflict_check":
                            property_status = "missing_history_file"
                        if tanon_id_status == "needs_conflict_check":
                            tanon_id_status = "missing_history_file"

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

    progress(f"History files to scan: {len(conflict_targets_by_file):,}")

    return rows, conflict_targets_by_file


def scan_conflict_triples(conflict_targets_by_file, wdt_folder):
    progress("Scanning history files for conflicting triples")

    conflict_matches = defaultdict(list)
    total_files = len(conflict_targets_by_file)

    for file_idx, (history_file, targets) in enumerate(conflict_targets_by_file.items(), start=1):
        path = os.path.join(wdt_folder, history_file)

        if not os.path.exists(path):
            progress(f"WARN missing history file: {path}")
            continue

        progress(
            f"Scanning {file_idx:,}/{total_files:,} ({pct(file_idx, total_files)}): "
            f"{history_file}; conflict_targets={len(targets):,}"
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
                    conflict_matches[key].append({
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

    progress(f"Finished conflict scan. Conflict triples found: {len(conflict_matches):,}")

    return conflict_matches


def conflict_intersects_required_lifecycle(conflict_hits, required_intervals):
    for hit in conflict_hits:
        conflict_interval = normalize_interval(hit.get("cdate"), hit.get("ddate"))

        if conflict_interval is None:
            continue

        for required_interval in required_intervals:
            if interval_intersection(conflict_interval, required_interval) is not None:
                return True

    return False


def finalize_status(pre_status, conflict_tests, conflict_matches):
    if pre_status != "needs_conflict_check":
        return pre_status

    for test in conflict_tests:
        conflict_key = test["conflict_key"]
        required_intervals = test["required_intervals"]

        if conflict_intersects_required_lifecycle(
            conflict_matches.get(conflict_key, []),
            required_intervals,
        ):
            return "historical_violation"

    return "not_violation_no_conflicting_statement"


def summarize_conflict_properties(conflict_tests):
    props = sorted({
        test["conflict_property"]
        for test in conflict_tests
        if test.get("conflict_property")
    })
    return ";".join(props)


def write_validated(rows, conflict_matches, output_csv):
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
        "matched_history_o",
        "conflict_properties_property_level",
        "conflict_properties_tanon_constraint_id_level",
        "historical_status_property_level",
        "historical_status_tanon_constraint_id_level",
    ]

    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=output_fields)
        writer.writeheader()

        for i, row in enumerate(rows, start=1):
            property_status = finalize_status(
                pre_status=row["_pre_status_property_level"],
                conflict_tests=row["_conflict_tests_property_level"],
                conflict_matches=conflict_matches,
            )

            tanon_id_status = finalize_status(
                pre_status=row["_pre_status_tanon_constraint_id_level"],
                conflict_tests=row["_conflict_tests_tanon_constraint_id_level"],
                conflict_matches=conflict_matches,
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
                "matched_history_o": row.get("matched_history_o", ""),
                "conflict_properties_property_level": summarize_conflict_properties(
                    row["_conflict_tests_property_level"]
                ),
                "conflict_properties_tanon_constraint_id_level": summarize_conflict_properties(
                    row["_conflict_tests_tanon_constraint_id_level"]
                ),
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
        description="Validate conflicts-with corrections against historical constraints."
    )

    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)

    parser.add_argument(
        "--constraints",
        default="historical_constraints/conflicts_with_simplified_constraints.json",
    )

    parser.add_argument(
        "--wdt-folder",
        default="/bigdata-nfs/wd_history_work/h3_constraint_editors/wdt_folder",
    )

    args = parser.parse_args()

    constraints = load_conflicts_constraints(args.constraints)

    rows, conflict_targets_by_file = read_prepare_rows(
        input_csv=args.input,
        constraints=constraints,
    )

    conflict_matches = scan_conflict_triples(
        conflict_targets_by_file=conflict_targets_by_file,
        wdt_folder=args.wdt_folder,
    )

    write_validated(
        rows=rows,
        conflict_matches=conflict_matches,
        output_csv=args.output,
    )


if __name__ == "__main__":
    main()
