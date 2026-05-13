#!/usr/bin/env bash
set -euo pipefail

subject="$1"
index_dir="${2:-wd_subject_lookup_100}"

awk -F',' -v s="$subject" '
  NR > 1 && $1 <= s && s <= $2 {
    gsub(/\r/, "", $4)
    print $4
  }
' "$index_dir/manifest.csv" |
while read -r part; do
  LC_ALL=C grep -m 1 $'^'"$subject"$'\t' "$part" || true
done | cut -f2
