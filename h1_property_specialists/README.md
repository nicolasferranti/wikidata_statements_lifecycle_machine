# H1 Property Specialists

This folder contains the scripts and notebook used for the property-specialist
analysis. The main input is the lifecycle CSV output produced by the root
extraction pipeline, usually stored in `../plain_edit_history_triples/`.

## Files

| Path | Description |
| --- | --- |
| `count_user_property_wdt.py` | Counts creation events by `(user, property)` for lifecycle rows whose predicate is `wdt:P*`. |
| `h1_property_specialists.ipynb` | Notebook for inspecting, joining, and analyzing the property-specialist counts. |
| `property_label_en.json` | English labels for Wikidata properties used to make outputs easier to interpret. |
| `wikidata_bots.json` | Bot account list used to filter or flag bot edits during analysis. |

## Input

`count_user_property_wdt.py` expects CSV files with the root lifecycle schema:

```text
s,p,o,cdate,cuser,ddate,duser
```

Only rows where `p` matches `wdt:P...` and `cuser` is non-empty are counted.

## Usage

```bash
python3 count_user_property_wdt.py \
  --input-dir ../plain_edit_history_triples \
  --pattern '*.csv' \
  --output-counts output/user_property_counts.csv \
  --checkpoint output/user_property_counts.checkpoint.json \
  --checkpoint-counts-csv output/user_property_counts.checkpoint.csv \
  --checkpoint-every 20 \
  --error-log output/user_property_counts.errors.csv \
  --progress-log output/user_property_counts.progress.log
```

The checkpoint files allow long runs to be resumed without recounting completed
input files.

## Output

The main count file has this schema:

```text
user,property,count
```

The notebook can then use these counts together with `property_label_en.json`
and `wikidata_bots.json` for the final H1 analysis.
