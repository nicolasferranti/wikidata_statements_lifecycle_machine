#!/usr/bin/env python3
"""
Wikidata pages-meta-history XML -> CSV of triple lifetimes

Output columns:
  s, p, o, cdate, cuser, ddate, duser

Triples (s,p,o) are RDF-ish CURIE strings, including:
- Labels:            wd:Q  rdfs:label         "..."@lang
- Descriptions:      wd:Q  schema:description "..."@lang
- Direct claims:     wd:Q  wdt:Pxx            <value>
- Statement graph:   wd:Q  p:Pxx              wds:Q-GUID
                     wds:  ps:Pxx             <value>
                     wds:  wikibase:rank      wikibase:NormalRank|PreferredRank|DeprecatedRank
                     wds:  pq:Pyy             <qualifierValue>

Notes:
- Streaming parse (low memory), supports .xml, .xml.gz, .xml.bz2
- Tracks presence/absence of triples across revisions to set creation/deletion metadata.
- Multiplicity: RDF allows duplicate identical triples, but CSV set logic tracks presence/absence only.
"""

from __future__ import annotations

import argparse
import bz2
import csv
import gzip
import html
import io
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Set, Tuple

MW_NS = "{http://www.mediawiki.org/xml/export-0.11/}"

# --------- IO ---------

def open_maybe_compressed(path: str) -> io.BufferedReader:
    if path.endswith(".bz2"):
        return bz2.open(path, "rb")
    if path.endswith(".gz"):
        return gzip.open(path, "rb")
    return open(path, "rb")

# --------- Revision metadata ---------

def revision_timestamp(rev_elem: ET.Element) -> str:
    ts = rev_elem.find(f"{MW_NS}timestamp")
    return ts.text.strip() if ts is not None and ts.text else ""

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

# --------- Decode JSON from <text> ---------

def parse_entity_json_from_text(text: Optional[str]) -> Optional[dict]:
    if not text:
        return None
    raw = html.unescape(text).strip()   # &quot; -> "
    if not raw:
        return None
    try:
        return json.loads(raw)          # decodes \uXXXX
    except json.JSONDecodeError:
        return None

# --------- RDF-ish term formatting ---------

def escape_literal(s: str) -> str:
    # Turtle/N-Triples-ish string escaping
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")

def lit_lang(text: str, lang: str) -> str:
    return f"\"{escape_literal(text)}\"@{lang}"

def lit_plain(text: str) -> str:
    return f"\"{escape_literal(text)}\""

def wd_entity(q_or_p: str) -> str:
    # expects "Q123" or "P123"
    return f"wd:{q_or_p}"

def wds_statement(statement_id: str) -> str:
    # JSON statement id: "Q6249108$D213..."
    # RDF export uses:    wds:Q6249108-D213...
    return "wds:" + statement_id.replace("$", "-")

def rank_term(rank: str) -> str:
    # JSON rank: normal|preferred|deprecated
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
        # fallback
        return lit_plain(json.dumps(value, ensure_ascii=False, sort_keys=True))

    if vtype == "string":
        return lit_plain(str(value))

    if vtype == "monolingualtext" and isinstance(value, dict):
        return lit_lang(str(value.get("text", "")), str(value.get("language", "")) or "und")

    # You can add richer RDF modeling for time/quantity/globecoordinate here if you want.
    # For now we keep a stable literal representation.
    return lit_plain(json.dumps(value, ensure_ascii=False, sort_keys=True))

def snak_to_object(snak: dict) -> str:
    """
    snaktype:
      - value: datavalue -> object
      - novalue/somevalue: encode as wikibase:novalue / wikibase:somevalue (RDF-ish)
    """
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

Triple = Tuple[str, str, str]  # (s, p, o)

def extract_triples(entity: dict) -> Set[Triple]:
    out: Set[Triple] = set()

    qid = entity.get("id")
    if not isinstance(qid, str) or not qid:
        return out

    subj = wd_entity(qid)

    # labels
    labels = entity.get("labels")
    if isinstance(labels, dict):
        for lang, obj in labels.items():
            if isinstance(obj, dict) and "value" in obj:
                out.add((subj, "rdfs:label", lit_lang(str(obj.get("value", "")), str(lang))))

    # descriptions
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

        # ---- 1) Always emit the full statement graph triples (p:, ps:, pq:, rank) ----
        # While we iterate, collect candidates for truthy wdt: selection.
        candidates = []  # list of tuples: (rank, mainsnak_obj, mainsnak_snaktype)

        for st in statements:
            if not isinstance(st, dict):
                continue

            mainsnak = st.get("mainsnak")
            if not isinstance(mainsnak, dict):
                continue

            prop = mainsnak.get("property") if isinstance(mainsnak.get("property"), str) else pid
            snaktype = mainsnak.get("snaktype")
            main_obj = snak_to_object(mainsnak)

            # statement node id
            stid = st.get("id")
            if not isinstance(stid, str) or not stid:
                stid = f"{qid}$NOID-{prop}-{mainsnak.get('hash','')}"
            stmt_node = wds_statement(stid)

            # entity -> statement
            out.add((subj, f"p:{prop}", stmt_node))
            # statement value
            out.add((stmt_node, f"ps:{prop}", main_obj))
            # rank triple
            rank = st.get("rank") if isinstance(st.get("rank"), str) else "normal"
            out.add((stmt_node, "wikibase:rank", rank_term(rank)))

            # qualifiers
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

            # collect for truthy direct (wdt:)
            candidates.append((rank, snaktype, main_obj))

        # ---- 2) Emit wdt: truthy triples ONLY ----
        # Filter out deprecated
        non_depr = [c for c in candidates if c[0] != "deprecated"]

        # Only value snaks should become wdt triples (omit somevalue/novalue)
        non_depr_value = [c for c in non_depr if c[1] == "value"]

        if not non_depr_value:
            continue

        has_preferred = any(rank == "preferred" for rank, _, _ in non_depr_value)

        if has_preferred:
            truthy = [c for c in non_depr_value if c[0] == "preferred"]
        else:
            truthy = [c for c in non_depr_value if c[0] == "normal"]

        for _, _, obj in truthy:
            out.add((subj, f"wdt:{pid}", obj))

    return out

# --------- Delta tracking / output ---------

@dataclass
class ActiveInfo:
    cdate: str
    cuser: str

def write_row(w: csv.writer, s: str, p: str, o: str, cdate: str, cuser: str, ddate: str, duser: str) -> None:
    w.writerow([s, p, o, cdate, cuser, ddate, duser])

def process_page(page_elem: ET.Element, writer: csv.writer) -> None:
    title_elem = page_elem.find(f"{MW_NS}title")
    if title_elem is None or not title_elem.text:
        return
    page_title = title_elem.text.strip()

    active: Dict[Triple, ActiveInfo] = {}
    prev_set: Set[Triple] = set()

    for rev in page_elem.findall(f"{MW_NS}revision"):
        ts = revision_timestamp(rev)
        user = contributor_to_user(rev)

        text_elem = rev.find(f"{MW_NS}text")
        entity = parse_entity_json_from_text(text_elem.text if text_elem is not None else None)
        if entity is None:
            continue
        if not isinstance(entity, dict):
            # Debug: identify bad JSON payloads (common cause of crashes / empty outputs)
            import sys
            print(f"[WARN] non-dict entity JSON: type={type(entity).__name__} page={page_title} ts={ts} user={user}", file=sys.stderr)
            continue

        try:
            cur_set = extract_triples(entity)
        except Exception as e:
            import sys
            print(f"[ERROR] extract_triples failed: page={page_title} ts={ts} user={user} err={e!r}", file=sys.stderr)
            raise

        # additions
        for t in (cur_set - prev_set):
            active[t] = ActiveInfo(cdate=ts, cuser=user)

        # removals
        for t in (prev_set - cur_set):
            info = active.get(t) or ActiveInfo(cdate="", cuser="")
            s, p, o = t
            write_row(writer, s, p, o, info.cdate, info.cuser, ts, user)
            active.pop(t, None)

        prev_set = cur_set

    # still active at end of file slice
    for (s, p, o), info in active.items():
        write_row(writer, s, p, o, info.cdate, info.cuser, "", "")

def iter_pages(xml_file: io.BufferedReader) -> Iterable[ET.Element]:
    context = ET.iterparse(xml_file, events=("end",))
    for _, elem in context:
        if elem.tag == f"{MW_NS}page":
            yield elem
            elem.clear()

# --------- Main ---------

def main() -> int:
    ap = argparse.ArgumentParser(description="Export RDF-ish Wikidata triples with lifetimes to CSV.")
    ap.add_argument("input", help="Input pages-meta-history XML (.xml, .gz, .bz2)")
    ap.add_argument("-o", "--output", required=True, help="Output CSV/TSV path")
    ap.add_argument("--tsv", action="store_true", help="Write TSV instead of CSV")
    ap.add_argument("--limit-pages", type=int, default=0, help="Stop after N pages (0=no limit)")
    args = ap.parse_args()

    delimiter = "\t" if args.tsv or args.output.endswith(".tsv") else ","

    with open_maybe_compressed(args.input) as f_in, open(args.output, "w", encoding="utf-8", newline="") as f_out:
        w = csv.writer(f_out, delimiter=delimiter, lineterminator="\n")
        w.writerow(["s", "p", "o", "cdate", "cuser", "ddate", "duser"])

        n = 0
        for page in iter_pages(f_in):
            process_page(page, w)
            n += 1
            if args.limit_pages and n >= args.limit_pages:
                break

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
