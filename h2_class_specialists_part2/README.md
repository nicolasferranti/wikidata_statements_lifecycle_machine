# H2 Class Specialists, Part 2

This folder contains the second class-specialist workflow. It filters WDT rows,
extracts `P31` and `P279` subsets, counts edits inside valid class intervals,
and aggregates the resulting per-class edit count JSON files.

## Files

| Path | Description |
| --- | --- |
| `filter_wdt_p31_p279.py` | Splits one WDT lifecycle CSV into separate `wdt:P31` and `wdt:P279` CSV files. |
| `run_filter_wdt_parallel.sh` | Runs the P31/P279 filter over a folder in parallel. |
| `count_class_editors_one_file.py` | Counts non-P31/non-P279 user edits by active subject `P31` class intervals for one file pair. |
| `run_class_edit_counts_parallel.sh` | Runs class edit counting over all matching P31 and WDT file pairs. |
| `aggregate_class_edit_counts.py` | Aggregates per-file `*-class-edit-counts.json` files by class. |

## Expected Input

This workflow expects WDT-only lifecycle CSV files with the root schema:

```text
s,p,o,cdate,cuser,ddate,duser
```

WDT-only files can be produced from full lifecycle CSVs with
`../h3_constraint_editors/extract_wdt_triples.py` and
`../h3_constraint_editors/extract_wdt_folder.sh`.

## Split P31 and P279 Rows

```bash
./run_filter_wdt_parallel.sh \
  8 \
  /path/to/wdt_folder \
  /path/to/wdt_P279 \
  /path/to/wdt_P31
```

Each input file produces:

```text
<stem>-P279.csv
<stem>-P31.csv
```

Existing output pairs are skipped unless the single-file script is run with
`--overwrite`.

## Count Edits by Class

```bash
./run_class_edit_counts_parallel.sh \
  8 \
  /path/to/wdt_P31 \
  /path/to/wdt_folder \
  /path/to/class_edit_counts
```

For every `<stem>-P31.csv`, the runner expects the matching full WDT file:

```text
/path/to/wdt_folder/<stem>.csv
```

The per-file output is JSON and is skipped when it already exists.

## Aggregate JSON Outputs

```bash
python3 aggregate_class_edit_counts.py \
  /path/to/class_edit_counts \
  /path/to/class_edit_counts_aggregated.json \
  --mode concat
```

Use `--mode merge-identical` to sum identical class/user/time rows instead of
concatenating them.
