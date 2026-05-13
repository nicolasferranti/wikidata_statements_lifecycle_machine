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
    """Parse timestamp strings used in CSV/JSON exports."""
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
    """Return a half-open interval [start, end), where end may be None."""
    start = parse_time(start)
    end = parse_time(end)

    if start is None:
        return None

    return (start, end)


def interval_intersection(a, b):
    """Intersect two half-open intervals."""
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
    """Merge overlapping or touching intervals."""
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
    """Subtract one interval from another."""
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
    """Subtract a set of intervals from another set of intervals."""
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
    """Check whether covering_intervals fully cover required_intervals."""
    uncovered = subtract_intervals(required_intervals, covering_intervals)
    return len(uncovered) == 0, uncovered


def wdt_to_wd_property(p):
    """Convert wdt:P123 to wd:P123."""
    p = (p or "").strip()

    if not p.startswith("wdt:"):
        return None

    return "wd:" + p.replace("wdt:", "", 1)


def wd_to_wdt_property(p):
    """Convert wd:P123 to wdt:P123."""
    p = (p or "").strip()

    if not p.startswith("wd:P"):
        return None

    return "wdt:" + p.replace("wd:", "", 1)


def load_item_requires_constraints(path):
    """
    Load simplified item-requires-statement constraints.

    Each requirement row means:
      if original triple exists, then subject s must have
      (s, pq:P2306, pq:P2305), or if pq:P2305 is null,
      any value for pq:P2306.
    """
    progress(f"Loading item-requires-statement constraints: {path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    constraints = {}

    for prop, prop_data in raw.items():
        requirements = []
        exceptions = []

        for row in prop_data.get("requirements", []):
            requirements.append({
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
            "requirements": requirements,
            "pq:P2303": exceptions,
        }

    progress(
        f"Loaded properties with item-requires constraints: {len(constraints):,}"
    )

    return constraints


def filter_entries_by_constraint_id(entries, constraint_id_filter):
    """Optionally restrict entries to Tanon's referenced constraint_id."""
    if constraint_id_filter is None:
        return entries

    return [
        e for e in entries
        if e.get("constraint_id") == constraint_id_filter
    ]


def collect_event_points(intervals):
    """
    Collect interval boundary points.

    For open-ended intervals, use current time as the temporary right boundary
    so that active constraints/triples can still be tested.
    """
    points = set()
    now = datetime.now(timezone.utc)

    for start, end in intervals:
        if start is not None:
            points.add(start)

        if end is not None:
            points.add(end)
        else:
            points.add(now)

    return sorted(points)

def active_rows_during_segment(rows, segment):
    """Return requirement rows whose intervals fully cover the segment midpoint."""
    start, end = segment

    # For open-ended segment end, testing at start is enough.
    if end is None:
        t = start
    else:
        t = start + (end - start) / 2

    active = []

    for row in rows:
        r_start = row["interval"][0]
        r_end = row["interval"][1]

        if r_start is None:
            continue

        if t < r_start:
            continue

        if r_end is None or t < r_end:
            active.append(row)

    return active


def build_requirement_groups_for_row(row, constraints, constraint_id_filter=None):
    """
    Build disjunctive requirement groups for one input row.

    For each original triple lifecycle and historical constraint lifecycle,
    this creates tests of the form:

      property-only:
        subject must have some (s, required_property, ANY)

      value-specific:
        subject must have at least one of
        (s, required_property, allowed_value_1),
        (s, required_property, allowed_value_2), ...

    Multiple pq:P2305 values active for the same constraint_id and pq:P2306
    are treated as alternatives, not cumulative obligations.
    """
    prop = wdt_to_wd_property(row.get("p"))

    if prop is None:
        return "non_wdt_property", []

    if prop not in constraints:
        return "no_constraint_for_property", []

    triple_interval = normalize_interval(row.get("cdate"), row.get("ddate"))

    if triple_interval is None:
        return "invalid_dates", []

    subject = row.get("s", "").strip()

    requirement_entries = filter_entries_by_constraint_id(
        constraints[prop]["requirements"],
        constraint_id_filter,
    )

    exception_entries = filter_entries_by_constraint_id(
        constraints[prop]["pq:P2303"],
        constraint_id_filter,
    )

    if not requirement_entries:
        return "no_constraint_for_property_instance", []

    # First intersect each requirement row with the original triple lifecycle.
    # Then subtract exception intervals for this subject.
    rows_by_logical_requirement = defaultdict(list)
    any_lifecycle_overlap = False

    for entry in requirement_entries:
        required_property_wd = entry.get("pq:P2306")
        required_property_wdt = wd_to_wdt_property(required_property_wd)

        if required_property_wdt is None:
            continue

        inter = interval_intersection(
            triple_interval,
            (entry["start"], entry["end"]),
        )

        if inter is None:
            continue

        any_lifecycle_overlap = True

        exception_intervals = []

        for exception in exception_entries:
            if exception.get("entity") != subject:
                continue

            exc_inter = interval_intersection(
                inter,
                (exception["start"], exception["end"]),
            )

            if exc_inter is not None:
                exception_intervals.append(exc_inter)

        remaining_intervals = subtract_intervals([inter], exception_intervals)

        if not remaining_intervals:
            continue

        logical_key = (
            entry.get("constraint_id"),
            required_property_wdt,
        )

        for remaining_interval in remaining_intervals:
            rows_by_logical_requirement[logical_key].append({
                "constraint_id": entry.get("constraint_id"),
                "required_property": required_property_wdt,
                "required_value": entry.get("pq:P2305"),
                "interval": remaining_interval,
            })

    if not rows_by_logical_requirement:
        if not any_lifecycle_overlap:
            return "no_constraint_lifecycle_intersection", []

        return "not_violation_exception", []

    tests = []

    # For each constraint_id + required property, split into stable time segments.
    for logical_key, req_rows in rows_by_logical_requirement.items():
        event_points = collect_event_points([r["interval"] for r in req_rows])

        if len(event_points) < 2:
            continue

        for seg_start, seg_end in zip(event_points[:-1], event_points[1:]):
            if seg_start >= seg_end:
                continue

            segment = (seg_start, seg_end)
            active = active_rows_during_segment(req_rows, segment)

            if not active:
                continue

            required_property = active[0]["required_property"]

            values = {
                r["required_value"]
                for r in active
                if r["required_value"] is not None
            }

            has_property_only = any(
                r["required_value"] is None
                for r in active
            )

            if has_property_only:
                tests.append({
                    "kind": "property_only",
                    "required_property": required_property,
                    "allowed_values": set(),
                    "required_intervals": [segment],
                })
            else:
                tests.append({
                    "kind": "value_specific",
                    "required_property": required_property,
                    "allowed_values": values,
                    "required_intervals": [segment],
                })

    if not tests:
        return "no_constraint_lifecycle_intersection", []

    return "needs_required_statement_check", tests


def read_prepare_rows(input_csv, constraints):
    """
    Read enriched Tanon rows and prepare exact/property-only lookup targets.

    Since item-requires checks statements on the same subject s, we reuse
    the enriched row's history_file.
    """
    progress(f"Reading input: {input_csv}")

    rows = []

    exact_targets_by_file = defaultdict(set)
    property_targets_by_file = defaultdict(set)

    property_counts = defaultdict(int)
    tanon_id_counts = defaultdict(int)

    with open(input_csv, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for i, row in enumerate(reader, start=1):
            row["_row_id"] = str(i - 1)

            row["_tests_property_level"] = []
            row["_tests_tanon_constraint_id_level"] = []

            if row.get("match_status") != "found":
                property_status = "no_historical_row"
                tanon_id_status = "no_historical_row"
            else:
                property_status, property_tests = build_requirement_groups_for_row(
                    row=row,
                    constraints=constraints,
                    constraint_id_filter=None,
                )

                tanon_id_status, tanon_tests = build_requirement_groups_for_row(
                    row=row,
                    constraints=constraints,
                    constraint_id_filter=row.get("constraint_id"),
                )

                row["_tests_property_level"] = property_tests
                row["_tests_tanon_constraint_id_level"] = tanon_tests

                needs_scan = (
                    property_status == "needs_required_statement_check"
                    or tanon_id_status == "needs_required_statement_check"
                )

                if needs_scan:
                    history_file = row.get("history_file", "").strip()

                    if not history_file:
                        if property_status == "needs_required_statement_check":
                            property_status = "missing_history_file"
                        if tanon_id_status == "needs_required_statement_check":
                            tanon_id_status = "missing_history_file"
                    else:
                        for test in property_tests + tanon_tests:
                            subject = row.get("s", "").strip()
                            required_property = test["required_property"]

                            if test["kind"] == "property_only":
                                property_targets_by_file[history_file].add(
                                    (subject, required_property)
                                )
                            else:
                                for value in test["allowed_values"]:
                                    exact_targets_by_file[history_file].add(
                                        (subject, required_property, value)
                                    )

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

    progress(
        f"History files to scan: "
        f"exact={len(exact_targets_by_file):,}; "
        f"property_only={len(property_targets_by_file):,}"
    )

    return rows, exact_targets_by_file, property_targets_by_file


def scan_required_statements(
    exact_targets_by_file,
    property_targets_by_file,
    wdt_folder,
):
    """
    Scan history files once and collect matching required statements.

    exact target:
      (s, p, o)

    property-only target:
      (s, p), any object satisfies.
    """
    progress("Scanning history files for required statements")

    all_files = sorted(
        set(exact_targets_by_file.keys()) | set(property_targets_by_file.keys())
    )

    exact_matches = defaultdict(list)
    property_matches = defaultdict(list)

    for file_idx, history_file in enumerate(all_files, start=1):
        exact_targets = exact_targets_by_file.get(history_file, set())
        property_targets = property_targets_by_file.get(history_file, set())

        path = os.path.join(wdt_folder, history_file)

        if not os.path.exists(path):
            progress(f"WARN missing history file: {path}")
            continue

        progress(
            f"Scanning {file_idx:,}/{len(all_files):,} ({pct(file_idx, len(all_files))}): "
            f"{history_file}; exact={len(exact_targets):,}; "
            f"property_only={len(property_targets):,}"
        )

        file_rows = 0
        file_exact_matches = 0
        file_property_matches = 0

        with open(path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)

            for hrow in reader:
                file_rows += 1

                s = hrow.get("s", "").strip()
                p = hrow.get("p", "").strip()
                o = hrow.get("o", "").strip()

                exact_key = (s, p, o)
                property_key = (s, p)

                hit = {
                    "cdate": hrow.get("cdate", "").strip(),
                    "cuser": hrow.get("cuser", "").strip(),
                    "ddate": hrow.get("ddate", "").strip(),
                    "duser": hrow.get("duser", "").strip(),
                    "o": o,
                }

                if exact_key in exact_targets:
                    exact_matches[exact_key].append(hit)
                    file_exact_matches += 1

                if property_key in property_targets:
                    property_matches[property_key].append(hit)
                    file_property_matches += 1

                if file_rows % 5000000 == 0:
                    progress(
                        f"  scanning {history_file}: rows={file_rows:,}; "
                        f"exact_matches={file_exact_matches:,}; "
                        f"property_matches={file_property_matches:,}"
                    )

        progress(
            f"Finished {history_file}: rows={file_rows:,}; "
            f"exact_matches={file_exact_matches:,}; "
            f"property_matches={file_property_matches:,}"
        )

    progress(
        f"Finished required statement scan. "
        f"exact_keys_found={len(exact_matches):,}; "
        f"property_keys_found={len(property_matches):,}"
    )

    return exact_matches, property_matches


def hit_intervals(hits):
    """Convert matched history rows into lifecycle intervals."""
    intervals = []

    for hit in hits:
        interval = normalize_interval(hit.get("cdate"), hit.get("ddate"))

        if interval is not None:
            intervals.append(interval)

    return merge_intervals(intervals)


def test_is_covered(row, test, exact_matches, property_matches):
    """
    Check whether a single disjunctive requirement test is fully satisfied.

    Property-only:
      coverage comes from any (s, required_property, *)

    Value-specific:
      coverage is the union of all acceptable value-specific matches.
    """
    subject = row.get("s", "").strip()
    required_property = test["required_property"]
    required_intervals = test["required_intervals"]

    covering_intervals = []

    if test["kind"] == "property_only":
        property_key = (subject, required_property)
        covering_intervals = hit_intervals(property_matches.get(property_key, []))
    else:
        for value in test["allowed_values"]:
            exact_key = (subject, required_property, value)
            covering_intervals.extend(
                hit_intervals(exact_matches.get(exact_key, []))
            )

        covering_intervals = merge_intervals(covering_intervals)

    covered, uncovered = intervals_fully_covered(
        required_intervals,
        covering_intervals,
    )

    return covered


def finalize_status(pre_status, row, tests, exact_matches, property_matches):
    """
    Finalize one validation mode.

    All logical requirement groups must be satisfied.
    If any required segment is uncovered, the original triple is a violation.
    """
    if pre_status != "needs_required_statement_check":
        return pre_status

    for test in tests:
        if not test_is_covered(row, test, exact_matches, property_matches):
            return "historical_violation"

    return "not_violation_required_statement_covers_required_lifecycle"


def summarize_required_properties(tests):
    props = sorted({
        test["required_property"]
        for test in tests
        if test.get("required_property")
    })

    return ";".join(props)


def write_validated(rows, exact_matches, property_matches, output_csv):
    """Write final validation statuses and summary JSON."""
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
        "required_properties_property_level",
        "required_properties_tanon_constraint_id_level",
        "historical_status_property_level",
        "historical_status_tanon_constraint_id_level",
    ]

    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=output_fields)
        writer.writeheader()

        for i, row in enumerate(rows, start=1):
            property_status = finalize_status(
                pre_status=row["_pre_status_property_level"],
                row=row,
                tests=row["_tests_property_level"],
                exact_matches=exact_matches,
                property_matches=property_matches,
            )

            tanon_id_status = finalize_status(
                pre_status=row["_pre_status_tanon_constraint_id_level"],
                row=row,
                tests=row["_tests_tanon_constraint_id_level"],
                exact_matches=exact_matches,
                property_matches=property_matches,
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
                "required_properties_property_level": summarize_required_properties(
                    row["_tests_property_level"]
                ),
                "required_properties_tanon_constraint_id_level": summarize_required_properties(
                    row["_tests_tanon_constraint_id_level"]
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
        description="Validate item-requires-statement corrections historically."
    )

    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)

    parser.add_argument(
        "--constraints",
        default="historical_constraints/item_requires_statement_simplified_constraints.json",
    )

    parser.add_argument(
        "--wdt-folder",
        default="/bigdata-nfs/wd_history_work/h3_constraint_editors/wdt_folder",
    )

    args = parser.parse_args()

    constraints = load_item_requires_constraints(args.constraints)

    rows, exact_targets_by_file, property_targets_by_file = read_prepare_rows(
        input_csv=args.input,
        constraints=constraints,
    )

    exact_matches, property_matches = scan_required_statements(
        exact_targets_by_file=exact_targets_by_file,
        property_targets_by_file=property_targets_by_file,
        wdt_folder=args.wdt_folder,
    )

    write_validated(
        rows=rows,
        exact_matches=exact_matches,
        property_matches=property_matches,
        output_csv=args.output,
    )


if __name__ == "__main__":
    main()
