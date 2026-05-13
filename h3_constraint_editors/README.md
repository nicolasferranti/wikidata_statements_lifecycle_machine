# H3 Constraint Editors

This folder contains scripts, notebooks, and validation utilities for the
constraint-editor analysis. The workflow extracts constraint-related rows from
the lifecycle CSVs and also prepares WDT-only files used by H2 part 2 and H3
validation steps.

## Files

| Path | Description |
| --- | --- |
| `extract_constraints.py` | Extracts rows related to Wikidata property constraints by following `p:P2302` statement links. |
| `run_extract_constraints.sh` | Runs constraint extraction over a folder of lifecycle CSVs in parallel. |
| `extract_wdt_triples.py` | Extracts rows whose predicate starts with `wdt:` from one lifecycle CSV. |
| `extract_wdt_folder.sh` | Runs WDT extraction over a folder of lifecycle CSVs in parallel. |
| `h3_historical_violations_finder.ipynb` | Notebook for finding and inspecting historical constraint violations. |
| `h3_tester-final.ipynb` | Notebook for final H3 testing and inspection. |
| `wikidata_bots.json` | Bot account list used to filter or flag bot edits during analysis. |
| `h3_experiment_find_tanon_validity_intersection/` | Tanon correction enrichment and historical validation scripts, with its own dataset README. |

## Expected Input

The extraction scripts start from lifecycle CSV files with the root schema:

```text
s,p,o,cdate,cuser,ddate,duser
```

These files are normally produced by `../wd_history_rdf_csv.py` and stored in
`../plain_edit_history_triples/`.

## Extract Constraint-related Rows

For one file:

```bash
python3 extract_constraints.py \
  ../plain_edit_history_triples/example.csv \
  --output output/constraints_only
```

For a full folder:

```bash
./run_extract_constraints.sh \
  ../plain_edit_history_triples \
  output/constraints_only \
  ./extract_constraints.py \
  8
```

The script first collects statement IDs from rows where `p == p:P2302`, then
writes those rows plus all rows whose subject is one of the collected statement
IDs.

## Extract WDT-only Rows

For one file:

```bash
python3 extract_wdt_triples.py \
  --input ../plain_edit_history_triples/example.csv \
  --output output/wdt_folder/example.csv
```

For a full folder:

```bash
./extract_wdt_folder.sh \
  ../plain_edit_history_triples \
  output/wdt_folder \
  8 \
  ./extract_wdt_triples.py
```

The WDT-only outputs preserve the lifecycle columns and keep only predicates
whose value starts with `wdt:`.

## Tanon Validation Experiment

The `h3_experiment_find_tanon_validity_intersection/` subfolder contains
scripts for enriching Tanon correction files with historical lifecycle metadata
and validating several constraint types historically, including one-of,
inverse, symmetric, conflicts-with, item-requires-statement, and
value-requires-statement constraints.
