# Wikidata Statements Lifecycle Machine

Utilities for extracting the lifecycle of Wikidata RDF-like statements from
`pages-meta-history` XML dumps.

The main script streams Wikidata page history dumps, reconstructs RDF-like
triples for each entity revision, and writes one row per triple lifecycle with
creation and deletion metadata.

## Repository Contents

| Path | Description |
| --- | --- |
| `wd_history_rdf_csv.py` | Converts a Wikidata `pages-meta-history` XML file into a CSV or TSV of triple lifetimes. |
| `run_wd_history_batch_parallel.sh` | Runs the converter over many Wikidata history dump parts in parallel. |
| `wikidata_pages-history/` | Example input location for compressed Wikidata history dump files. |
| `plain_edit_history_triples/` | Default output location used in the examples for generated triple lifecycle CSV files. |
| `index_entity_files/` | Helper scripts for building and looking up subject/entity indexes from Wikidata history files. |
| `h1_property_specialists/` | Scripts and notebooks for the property-specialist analysis. |
| `h2_class_specialists_part1/` | First-stage scripts for the class-specialist analysis, including user/class edit counts and table builders. |
| `h2_class_specialists_part2/` | Second-stage scripts for filtering `wdt:P31`/`wdt:P279` triples and aggregating class edit counts. |
| `h3_constraint_editors/` | Scripts, notebooks, and validation utilities for the constraint-editor analysis. |

The repository is organized around a shared extraction step followed by
analysis-specific folders. First, `wd_history_rdf_csv.py` and
`run_wd_history_batch_parallel.sh` create the lifecycle tables in
`plain_edit_history_triples/`. The `h1_*`, `h2_*`, and `h3_*` directories then
contain the downstream scripts, artifacts, and notebooks used for each hypothesis-specific
analysis.

## Requirements

- Python 3.8 or newer
- Bash, GNU `find`, `xargs`, `stat`, `du`, `wc`, and `/usr/bin/time` for the
  batch runner

The Python converter uses only the Python standard library. No additional
Python packages are required.

## Input

The converter accepts MediaWiki `pages-meta-history` XML files in any of these
formats:

- `.xml`
- `.xml.gz`
- `.xml.bz2`

Each revision is expected to contain Wikidata entity JSON in the revision
`<text>` field.

## Output Schema

The output file contains the following columns:

| Column | Meaning |
| --- | --- |
| `s` | Triple subject |
| `p` | Triple predicate |
| `o` | Triple object |
| `cdate` | Timestamp of the revision where the triple first appeared |
| `cuser` | User or IP that created the triple |
| `ddate` | Timestamp of the revision where the triple disappeared |
| `duser` | User or IP that deleted the triple |

Rows with empty `ddate` and `duser` were still active at the end of the input
file slice.

## Extracted Triples

The script emits RDF-like CURIE terms for:

- Labels: `wd:Q... rdfs:label "...@lang"`
- Descriptions: `wd:Q... schema:description "...@lang"`
- Truthy direct claims: `wd:Q... wdt:P... <value>`
- Statement graph links: `wd:Q... p:P... wds:...`
- Statement values: `wds:... ps:P... <value>`
- Statement ranks: `wds:... wikibase:rank wikibase:NormalRank`
- Qualifiers: `wds:... pq:P... <value>`

For claim truthy triples, deprecated statements are ignored. If a claim has one
or more preferred value statements, only preferred values are emitted as
`wdt:` triples; otherwise normal value statements are emitted.

## Single-file Usage

Convert one compressed dump part to CSV:

```bash
python3 wd_history_rdf_csv.py \
  wikidata_pages-history/wikidatawiki-20250501-pages-meta-history17.xml-p31710574p31710876.bz2 \
  -o triples_lifecycle.csv
```

Write TSV instead of CSV:

```bash
python3 wd_history_rdf_csv.py input.xml.bz2 -o triples_lifecycle.tsv --tsv
```

Process only the first `N` pages, which is useful for smoke tests:

```bash
python3 wd_history_rdf_csv.py input.xml.bz2 -o sample.csv --limit-pages 10
```

## Batch Usage

Run the converter over all matching dump parts in an input directory:

```bash
chmod +x run_wd_history_batch_parallel.sh

./run_wd_history_batch_parallel.sh \
  --input-dir /path/to/wikidata_pages_meta_history \
  --out-dir plain_edit_history_triples \
  --script wd_history_rdf_csv.py \
  --jobs 4
```

Run the batch job in the background and keep a log:

```bash
nohup ./run_wd_history_batch_parallel.sh \
  --input-dir /path/to/wikidata_pages_meta_history \
  --out-dir plain_edit_history_triples \
  --script wd_history_rdf_csv.py \
  --jobs 4 \
  > batch_run.log 2>&1 &
```

For a quick batch smoke test, process only the first matching input file:

```bash
./run_wd_history_batch_parallel.sh \
  --input-dir /path/to/wikidata_pages_meta_history \
  --out-dir plain_edit_history_triples \
  --script wd_history_rdf_csv.py \
  --jobs 1 \
  --first-only
```

The batch script currently looks for files named:

```text
wikidatawiki-20250501-pages-meta-history*.bz2
```

For each input file, it writes an output CSV named like:

```text
20250501-triple-pages-meta-history<PART>-p<PAGE_RANGE>.csv
```

For example, an input file ending in
`pages-meta-history17.xml-p31710574p31710876.bz2` becomes
`20250501-triple-pages-meta-history17-p31710574p31710876.csv`.

Existing non-empty output files are skipped, so interrupted batch jobs can be
restarted.

## Notes and Limitations

- Processing is streaming and memory-conscious, but full Wikidata history dumps
  are very large. Use `--limit-pages` or `--first-only` before launching a full
  batch run.
- Triple multiplicity is tracked as set membership: duplicate identical triples
  in the same revision are treated as one triple.
- Time, quantity, coordinate, and other structured datatypes are currently
  serialized as stable JSON string literals rather than fully expanded RDF
  value nodes.
- The lifecycle is computed within each input dump slice. Triples active at the
  end of a slice are written as still active, as all data of the same subject belongs to the same input slice.
