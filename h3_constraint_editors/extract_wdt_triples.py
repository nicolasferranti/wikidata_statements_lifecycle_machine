#!/usr/bin/env python3

import argparse
import csv
import os
import sys
import tempfile


def normalize_header(name: str) -> str:
    return (name or "").strip().lower()


def process_file(input_path: str, output_path: str) -> int:
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        newline="",
        encoding="utf-8",
        dir=os.path.dirname(output_path) or ".",
        prefix=".tmp.",
        suffix=".csv",
        delete=False,
    )

    input_rows = 0
    output_rows = 0

    try:
        with open(input_path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)

            if reader.fieldnames is None:
                raise ValueError(f"No header row found in {input_path}")

            normalized = [normalize_header(x) for x in reader.fieldnames]
            original_to_normalized = dict(zip(reader.fieldnames, normalized))

            required = {"s", "p", "o", "cdate", "cuser", "ddate", "duser"}
            if not required.issubset(set(normalized)):
                raise ValueError(
                    f"Missing required columns. Found={normalized}, required={sorted(required)}"
                )

            writer = csv.DictWriter(
                tmp,
                fieldnames=["s", "p", "o", "cdate", "cuser", "ddate", "duser"],
            )
            writer.writeheader()

            for raw_row in reader:
                input_rows += 1

                row = {
                    original_to_normalized[k]: (v if v is not None else "")
                    for k, v in raw_row.items()
                }

                p = (row.get("p", "") or "").strip()

                if p.startswith("wdt:"):
                    writer.writerow({
                        "s": (row.get("s", "") or "").strip(),
                        "p": p,
                        "o": (row.get("o", "") or "").strip(),
                        "cdate": (row.get("cdate", "") or "").strip(),
                        "cuser": (row.get("cuser", "") or "").strip(),
                        "ddate": (row.get("ddate", "") or "").strip(),
                        "duser": (row.get("duser", "") or "").strip(),
                    })
                    output_rows += 1

        tmp.flush()
        tmp.close()
        os.replace(tmp.name, output_path)

        print(f"[OK] {input_path} | input_rows={input_rows} | wdt_rows={output_rows}")
        return 0

    except Exception as exc:
        try:
            tmp.close()
        except Exception:
            pass

        if os.path.exists(tmp.name):
            os.remove(tmp.name)

        print(f"[ERROR] {input_path}: {exc}", file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(
        description="Extract rows where p starts with wdt:, preserving full historical row."
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)

    args = parser.parse_args()

    if not os.path.isfile(args.input):
        print(f"[ERROR] Input file does not exist: {args.input}", file=sys.stderr)
        sys.exit(1)

    sys.exit(process_file(args.input, args.output))


if __name__ == "__main__":
    main()
