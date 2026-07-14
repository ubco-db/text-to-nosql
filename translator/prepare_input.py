#!/usr/bin/env python3
"""
Prepare canonical SQL-to-MongoDB pipeline input.

This script converts a benchmark file plus SQL predictions into the canonical
record format used by the translator/evaluation pipeline.

It supports two benchmark shapes:

1. Compact TEND-style benchmark:
   [
     {
       "record_id": 1861,
       "db_id": "school_bus",
       "nl_queries": ["...", "..."],
       "ref_sql": "SELECT ...",
       "MQL": "db...."
     }
   ]

2. Already-flattened benchmark:
   [
     {
       "record_id": "1861_1",
       "db_id": "school_bus",
       "nlq": "...",
       "SQL": "SELECT ...",
       "MQL": "db...."
     }
   ]

Prediction input can be:
  - .txt: one SQL prediction per line
  - .json: top-level JSON array of strings or objects
  - .jsonl: one JSON object/string per line

Output:
  [
    {
      "count": 1,
      "record_id": "1861_1",
      "db_id": "school_bus",
      "nlq": "...",
      "SQL": "gold SQL",
      "SQL_pred": "predicted SQL",
      "MQL": "gold MongoDB query",
      "MQL_pred": ""
    }
  ]

Example:
  python translator/prepare_sql_input.py ^
    --benchmark data/benchmark/tend/test.json ^
    --predictions data/benchmark/dail/predictions/DAILresults.txt ^
    --out translator/out/input.json

For translating gold SQL instead of predicted SQL:
  python translator/prepare_sql_input.py ^
    --benchmark data/benchmark/tend/test.json ^
    --sql-source gold ^
    --out translator/out/input_gold_sql.json
"""

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


PREDICTION_FIELD_CANDIDATES = (
    "SQL_pred",
    "sql_pred",
    "prediction",
    "pred",
    "sql",
    "query",
    "text",
)


def load_json_array(path: Path) -> List[Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise RuntimeError(f"{path} must contain a top-level JSON array.")

    return data


def get_required_string(row: Dict[str, Any], keys: Iterable[str], row_num: int, source: Path) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return str(value)

    key_list = ", ".join(keys)
    raise RuntimeError(f"Missing one of [{key_list}] in row {row_num} of {source}.")


def flatten_benchmark_rows(path: Path) -> List[Dict[str, Any]]:
    """Load compact or flattened benchmark rows and return one row per NLQ."""
    records = load_json_array(path)
    if not records:
        return []

    first = records[0]
    if not isinstance(first, dict):
        raise RuntimeError(f"Benchmark rows in {path} must be JSON objects.")

    flattened: List[Dict[str, Any]] = []

    # Compact TEND-style input: one row has multiple NLQs.
    if "nl_queries" in first:
        for row_num, rec in enumerate(records, start=1):
            if not isinstance(rec, dict):
                raise RuntimeError(f"Benchmark row {row_num} in {path} is not an object.")

            base_record_id = get_required_string(rec, ("record_id",), row_num, path)
            db_id = get_required_string(rec, ("db_id",), row_num, path)
            sql_gold = get_required_string(rec, ("ref_sql", "SQL", "query"), row_num, path)
            mql_gold = str(rec.get("MQL", "") or "")
            nl_queries = rec.get("nl_queries")

            if not isinstance(nl_queries, list):
                raise RuntimeError(f"'nl_queries' must be a list in row {row_num} of {path}.")

            for i, nlq in enumerate(nl_queries, start=1):
                flattened.append({
                    "record_id": f"{base_record_id}_{i}",
                    "db_id": db_id,
                    "nlq": str(nlq),
                    "SQL": sql_gold,
                    "MQL": mql_gold,
                })

        return flattened

    # Already-flattened input: one row per NLQ.
    for row_num, rec in enumerate(records, start=1):
        if not isinstance(rec, dict):
            raise RuntimeError(f"Benchmark row {row_num} in {path} is not an object.")

        record_id = str(rec.get("record_id", row_num))
        db_id = get_required_string(rec, ("db_id",), row_num, path)
        nlq = get_required_string(rec, ("nlq", "question"), row_num, path)
        sql_gold = get_required_string(rec, ("SQL", "ref_sql", "query"), row_num, path)
        mql_gold = str(rec.get("MQL", "") or "")

        flattened.append({
            "record_id": record_id,
            "db_id": db_id,
            "nlq": nlq,
            "SQL": sql_gold,
            "MQL": mql_gold,
        })

    return flattened


def detect_prediction_format(path: Path, requested_format: str) -> str:
    if requested_format != "auto":
        return requested_format

    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        return "jsonl"
    if suffix == ".json":
        return "json"
    return "txt"


def extract_prediction_value(
    item: Any,
    item_num: int,
    source: Path,
    prediction_field: Optional[str],
) -> str:
    if isinstance(item, str):
        return item

    if not isinstance(item, dict):
        raise RuntimeError(
            f"Prediction item {item_num} in {source} must be a string or object."
        )

    if prediction_field:
        if prediction_field not in item:
            raise RuntimeError(
                f"Prediction item {item_num} in {source} is missing field '{prediction_field}'."
            )
        return str(item[prediction_field] or "")

    for field in PREDICTION_FIELD_CANDIDATES:
        if field in item:
            return str(item[field] or "")

    candidates = ", ".join(PREDICTION_FIELD_CANDIDATES)
    raise RuntimeError(
        f"Could not find prediction SQL in item {item_num} of {source}. "
        f"Use --prediction-field. Tried: {candidates}."
    )


def load_predictions(
    path: Path,
    prediction_format: str,
    prediction_field: Optional[str],
    keep_blank_lines: bool,
) -> List[str]:
    fmt = detect_prediction_format(path, prediction_format)

    if fmt == "txt":
        predictions = []
        with path.open("r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, start=1):
                value = line.rstrip("\n\r")
                if not keep_blank_lines and not value.strip():
                    continue
                predictions.append(value.strip())
        return predictions

    if fmt == "json":
        data = load_json_array(path)
        return [
            extract_prediction_value(item, i, path, prediction_field).strip()
            for i, item in enumerate(data, start=1)
        ]

    if fmt == "jsonl":
        predictions = []
        with path.open("r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    if keep_blank_lines:
                        predictions.append("")
                    continue

                try:
                    item = json.loads(line)
                except json.JSONDecodeError as e:
                    raise RuntimeError(
                        f"Invalid JSON on line {line_num} of {path}: {e}"
                    ) from e

                predictions.append(
                    extract_prediction_value(item, line_num, path, prediction_field).strip()
                )

        return predictions

    raise RuntimeError(f"Unsupported prediction format: {fmt}")


def build_records(
    benchmark_rows: List[Dict[str, Any]],
    predictions: Optional[List[str]],
    sql_source: str,
) -> List[Dict[str, Any]]:
    output = []

    if sql_source == "pred" and predictions is None:
        raise RuntimeError("--predictions is required when --sql-source pred.")

    if sql_source == "pred" and len(benchmark_rows) != len(predictions or []):
        raise RuntimeError(
            "Benchmark and prediction lengths must match for positional merge. "
            f"Benchmark={len(benchmark_rows)} vs Predictions={len(predictions or [])}."
        )

    for idx, row in enumerate(benchmark_rows, start=1):
        sql_gold = str(row.get("SQL", "") or "")

        if sql_source == "gold":
            sql_pred = sql_gold
        else:
            assert predictions is not None
            sql_pred = predictions[idx - 1]

        output.append({
            "count": idx,
            "record_id": str(row.get("record_id", idx)),
            "db_id": str(row.get("db_id", "")).strip(),
            "nlq": str(row.get("nlq", "")),
            "SQL": sql_gold,
            "SQL_pred": sql_pred,
            "MQL": str(row.get("MQL", "") or ""),
            "MQL_pred": "",
        })

    return output


def write_output(records: List[Dict[str, Any]], path: Path, output_format: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    if output_format == "json":
        with path.open("w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        return

    if output_format == "jsonl":
        with path.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return

    raise RuntimeError(f"Unsupported output format: {output_format}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare canonical SQL-to-MongoDB pipeline input."
    )

    parser.add_argument(
        "--benchmark",
        required=True,
        help="TEND benchmark file. Can be compact test.json or flattened testCopy_flat.json.",
    )
    parser.add_argument(
        "--predictions",
        help="SQL prediction file. Required unless --sql-source gold is used.",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Output canonical input file.",
    )
    parser.add_argument(
        "--sql-source",
        choices=("pred", "gold"),
        default="pred",
        help="Use predicted SQL or gold SQL as SQL_pred. Default: pred.",
    )
    parser.add_argument(
        "--prediction-format",
        choices=("auto", "txt", "json", "jsonl"),
        default="auto",
        help="Prediction file format. Default: auto.",
    )
    parser.add_argument(
        "--prediction-field",
        help=(
            "Field containing predicted SQL when predictions are JSON/JSONL objects. "
            "If omitted, common field names are tried automatically."
        ),
    )
    parser.add_argument(
        "--output-format",
        choices=("json", "jsonl"),
        default="json",
        help="Canonical output format. Default: json.",
    )
    parser.add_argument(
        "--keep-blank-prediction-lines",
        action="store_true",
        help=(
            "For TXT/JSONL prediction files, preserve blank lines as empty predictions. "
            "Use this only if blank lines are meaningful placeholders."
        ),
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    benchmark_path = Path(args.benchmark)
    prediction_path = Path(args.predictions) if args.predictions else None
    out_path = Path(args.out)

    benchmark_rows = flatten_benchmark_rows(benchmark_path)

    predictions = None
    if args.sql_source == "pred":
        if prediction_path is None:
            raise RuntimeError("--predictions is required when --sql-source pred.")

        predictions = load_predictions(
            path=prediction_path,
            prediction_format=args.prediction_format,
            prediction_field=args.prediction_field,
            keep_blank_lines=args.keep_blank_prediction_lines,
        )

    records = build_records(
        benchmark_rows=benchmark_rows,
        predictions=predictions,
        sql_source=args.sql_source,
    )

    write_output(records, out_path, args.output_format)

    print(f"[OK] Benchmark rows: {len(benchmark_rows)}")
    if predictions is not None:
        print(f"[OK] Predictions:    {len(predictions)}")
    print(f"[OK] Wrote:          {out_path.resolve()}")


if __name__ == "__main__":
    main()