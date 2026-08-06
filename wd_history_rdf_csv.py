#!/usr/bin/env python3
"""
Wikidata pages-meta-history XML -> CSV/TSV of triple lifetimes.

The extractor streams one shard at a time, reconstructs the RDF-ish triple set
for each valid entity revision, and writes one row for every interval in which a
triple is active. Intervals are [cdate, ddate): a triple is active from the
creation revision until immediately before the deletion revision.

Supported Wikidata entity namespaces are explicit: 0 (items), 120
(properties), and 146 (lexemes). Pages in other namespaces are skipped before
revision text is interpreted as Wikibase JSON.

Timing notes for metrics:
- xml_iteration_seconds measures time spent advancing the streaming XML parser.
- json_parsing_seconds measures JSON decoding of revision text.
- triple_extraction_seconds measures conversion from entity JSON to triple sets.
- delta_computation_seconds measures set-delta construction and active-state updates.
- csv_writing_seconds measures csv.writer row writes.
These stages are measured around local operations and may overlap slightly with
interpreter overhead outside the timed blocks.
"""

from __future__ import annotations

import argparse
import bz2
import csv
import gzip
import io
import json
import os
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, Optional, Sequence, Set, Tuple

MW_NS = "{http://www.mediawiki.org/xml/export-0.11/}"
SCHEMA_VERSION = "2.0.0"
SUPPORTED_ENTITY_NAMESPACES = {"0", "120", "146"}

LEGACY_HEADER = ["s", "p", "o", "cdate", "cuser", "ddate", "duser"]
EXTENDED_HEADER = LEGACY_HEADER + [
    "entity_id",
    "page_id",
    "crevid",
    "cparentid",
    "drevid",
    "dparentid",
    "source_shard",
    "schema_version",
    "quality_flags",
]
EVENT_HEADER = [
    "entity_id",
    "page_id",
    "revision_id",
    "parent_revision_id",
    "timestamp",
    "contributor",
    "action",
    "s",
    "p",
    "o",
    "source_shard",
    "schema_version",
    "quality_flags",
]


# --------- IO ---------

def open_maybe_compressed(path: str) -> io.BufferedReader:
    if path.endswith(".bz2"):
        return bz2.open(path, "rb")
    if path.endswith(".gz"):
        return gzip.open(path, "rb")
    return open(path, "rb")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def atomic_write_json(path: str, payload: dict) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_publish_file(tmp_path: str, final_path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(final_path)) or ".", exist_ok=True)
    os.replace(tmp_path, final_path)


class AtomicTextWriter:
    def __init__(self, path: Optional[str], newline: str = ""):
        self.path = path
        self.newline = newline
        self.tmp_path: Optional[str] = None
        self.handle: Optional[io.TextIOWrapper] = None

    def __enter__(self) -> Optional[io.TextIOWrapper]:
        if not self.path:
            return None
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(self.path)}.", suffix=".tmp", dir=directory)
        self.tmp_path = tmp_path
        self.handle = os.fdopen(fd, "w", encoding="utf-8", newline=self.newline)
        return self.handle

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None
        if not self.path or not self.tmp_path:
            return
        if exc_type is None:
            atomic_publish_file(self.tmp_path, self.path)
        else:
            try:
                os.unlink(self.tmp_path)
            except OSError:
                pass
        self.tmp_path = None


# --------- Metrics and errors ---------

def new_metrics(input_path: str, output_path: str, output_schema: str, emit_events: bool, events_output: Optional[str]) -> dict:
    input_bytes = 0
    try:
        input_bytes = os.path.getsize(input_path)
    except OSError:
        pass

    return {
        "schema_version": SCHEMA_VERSION,
        "output_schema": output_schema,
        "emit_events": emit_events,
        "input_path": input_path,
        "input_basename": os.path.basename(input_path),
        "input_compressed_bytes": input_bytes,
        "output_path": output_path,
        "output_bytes": 0,
        "output_data_rows": 0,
        "started_at_utc": utc_now_iso(),
        "finished_at_utc": "",
        "total_seconds": 0.0,
        "xml_iteration_seconds": 0.0,
        "json_parsing_seconds": 0.0,
        "triple_extraction_seconds": 0.0,
        "delta_computation_seconds": 0.0,
        "csv_writing_seconds": 0.0,
        "pages_seen": 0,
        "pages_processed": 0,
        "pages_without_title": 0,
        "pages_skipped_non_entity_namespace": 0,
        "revisions_seen": 0,
        "revisions_with_valid_entity_json": 0,
        "revisions_missing_text": 0,
        "revisions_with_empty_text": 0,
        "revisions_with_invalid_json": 0,
        "revisions_with_non_object_json": 0,
        "revisions_with_unknown_json_structure": 0,
        "revisions_missing_id": 0,
        "revisions_missing_timestamp": 0,
        "revisions_missing_contributor": 0,
        "revisions_skipped_non_entity_namespace": 0,
        "entity_id_mismatches": 0,
        "redirect_revisions": 0,
        "redirect_entity_mismatches": 0,
        "triples_extracted_total": 0,
        "triple_add_events": 0,
        "triple_delete_events": 0,
        "closed_lifecycle_rows": 0,
        "open_lifecycle_rows": 0,
        "warnings": 0,
        "errors": 0,
        "event_output_path": events_output if emit_events else None,
        "event_output_bytes": 0,
        "event_output_rows": 0,
        "event_add_rows": 0,
        "event_delete_rows": 0,
        "completed_successfully": True,
    }


class ErrorLogger:
    def __init__(self, path: Optional[str], input_basename: str, metrics: dict):
        self.path = path
        self.input_basename = input_basename
        self.metrics = metrics
        self.tmp_path: Optional[str] = None
        self.handle: Optional[io.TextIOWrapper] = None

        if path:
            directory = os.path.dirname(os.path.abspath(path)) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
            self.tmp_path = tmp_path
            self.handle = os.fdopen(fd, "w", encoding="utf-8")

    def log(
        self,
        level: str,
        error_type: str,
        message: str,
        *,
        page_title: str = "",
        page_id: str = "",
        entity_id: str = "",
        revision_id: str = "",
        parent_revision_id: str = "",
        timestamp: str = "",
        contributor: str = "",
        json_keys: Optional[Sequence[str]] = None,
    ) -> None:
        if level == "error":
            self.metrics["errors"] += 1
        else:
            self.metrics["warnings"] += 1

        record = {
            "level": level,
            "error_type": error_type,
            "message": message,
            "input_basename": self.input_basename,
            "page_title": page_title,
            "page_id": page_id,
            "entity_id": entity_id,
            "revision_id": revision_id,
            "parent_revision_id": parent_revision_id,
            "timestamp": timestamp,
            "contributor": contributor,
        }
        if json_keys is not None:
            record["json_keys"] = sorted(str(k) for k in json_keys)
        if self.handle is not None:
            self.handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def close(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None

    def publish(self) -> None:
        self.close()
        if self.path and self.tmp_path:
            atomic_publish_file(self.tmp_path, self.path)
            self.tmp_path = None


# --------- XML metadata helpers ---------

def child_text(elem: ET.Element, tag: str) -> str:
    child = elem.find(f"{MW_NS}{tag}")
    return child.text.strip() if child is not None and child.text else ""


def page_id(page_elem: ET.Element) -> str:
    return child_text(page_elem, "id")


def page_title(page_elem: ET.Element) -> str:
    return child_text(page_elem, "title")


def page_namespace(page_elem: ET.Element) -> str:
    return child_text(page_elem, "ns")


def revision_id(rev_elem: ET.Element) -> str:
    return child_text(rev_elem, "id")


def parent_revision_id(rev_elem: ET.Element) -> str:
    return child_text(rev_elem, "parentid")


def revision_timestamp(rev_elem: ET.Element) -> str:
    return child_text(rev_elem, "timestamp")


def contributor_to_user(rev_elem: ET.Element) -> str:
    contrib = rev_elem.find(f"{MW_NS}contributor")
    if contrib is None:
        return ""
    username = contrib.find(f"{MW_NS}username")
    if username is not None and username.text:
        return username.text.strip()
    ip = contrib.find(f"{MW_NS}ip")
    if ip is not None and ip.text:
        return ip.text.strip()
    return ""


@dataclass
class RevisionMeta:
    revision_id: str
    parent_revision_id: str
    timestamp: str
    contributor: str
    flags: Set[str] = field(default_factory=set)


def extract_revision_meta(
    rev_elem: ET.Element,
    metrics: dict,
    errors: ErrorLogger,
    page_title_value: str,
    page_id_value: str,
) -> RevisionMeta:
    rid = revision_id(rev_elem)
    parent_id = parent_revision_id(rev_elem)
    ts = revision_timestamp(rev_elem)
    user = contributor_to_user(rev_elem)
    flags: Set[str] = set()

    if not rid:
        metrics["revisions_missing_id"] += 1
        flags.add("missing_creation_revision_id")
        errors.log(
            "warning",
            "missing_revision_id",
            "Revision is missing an <id> value.",
            page_title=page_title_value,
            page_id=page_id_value,
            timestamp=ts,
            contributor=user,
        )
    if not ts:
        metrics["revisions_missing_timestamp"] += 1
        flags.add("missing_creation_timestamp")
        errors.log(
            "warning",
            "missing_revision_timestamp",
            "Revision is missing a timestamp.",
            page_title=page_title_value,
            page_id=page_id_value,
            revision_id=rid,
            parent_revision_id=parent_id,
            contributor=user,
        )
    if not user:
        metrics["revisions_missing_contributor"] += 1
        flags.add("missing_creation_contributor")
        errors.log(
            "warning",
            "missing_contributor",
            "Revision is missing contributor username/IP.",
            page_title=page_title_value,
            page_id=page_id_value,
            revision_id=rid,
            parent_revision_id=parent_id,
            timestamp=ts,
        )

    return RevisionMeta(rid, parent_id, ts, user, flags)


# --------- Decode/classify JSON from <text> ---------

def parse_entity_json_from_text(text: str) -> Tuple[Optional[object], bool]:
    raw = text.strip()
    if not raw:
        return None, False
    try:
        return json.loads(raw), True
    except json.JSONDecodeError:
        return None, False


def classify_revision_json(data: object) -> str:
    if not isinstance(data, dict):
        return "non_object"
    if isinstance(data.get("id"), str) and data.get("id"):
        return "entity"
    if isinstance(data.get("entity"), str) and data.get("entity") and isinstance(data.get("redirect"), str) and data.get("redirect"):
        return "redirect"
    return "unknown_object"


# --------- RDF-ish term formatting ---------

def escape_literal(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


def lit_lang(text: str, lang: str) -> str:
    return f"\"{escape_literal(text)}\"@{lang}"


def lit_plain(text: str) -> str:
    return f"\"{escape_literal(text)}\""


def wd_entity(q_or_p: str) -> str:
    return f"wd:{q_or_p}"


def wds_statement(statement_id: str) -> str:
    return "wds:" + statement_id.replace("$", "-")


def rank_term(rank: str) -> str:
    m = {
        "normal": "wikibase:NormalRank",
        "preferred": "wikibase:PreferredRank",
        "deprecated": "wikibase:DeprecatedRank",
    }
    return m.get(rank, "wikibase:NormalRank")


def datavalue_to_object(dv: dict) -> str:
    """
    Minimal mapping to RDF-ish objects.
    - wikibase-entityid -> wd:Q...
    - string -> "..."
    - monolingualtext -> "..."@lang
    - other structured types -> JSON literal string
    """
    if not isinstance(dv, dict):
        return lit_plain(str(dv))

    vtype = dv.get("type")
    value = dv.get("value")

    if vtype == "wikibase-entityid" and isinstance(value, dict):
        et = value.get("entity-type")
        nid = value.get("numeric-id")
        if et == "item" and isinstance(nid, int):
            return wd_entity(f"Q{nid}")
        if et == "property" and isinstance(nid, int):
            return wd_entity(f"P{nid}")
        return lit_plain(json.dumps(value, ensure_ascii=False, sort_keys=True))

    if vtype == "string":
        return lit_plain(str(value))

    if vtype == "monolingualtext" and isinstance(value, dict):
        return lit_lang(str(value.get("text", "")), str(value.get("language", "")) or "und")

    return lit_plain(json.dumps(value, ensure_ascii=False, sort_keys=True))


def snak_to_object(snak: dict) -> str:
    st = snak.get("snaktype")
    if st == "value":
        dv = snak.get("datavalue")
        return datavalue_to_object(dv) if isinstance(dv, dict) else lit_plain("")
    if st == "novalue":
        return "wikibase:novalue"
    if st == "somevalue":
        return "wikibase:somevalue"
    return lit_plain("")


# --------- Triple extraction per revision ---------

Triple = Tuple[str, str, str]


def extract_triples(entity: dict) -> Set[Triple]:
    out: Set[Triple] = set()

    qid = entity.get("id")
    if not isinstance(qid, str) or not qid:
        return out

    subj = wd_entity(qid)

    labels = entity.get("labels")
    if isinstance(labels, dict):
        for lang, obj in labels.items():
            if isinstance(obj, dict) and "value" in obj:
                out.add((subj, "rdfs:label", lit_lang(str(obj.get("value", "")), str(lang))))

    desc = entity.get("descriptions")
    if isinstance(desc, dict):
        for lang, obj in desc.items():
            if isinstance(obj, dict) and "value" in obj:
                out.add((subj, "schema:description", lit_lang(str(obj.get("value", "")), str(lang))))

    claims = entity.get("claims")
    if not isinstance(claims, dict):
        return out

    for pid, statements in claims.items():
        if not (isinstance(pid, str) and pid.startswith("P")):
            continue
        if not isinstance(statements, list) or not statements:
            continue

        candidates = []

        for st in statements:
            if not isinstance(st, dict):
                continue

            mainsnak = st.get("mainsnak")
            if not isinstance(mainsnak, dict):
                continue

            prop = mainsnak.get("property") if isinstance(mainsnak.get("property"), str) else pid
            snaktype = mainsnak.get("snaktype")
            main_obj = snak_to_object(mainsnak)

            stid = st.get("id")
            if not isinstance(stid, str) or not stid:
                stid = f"{qid}$NOID-{prop}-{mainsnak.get('hash','')}"
            stmt_node = wds_statement(stid)

            out.add((subj, f"p:{prop}", stmt_node))
            out.add((stmt_node, f"ps:{prop}", main_obj))
            rank = st.get("rank") if isinstance(st.get("rank"), str) else "normal"
            out.add((stmt_node, "wikibase:rank", rank_term(rank)))

            qualifiers = st.get("qualifiers")
            if isinstance(qualifiers, dict):
                for qpid, qsnaks in qualifiers.items():
                    if not (isinstance(qpid, str) and qpid.startswith("P")):
                        continue
                    if not isinstance(qsnaks, list):
                        continue
                    for qsnak in qsnaks:
                        if isinstance(qsnak, dict):
                            out.add((stmt_node, f"pq:{qpid}", snak_to_object(qsnak)))

            candidates.append((rank, snaktype, main_obj))

        non_depr = [c for c in candidates if c[0] != "deprecated"]
        non_depr_value = [c for c in non_depr if c[1] == "value"]

        if not non_depr_value:
            continue

        has_preferred = any(rank == "preferred" for rank, _, _ in non_depr_value)
        truthy = [c for c in non_depr_value if c[0] == "preferred"] if has_preferred else [
            c for c in non_depr_value if c[0] == "normal"
        ]

        for _, _, obj in truthy:
            out.add((subj, f"wdt:{pid}", obj))

    return out


# --------- Delta tracking / output ---------

@dataclass
class ActiveInfo:
    entity_id: str
    page_id: str
    cdate: str
    crevid: str
    cparentid: str
    cuser: str
    source_shard: str
    schema_version: str
    quality_flags: Set[str] = field(default_factory=set)


def deletion_flags(meta: RevisionMeta) -> Set[str]:
    flags: Set[str] = set()
    if not meta.revision_id:
        flags.add("missing_deletion_revision_id")
    if not meta.timestamp:
        flags.add("missing_deletion_timestamp")
    if not meta.contributor:
        flags.add("missing_deletion_contributor")
    return flags


def serialize_quality_flags(flags: Set[str]) -> str:
    return ";".join(sorted(f for f in flags if f))


def lifecycle_row(
    output_schema: str,
    info: ActiveInfo,
    triple: Triple,
    ddate: str,
    drevid: str,
    dparentid: str,
    duser: str,
    flags: Set[str],
) -> list:
    s, p, o = triple
    legacy = [s, p, o, info.cdate, info.cuser, ddate, duser]
    if output_schema == "legacy":
        return legacy
    return legacy + [
        info.entity_id,
        info.page_id,
        info.crevid,
        info.cparentid,
        drevid,
        dparentid,
        info.source_shard,
        info.schema_version,
        serialize_quality_flags(info.quality_flags | flags),
    ]


def write_lifecycle_row(
    writer: csv.writer,
    metrics: dict,
    output_schema: str,
    info: ActiveInfo,
    triple: Triple,
    ddate: str,
    drevid: str,
    dparentid: str,
    duser: str,
    flags: Set[str],
) -> None:
    t0 = time.perf_counter_ns()
    writer.writerow(lifecycle_row(output_schema, info, triple, ddate, drevid, dparentid, duser, flags))
    metrics["csv_writing_seconds"] += (time.perf_counter_ns() - t0) / 1_000_000_000
    metrics["output_data_rows"] += 1


def write_event_row(
    writer: Optional[csv.writer],
    metrics: dict,
    entity_id: str,
    page_id_value: str,
    rev_meta: RevisionMeta,
    action: str,
    triple: Triple,
    source_shard: str,
    flags: Set[str],
) -> None:
    if writer is None:
        return
    s, p, o = triple
    t0 = time.perf_counter_ns()
    writer.writerow([
        entity_id,
        page_id_value,
        rev_meta.revision_id,
        rev_meta.parent_revision_id,
        rev_meta.timestamp,
        rev_meta.contributor,
        action,
        s,
        p,
        o,
        source_shard,
        SCHEMA_VERSION,
        serialize_quality_flags(flags),
    ])
    metrics["csv_writing_seconds"] += (time.perf_counter_ns() - t0) / 1_000_000_000
    metrics["event_output_rows"] += 1
    if action == "ADD":
        metrics["event_add_rows"] += 1
    elif action == "DELETE":
        metrics["event_delete_rows"] += 1


def title_matches_entity(page_title_value: str, entity_id: str) -> bool:
    if not page_title_value or not entity_id:
        return True
    return page_title_value == entity_id or page_title_value == f"Property:{entity_id}" or page_title_value == f"Lexeme:{entity_id}"


def process_revision_state(
    writer: csv.writer,
    event_writer: Optional[csv.writer],
    metrics: dict,
    active: Dict[Triple, ActiveInfo],
    prev_set: Set[Triple],
    cur_set: Set[Triple],
    entity_id: str,
    page_id_value: str,
    rev_meta: RevisionMeta,
    source_shard: str,
    page_quality_flags: Set[str],
    output_schema: str,
) -> Set[Triple]:
    t0 = time.perf_counter_ns()
    added = sorted(cur_set - prev_set)
    removed = sorted(prev_set - cur_set)
    metrics["delta_computation_seconds"] += (time.perf_counter_ns() - t0) / 1_000_000_000

    for t in added:
        metrics["triple_add_events"] += 1
        creation_flags = set(page_quality_flags) | set(rev_meta.flags)
        active[t] = ActiveInfo(
            entity_id=entity_id,
            page_id=page_id_value,
            cdate=rev_meta.timestamp,
            crevid=rev_meta.revision_id,
            cparentid=rev_meta.parent_revision_id,
            cuser=rev_meta.contributor,
            source_shard=source_shard,
            schema_version=SCHEMA_VERSION,
            quality_flags=creation_flags,
        )
        write_event_row(event_writer, metrics, entity_id, page_id_value, rev_meta, "ADD", t, source_shard, creation_flags)

    for t in removed:
        metrics["triple_delete_events"] += 1
        row_flags = set(page_quality_flags) | deletion_flags(rev_meta)
        info = active.get(t) or ActiveInfo(
            entity_id=entity_id,
            page_id=page_id_value,
            cdate="",
            crevid="",
            cparentid="",
            cuser="",
            source_shard=source_shard,
            schema_version=SCHEMA_VERSION,
            quality_flags=set(page_quality_flags),
        )
        write_event_row(event_writer, metrics, entity_id, page_id_value, rev_meta, "DELETE", t, source_shard, row_flags)
        write_lifecycle_row(
            writer,
            metrics,
            output_schema,
            info,
            t,
            rev_meta.timestamp,
            rev_meta.revision_id,
            rev_meta.parent_revision_id,
            rev_meta.contributor,
            row_flags,
        )
        metrics["closed_lifecycle_rows"] += 1
        active.pop(t, None)

    return cur_set


def process_page(
    page_elem: ET.Element,
    writer: csv.writer,
    event_writer: Optional[csv.writer],
    metrics: dict,
    errors: ErrorLogger,
    source_shard: str,
    output_schema: str,
) -> None:
    metrics["pages_seen"] += 1
    title_value = page_title(page_elem)
    page_id_value = page_id(page_elem)
    ns_value = page_namespace(page_elem)

    if not title_value:
        metrics["pages_without_title"] += 1
        errors.log("warning", "page_without_title", "Page is missing a title.", page_id=page_id_value)
        return

    revisions = page_elem.findall(f"{MW_NS}revision")
    if ns_value not in SUPPORTED_ENTITY_NAMESPACES:
        metrics["pages_skipped_non_entity_namespace"] += 1
        metrics["revisions_skipped_non_entity_namespace"] += len(revisions)
        return

    metrics["pages_processed"] += 1
    active: Dict[Triple, ActiveInfo] = {}
    prev_set: Set[Triple] = set()
    page_quality_flags: Set[str] = set()
    current_entity_id = ""

    for rev in revisions:
        metrics["revisions_seen"] += 1
        rev_meta = extract_revision_meta(rev, metrics, errors, title_value, page_id_value)
        text_elem = rev.find(f"{MW_NS}text")

        if text_elem is None:
            metrics["revisions_missing_text"] += 1
            page_quality_flags.add("page_has_revision_gap")
            errors.log(
                "warning",
                "missing_revision_text",
                "Revision is missing a <text> element.",
                page_title=title_value,
                page_id=page_id_value,
                revision_id=rev_meta.revision_id,
                parent_revision_id=rev_meta.parent_revision_id,
                timestamp=rev_meta.timestamp,
                contributor=rev_meta.contributor,
            )
            continue

        if text_elem.text is None or not text_elem.text.strip():
            metrics["revisions_with_empty_text"] += 1
            page_quality_flags.add("page_has_revision_gap")
            errors.log(
                "warning",
                "empty_revision_text",
                "Revision text is empty.",
                page_title=title_value,
                page_id=page_id_value,
                revision_id=rev_meta.revision_id,
                parent_revision_id=rev_meta.parent_revision_id,
                timestamp=rev_meta.timestamp,
                contributor=rev_meta.contributor,
            )
            continue

        t0 = time.perf_counter_ns()
        entity_obj, ok = parse_entity_json_from_text(text_elem.text)
        metrics["json_parsing_seconds"] += (time.perf_counter_ns() - t0) / 1_000_000_000

        if not ok:
            metrics["revisions_with_invalid_json"] += 1
            page_quality_flags.add("page_has_revision_gap")
            errors.log(
                "warning",
                "invalid_json",
                "Revision text could not be decoded as JSON.",
                page_title=title_value,
                page_id=page_id_value,
                revision_id=rev_meta.revision_id,
                parent_revision_id=rev_meta.parent_revision_id,
                timestamp=rev_meta.timestamp,
                contributor=rev_meta.contributor,
            )
            continue

        classification = classify_revision_json(entity_obj)
        if classification == "non_object":
            metrics["revisions_with_non_object_json"] += 1
            page_quality_flags.add("page_has_revision_gap")
            errors.log(
                "warning",
                "non_object_json",
                f"Revision JSON is {type(entity_obj).__name__}, expected object.",
                page_title=title_value,
                page_id=page_id_value,
                revision_id=rev_meta.revision_id,
                parent_revision_id=rev_meta.parent_revision_id,
                timestamp=rev_meta.timestamp,
                contributor=rev_meta.contributor,
            )
            continue

        if classification == "unknown_object":
            metrics["revisions_with_unknown_json_structure"] += 1
            page_quality_flags.add("page_has_revision_gap")
            errors.log(
                "warning",
                "unknown_entity_json_structure",
                "Revision JSON is a dictionary but is neither an entity nor a redirect.",
                page_title=title_value,
                page_id=page_id_value,
                revision_id=rev_meta.revision_id,
                parent_revision_id=rev_meta.parent_revision_id,
                timestamp=rev_meta.timestamp,
                contributor=rev_meta.contributor,
                json_keys=entity_obj.keys() if isinstance(entity_obj, dict) else [],
            )
            continue

        if classification == "redirect":
            metrics["redirect_revisions"] += 1
            assert isinstance(entity_obj, dict)
            entity_id = str(entity_obj["entity"])
            current_entity_id = entity_id
            if entity_id != title_value:
                metrics["redirect_entity_mismatches"] += 1
                errors.log(
                    "warning",
                    "redirect_entity_mismatch",
                    "Redirect source does not match the page title.",
                    page_title=title_value,
                    page_id=page_id_value,
                    entity_id=entity_id,
                    revision_id=rev_meta.revision_id,
                    parent_revision_id=rev_meta.parent_revision_id,
                    timestamp=rev_meta.timestamp,
                    contributor=rev_meta.contributor,
                )
            prev_set = process_revision_state(
                writer,
                event_writer,
                metrics,
                active,
                prev_set,
                set(),
                entity_id,
                page_id_value,
                rev_meta,
                source_shard,
                page_quality_flags,
                output_schema,
            )
            continue

        assert isinstance(entity_obj, dict)
        metrics["revisions_with_valid_entity_json"] += 1
        entity_id = str(entity_obj["id"])
        current_entity_id = entity_id
        if not title_matches_entity(title_value, entity_id):
            metrics["entity_id_mismatches"] += 1
            page_quality_flags.add("entity_id_mismatch")
            errors.log(
                "warning",
                "entity_id_mismatch",
                "Entity JSON id does not match the page title.",
                page_title=title_value,
                page_id=page_id_value,
                entity_id=entity_id,
                revision_id=rev_meta.revision_id,
                parent_revision_id=rev_meta.parent_revision_id,
                timestamp=rev_meta.timestamp,
                contributor=rev_meta.contributor,
            )

        try:
            t0 = time.perf_counter_ns()
            cur_set = extract_triples(entity_obj)
            metrics["triple_extraction_seconds"] += (time.perf_counter_ns() - t0) / 1_000_000_000
        except Exception as e:
            errors.log(
                "error",
                "triple_extraction_exception",
                f"extract_triples failed: {e!r}",
                page_title=title_value,
                page_id=page_id_value,
                entity_id=entity_id,
                revision_id=rev_meta.revision_id,
                parent_revision_id=rev_meta.parent_revision_id,
                timestamp=rev_meta.timestamp,
                contributor=rev_meta.contributor,
            )
            raise

        metrics["triples_extracted_total"] += len(cur_set)
        prev_set = process_revision_state(
            writer,
            event_writer,
            metrics,
            active,
            prev_set,
            cur_set,
            entity_id,
            page_id_value,
            rev_meta,
            source_shard,
            page_quality_flags,
            output_schema,
        )

    for t in sorted(active):
        write_lifecycle_row(writer, metrics, output_schema, active[t], t, "", "", "", "", set(page_quality_flags))
        metrics["open_lifecycle_rows"] += 1

    if not current_entity_id:
        current_entity_id = title_value


def iter_pages(xml_file: io.BufferedReader, metrics: dict) -> Iterable[ET.Element]:
    t0 = time.perf_counter_ns()
    context = ET.iterparse(xml_file, events=("end",))
    metrics["xml_iteration_seconds"] += (time.perf_counter_ns() - t0) / 1_000_000_000
    for _, elem in context:
        if elem.tag == f"{MW_NS}page":
            yield elem
            t0 = time.perf_counter_ns()
            elem.clear()
            metrics["xml_iteration_seconds"] += (time.perf_counter_ns() - t0) / 1_000_000_000


# --------- Main ---------

def run(args: argparse.Namespace, metrics: dict, errors: ErrorLogger) -> None:
    delimiter = "\t" if args.tsv or args.output.endswith(".tsv") else ","
    source_shard = os.path.basename(args.input)
    header = EXTENDED_HEADER if args.output_schema == "extended" else LEGACY_HEADER

    with AtomicTextWriter(args.events_output if args.emit_events else None, newline="") as events_handle:
        event_writer = csv.writer(events_handle, delimiter=delimiter, lineterminator="\n") if events_handle else None
        if event_writer is not None:
            event_writer.writerow(EVENT_HEADER)

        with open_maybe_compressed(args.input) as f_in, open(args.output, "w", encoding="utf-8", newline="") as f_out:
            w = csv.writer(f_out, delimiter=delimiter, lineterminator="\n")
            t0 = time.perf_counter_ns()
            w.writerow(header)
            metrics["csv_writing_seconds"] += (time.perf_counter_ns() - t0) / 1_000_000_000

            n = 0
            for page in iter_pages(f_in, metrics):
                process_page(page, w, event_writer, metrics, errors, source_shard, args.output_schema)
                n += 1
                if args.limit_pages and n >= args.limit_pages:
                    break


def validate_event_args(args: argparse.Namespace) -> None:
    if args.emit_events and not args.events_output:
        raise SystemExit("--emit-events requires --events-output PATH")
    if args.events_output and not args.emit_events:
        raise SystemExit("--events-output requires --emit-events")


def main() -> int:
    ap = argparse.ArgumentParser(description="Export RDF-ish Wikidata triples with lifetimes to CSV/TSV.")
    ap.add_argument("input", help="Input pages-meta-history XML (.xml, .gz, .bz2)")
    ap.add_argument("-o", "--output", required=True, help="Output CSV/TSV path")
    ap.add_argument("--tsv", action="store_true", help="Write TSV instead of CSV")
    ap.add_argument("--limit-pages", type=int, default=0, help="Stop after N pages (0=no limit)")
    ap.add_argument("--metrics-output", help="Output shard metrics JSON path")
    ap.add_argument("--errors-output", help="Output JSON Lines warning/error path")
    ap.add_argument("--output-schema", choices=("extended", "legacy"), default="extended", help="Lifecycle CSV schema.")
    ap.add_argument("--emit-events", action="store_true", help="Write an ADD/DELETE event stream CSV.")
    ap.add_argument("--events-output", help="Output event stream CSV path; requires --emit-events.")
    args = ap.parse_args()
    validate_event_args(args)

    started_ns = time.perf_counter_ns()
    metrics = new_metrics(args.input, args.output, args.output_schema, args.emit_events, args.events_output)
    errors = ErrorLogger(args.errors_output, metrics["input_basename"], metrics)

    try:
        run(args, metrics, errors)
    except Exception:
        metrics["completed_successfully"] = False
        raise
    finally:
        metrics["finished_at_utc"] = utc_now_iso()
        metrics["total_seconds"] = (time.perf_counter_ns() - started_ns) / 1_000_000_000
        try:
            metrics["output_bytes"] = os.path.getsize(args.output)
        except OSError:
            metrics["output_bytes"] = 0
        if args.emit_events and args.events_output:
            try:
                metrics["event_output_bytes"] = os.path.getsize(args.events_output)
            except OSError:
                metrics["event_output_bytes"] = 0
        try:
            errors.publish()
        except Exception as e:
            print(f"[ERROR] failed to publish error log: {e!r}", file=sys.stderr)
        if args.metrics_output:
            try:
                atomic_write_json(args.metrics_output, metrics)
            except Exception as e:
                print(f"[ERROR] failed to write metrics: {e!r}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
