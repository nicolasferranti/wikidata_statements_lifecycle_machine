#!/usr/bin/env python3

import argparse
import csv
import os
import time
from collections import defaultdict
import json
import re
import codecs
from decimal import Decimal, InvalidOperation


def progress(message):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def pct(done, total):
    if total == 0:
        return "100.0%"
    return f"{(done / total) * 100:.1f}%"


def normalize_object_for_match(value):
    """
    Normalize object values only for matching.

    Handles:
    - entity values: wd:Q...
    - quoted literals: \"8488\", \"\"\"8488\"\"\"
    - unicode escapes: \\u015B -> ś
    - typed date literals: 1951-00-00T00:00:00Z^^...
    - Wikidata JSON time literals: {"time":"+1951-00-00T00:00:00Z", ...}
    - typed decimals: 2239^^...#decimal
    - Wikidata quantity JSON literals: {"amount":"+2239","unit":"1"}
    """
    if value is None:
        return ""

    v = str(value).strip()

    if v.startswith(("wd:", "wds:", "wdt:")):
        return v

    v = decode_unicode_escapes(v)

    # Remove repeated outer quotes introduced by CSV/RDF literal escaping.
    changed = True
    while changed and len(v) >= 2:
        changed = False
        if v.startswith('"') and v.endswith('"'):
            v = v[1:-1].strip()
            changed = True

    v = decode_unicode_escapes(v)

    # Typed literal: lexical^^datatype
    if "^^" in v:
        lexical, datatype = v.split("^^", 1)
        lexical = lexical.strip().strip('"')
        datatype = datatype.strip()

        if datatype.endswith("#dateTime"):
            return canonicalize_wikidata_time(lexical)

        if datatype.endswith("#decimal") or datatype.endswith("#integer"):
            return canonicalize_decimal(lexical)

        return decode_unicode_escapes(lexical)

    # JSON-style Wikidata time literal
    if '"time"' in v or "'time'" in v:
        time_value = extract_jsonish_field(v, "time")
        if time_value is not None:
            return canonicalize_wikidata_time(time_value)

    # JSON-style Wikidata quantity literal
    if '"amount"' in v or "'amount'" in v:
        amount_value = extract_jsonish_field(v, "amount")
        if amount_value is not None:
            return canonicalize_decimal(amount_value)

    # Language-tagged literals after CSV parsing:
    # "text"@en -> text@en
    if v.startswith('"') and '"@' in v:
        first_lang_quote = v.rfind('"@')
        if first_lang_quote > 0:
            v = v[1:first_lang_quote] + v[first_lang_quote + 1:]

    return decode_unicode_escapes(v)


def decode_unicode_escapes(v):
    """
    Decode strings containing literal \\uXXXX sequences without damaging normal UTF-8 text.
    """
    if not isinstance(v, str):
        return v

    if "\\u" not in v and "\\U" not in v:
        return v

    try:
        return codecs.decode(v, "unicode_escape")
    except Exception:
        return v


def extract_jsonish_field(v, field):
    """
    Extract field from JSON-like literal.

    Handles both valid JSON and escaped CSV/RDF variants.
    """
    try:
        obj = json.loads(v)
        if isinstance(obj, dict) and field in obj:
            return obj[field]
    except Exception:
        pass

    pattern = rf'"{re.escape(field)}"\s*:\s*"([^"]+)"'
    m = re.search(pattern, v)
    if m:
        return m.group(1)

    pattern = rf"'{re.escape(field)}'\s*:\s*'([^']+)'"
    m = re.search(pattern, v)
    if m:
        return m.group(1)

    return None


def canonicalize_decimal(x):
    """
    Canonicalize decimal/quantity values.

    +2239 -> 2239
    2239.0 -> 2239
    +2239.50 -> 2239.5
    """
    if x is None:
        return ""

    x = str(x).strip().strip('"')

    if x.startswith("+"):
        x = x[1:]

    try:
        d = Decimal(x)
        return format(d.normalize(), "f")
    except InvalidOperation:
        return x


def canonicalize_wikidata_time(t):
    """
    Canonicalize Wikidata time strings for matching.

    +1951-00-00T00:00:00Z -> 1951-00-00T00:00:00Z
    +00000001951-06-04T00:00:00Z -> 1951-06-04T00:00:00Z
    """
    if t is None:
        return ""

    t = str(t).strip().strip('"')

    if t.startswith("+"):
        t = t[1:]

    m = re.match(r"^0+(\d{4,})-(\d{2})-(\d{2})T", t)
    if m:
        year = str(int(m.group(1)))
        rest = t[t.find("-"):]
        t = year + rest

    return t


def extract_time_from_jsonish_literal(v):
    """
    Extract the Wikidata time field from a JSON-like literal.

    Handles both valid JSON and escaped CSV/RDF variants.
    """
    # Try JSON first
    try:
        obj = json.loads(v)
        if isinstance(obj, dict) and "time" in obj:
            return obj["time"]
    except Exception:
        pass

    # Regex fallback
    m = re.search(r'"time"\s*:\s*"([^"]+)"', v)
    if m:
        return m.group(1)

    m = re.search(r"'time'\s*:\s*'([^']+)'", v)
    if m:
        return m.group(1)

    return None


def make_match_key(s, p, o):
    return (
        (s or "").strip(),
        (p or "").strip(),
        normalize_object_for_match(o),
    )


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

    missing = sum(filename is None for filename in subject_to_file.values())

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
            targets_by_file[filename].add(
                make_match_key(row["s"], row["p"], row["o"])
            )

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

                key = make_match_key(
                    hrow.get("s", ""),
                    hrow.get("p", ""),
                    hrow.get("o", ""),
                )

                if key in targets:
                    matches[key].append({
                        "cdate": hrow.get("cdate", "").strip(),
                        "cuser": hrow.get("cuser", "").strip(),
                        "ddate": hrow.get("ddate", "").strip(),
                        "duser": hrow.get("duser", "").strip(),
                        "matched_history_o": hrow.get("o", "").strip(),
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
        "matched_history_o",
    ]

    written = 0
    missing = 0

    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=output_fields)
        writer.writeheader()

        for i, row in enumerate(rows, start=1):
            key = make_match_key(row["s"], row["p"], row["o"])
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
                    "matched_history_o": "",
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
                        "matched_history_o": hit["matched_history_o"],
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
    )

    parser.add_argument(
        "--wdt-folder",
        default="/bigdata-nfs/wd_history_work/h3_constraint_editors/wdt_folder",
    )

    parser.add_argument(
        "--lookup-report-every",
        type=int,
        default=10000,
    )

    parser.add_argument(
        "--scan-report-every-rows",
        type=int,
        default=5000000,
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
