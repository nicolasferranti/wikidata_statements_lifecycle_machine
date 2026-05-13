# H2 Class Specialists, Part 1

This folder builds the first set of class-specialist intermediate tables from
the root lifecycle CSV files. It focuses on `wdt:P31` class membership events
and links user edits to class intervals.

## Files

| Path | Description |
| --- | --- |
| `build_h2_tables.py` | Streams one lifecycle CSV and writes Table 1, Table 1b, and Table 2 outputs. |
| `run_h2_tables.sh` | Runs `build_h2_tables.py` over a folder of lifecycle CSVs in parallel. |
| `build_user_class_edit_counts.py` | Combines matching Table 1b and Table 2 files to count edits by `(user, class)`. |
| `run_user_class_edit_counts.sh` | Runs the user/class counter over all matching per-file table pairs. |
| `build_class_add_delete_counts.py` | Aggregates class additions from Table 1 and deletions from Table 1b. |
| `merge_table4_agg_counts.py` | Merges per-file Table 4 aggregate count files into one summed CSV. |
| `sort_table2_inplace.py` | Utility for sorting Table 2 files in place. |
| `h2_class_specialists_tester.ipynb` | Notebook for testing and inspecting the H2 part 1 workflow. |
| `wikidata_bots.json` | Bot account list used by downstream analysis. |

## Expected Input

The starting point is the root lifecycle CSV schema:

```text
s,p,o,cdate,cuser,ddate,duser
```

These files are normally produced by `../wd_history_rdf_csv.py` and stored in
`../plain_edit_history_triples/`.

## Build Per-file Tables

Run over all lifecycle CSV files in parallel:

```bash
./run_h2_tables.sh ../plain_edit_history_triples 4
```

Resume from a specific input basename:

```bash
./run_h2_tables.sh ../plain_edit_history_triples 4 20250501-triple-pages-meta-history9-p13886345p13998367.csv
```

The runner writes under `output/`:

| Output | Meaning |
| --- | --- |
| `output/table1/` | Class membership additions. |
| `output/table1b/` | Class membership deletions. |
| `output/table2/` | Edit events used for user/class counting. |
| `output/logs/` | Per-file worker logs. |
| `output/state/` | Done markers for restart-safe runs. |

## Build User/Class Edit Counts

After Table 1b and Table 2 are available:

```bash
./run_user_class_edit_counts.sh ./output 4
```

This writes per-file counts under:

```text
output/user_class_edit_counts/
```

The script expects matching stems between:

```text
output/table1b/<stem>.table1b.csv
output/table2/<stem>.table2.csv
```

## Aggregate Class Add/Delete Counts

```bash
python3 build_class_add_delete_counts.py \
  --table1-dir output/table1 \
  --table1b-dir output/table1b \
  --output output/class_add_delete_counts.csv
```

The output schema is:

```text
class,n_instances_added,n_instances_deleted
```
