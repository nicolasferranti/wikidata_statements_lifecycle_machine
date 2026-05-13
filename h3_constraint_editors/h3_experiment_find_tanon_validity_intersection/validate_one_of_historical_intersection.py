#!/usr/bin/env python3

import argparse
import csv
import json
import os
import time
from datetime import datetime, timezone


def progress(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


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


def interval_intersects(a_start, a_end, b_start, b_end):
    if a_start is None or b_start is None:
        return False

    now = datetime.now(timezone.utc)

    if a_end is None:
        a_end = now
    if b_end is None:
        b_end = now

    return max(a_start, b_start) < min(a_end, b_end)


def wdt_to_wd_property(p):
    p = (p or "").strip()
    if not p.startswith("wdt:"):
        return None
    return "wd:" + p.replace("wdt:", "", 1)


def load_one_of_constraints(path):
    progress(f"Loading simplified one-of constraints: {path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    constraints = {}

    for prop, prop_data in raw.items():
        allowed = []
        exceptions = []

        for row in prop_data.get("pq:P2305", []):
            allowed.append({
                "entity": row.get("entity"),
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
            "allowed": allowed,
            "exceptions": exceptions,
        }

    progress(f"Loaded properties with one-of constraints: {len(constraints):,}")
    return constraints


def filter_by_constraint_id(entries, constraint_id_filter):
    if constraint_id_filter is None:
        return entries

    return [
        e for e in entries
        if e.get("constraint_id") == constraint_id_filter
    ]


def valid_entities_at(entries, t):
    result = set()

    for e in entries:
        start = e["start"]
        end = e["end"]

        if start is None:
            continue

        if t < start:
            continue

        if end is None or t < end:
            result.add(e["entity"])

    return result


def has_any_intersection(entries, triple_start, triple_end):
    for e in entries:
        if interval_intersects(triple_start, triple_end, e["start"], e["end"]):
            return True
    return False


def get_event_points(entries, triple_start, triple_end):
    now = datetime.now(timezone.utc)

    start = triple_start
    end = triple_end or now

    points = {start, end}

    for e in entries:
        e_start = e["start"]
        e_end = e["end"] or now

        if e_start is not None and start < e_start < end:
            points.add(e_start)

        if e_end is not None and start < e_end < end:
            points.add(e_end)

    return sorted(points)


def midpoint(a, b):
    return a + (b - a) / 2


def classify_one_of_row(row, constraints, constraint_id_filter=None):
    """
    constraint_id_filter=None:
        property-level validation using all historical one-of constraints for the property.

    constraint_id_filter=<wds:...>:
        validation only against the specific constraint instance.
    """
    if row.get("match_status") != "found":
        return "no_historical_row"

    prop = wdt_to_wd_property(row.get("p"))
    if prop is None:
        return "non_wdt_property"

    triple_start = parse_time(row.get("cdate"))
    triple_end = parse_time(row.get("ddate"))

    if triple_start is None:
        return "invalid_dates"

    if prop not in constraints:
        return "no_constraint_for_property"

    allowed_entries = filter_by_constraint_id(
        constraints[prop]["allowed"],
        constraint_id_filter,
    )

    exception_entries = filter_by_constraint_id(
        constraints[prop]["exceptions"],
        constraint_id_filter,
    )

    if not allowed_entries:
        return "no_constraint_for_property_instance"

    if not has_any_intersection(allowed_entries, triple_start, triple_end):
        return "no_constraint_lifecycle_intersection"

    relevant_exception_entries = [
        e for e in exception_entries
        if e["entity"] == row.get("s")
    ]

    event_points = get_event_points(
        allowed_entries + relevant_exception_entries,
        triple_start,
        triple_end,
    )

    saw_constraint_coverage = False
    saw_exception = False
    saw_allowed = False

    for a, b in zip(event_points[:-1], event_points[1:]):
        if a >= b:
            continue

        t = midpoint(a, b)

        allowed_now = valid_entities_at(allowed_entries, t)
        if not allowed_now:
            continue

        saw_constraint_coverage = True

        exceptions_now = valid_entities_at(relevant_exception_entries, t)
        if row.get("s") in exceptions_now:
            saw_exception = True
            continue

        if row.get("o") in allowed_now:
            saw_allowed = True
            continue

        return "historical_violation"

    if not saw_constraint_coverage:
        return "no_constraint_lifecycle_intersection"

    if saw_exception:
        return "not_violation_exception"

    if saw_allowed:
        return "not_violation_object_allowed"

    return "not_violation_other"


def validate_file(input_csv, output_csv, constraints_json, report_every):
    constraints = load_one_of_constraints(constraints_json)

    os.makedirs(os.path.dirname(output_csv) or ".", exist_ok=True)

    property_counts = {}
    tanon_id_counts = {}

    progress(f"Validating enriched file: {input_csv}")

    with open(input_csv, "r", newline="", encoding="utf-8") as fin, \
         open(output_csv, "w", newline="", encoding="utf-8") as fout:

        reader = csv.DictReader(fin)

        fieldnames = list(reader.fieldnames) + [
            "historical_status_property_level",
            "historical_status_tanon_constraint_id_level",
        ]

        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()

        for i, row in enumerate(reader, start=1):
            property_status = classify_one_of_row(
                row=row,
                constraints=constraints,
                constraint_id_filter=None,
            )

            tanon_id_status = classify_one_of_row(
                row=row,
                constraints=constraints,
                constraint_id_filter=row.get("constraint_id"),
            )

            row["historical_status_property_level"] = property_status
            row["historical_status_tanon_constraint_id_level"] = tanon_id_status

            property_counts[property_status] = property_counts.get(property_status, 0) + 1
            tanon_id_counts[tanon_id_status] = tanon_id_counts.get(tanon_id_status, 0) + 1

            writer.writerow(row)

            if i % report_every == 0:
                progress(
                    f"Processed {i:,} rows | "
                    f"property_level={property_counts} | "
                    f"tanon_id_level={tanon_id_counts}"
                )

    summary = {
        "property_level": property_counts,
        "tanon_constraint_id_level": tanon_id_counts,
    }

    progress(f"Finished validation. Final summary={summary}")

    report_path = output_csv + ".summary.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    progress(f"Wrote output: {output_csv}")
    progress(f"Wrote summary: {report_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Validate enriched one-of corrections against historical one-of constraints."
    )

    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--constraints",
        default="historical_constraints/one_of_simplified_constraints.json",
    )
    parser.add_argument(
        "--report-every",
        type=int,
        default=100000,
    )

    args = parser.parse_args()

    validate_file(
        input_csv=args.input,
        output_csv=args.output,
        constraints_json=args.constraints,
        report_every=args.report_every,
    )


if __name__ == "__main__":
    main()
