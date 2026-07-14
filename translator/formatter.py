#!/usr/bin/env python3
"""
Strict 1:1 join:
- Zips data/benchmark/dail/testCopy_flat.json with translator/out/output.json by position
- Writes translator/out/formatted_results.json with:
  count, db_id, nlq, SQL, SQL_pred, MQL, MQL_pred
"""

import argparse
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent      # translator/
REPO_ROOT = SCRIPT_DIR.parent                     # repo root

DEFAULT_TESTCOPY_FLAT_PATH = REPO_ROOT / "data" / "benchmark" / "dail" / "testCopy_flat.json"
DEFAULT_OUTPUT_PATH = SCRIPT_DIR / "out" / "output.json"
DEFAULT_OUT_PATH = SCRIPT_DIR / "out" / "formatted_results.json"


def load_json_array(p: Path):
    with p.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise SystemExit(f"ERROR: {p} does not contain a top-level JSON array.")
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", default=str(DEFAULT_TESTCOPY_FLAT_PATH),
                        help="Gold/reference JSON array with db_id, nlq, SQL, MQL.")
    parser.add_argument("--pred", default=str(DEFAULT_OUTPUT_PATH),
                        help="Prediction JSON array from collect_mql_preds.py.")
    parser.add_argument("--out", default=str(DEFAULT_OUT_PATH),
                        help="Formatted output JSON path.")
    args = parser.parse_args()

    gold_path = Path(args.gold)
    pred_path = Path(args.pred)
    out_path = Path(args.out)

    gold_rows = load_json_array(gold_path)
    pred_rows = load_json_array(pred_path)

    if len(gold_rows) != len(pred_rows):
        raise SystemExit(
            f"ERROR: Requires same length for both test file and prediction output. "
            f"Gold={len(gold_rows)} vs Preds={len(pred_rows)}."
        )

    combined = []
    for idx, (g, p) in enumerate(zip(gold_rows, pred_rows), start=1):
        db_id = str(g.get("db_id", "")).strip()
        nlq = g.get("nlq", "")
        sql_gold = g.get("SQL", "")
        mql_gold = g.get("MQL", "")

        sql_pred = p.get("sql", "") or ""
        mql_pred = p.get("mongodb", "") or ""

        combined.append({
            "count": idx,
            "db_id": db_id,
            "nlq": nlq,
            "SQL": sql_gold,
            "SQL_pred": sql_pred,
            "MQL": mql_gold,
            "MQL_pred": mql_pred
        })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(combined, f, ensure_ascii=False, indent=2)

    print(f"Wrote {len(combined)} rows to {out_path.resolve()}")


if __name__ == "__main__":
    main()
