#!/usr/bin/env python3
"""
Re-extract MongoDB predictions from saved llm_raw_output without re-calling an LLM.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mql_extractor import extract_first_mongo_query


def load_jsonl(path: Path):
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--raw-field", default="llm_raw_output")
    args = parser.parse_args()

    rows = list(load_jsonl(args.input))

    changed = 0
    empty_before = 0
    empty_after = 0

    args.output.parent.mkdir(parents=True, exist_ok=True)

    with args.output.open("w", encoding="utf-8") as f:
        for row in rows:
            old_pred = str(row.get("MQL_pred", "") or "").strip()
            raw = str(row.get(args.raw_field, "") or "")
            new_pred = extract_first_mongo_query(raw)

            if not old_pred:
                empty_before += 1

            if not new_pred:
                empty_after += 1

            if new_pred and new_pred != old_pred:
                changed += 1
                row["MQL_pred_old"] = old_pred
                row["prediction_query_old"] = row.get("prediction_query", old_pred)
                row["MQL_pred"] = new_pred
                row["prediction_query"] = new_pred
                row["reextracted"] = True
                row["extractor_found_db"] = "db." in raw
                row["extractor_balanced"] = bool(new_pred)
                row["extractor_raw_length"] = len(raw)
            else:
                row["reextracted"] = False

            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"rows: {len(rows)}")
    print(f"changed: {changed}")
    print(f"empty_before: {empty_before}")
    print(f"empty_after: {empty_after}")
    print(f"wrote: {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())