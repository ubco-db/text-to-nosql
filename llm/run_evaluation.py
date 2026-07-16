#!/usr/bin/env python3
"""
Run evaluation for an LLM-generated MongoDB prediction JSONL file.

This script:
  1. Reads an LLM JSONL output file.
  2. Normalizes fields expected by evaluation/compute_metrics.py.
  3. Writes evaluation/results/<run_name>.json as a JSON array.
  4. Runs compute_metrics.py with the selected metric mode.

Example from repo root:

  python llm/run_evaluation.py ^
    --input results/paper/llm/llm_gemini_31_flash_lite_nlq_20.jsonl ^
    --run-name llm_gemini_31_flash_lite_nlq_20 ^
    --metric-mode enhanced

Or let the run name come from the input filename:

  python llm/run_evaluation.py ^
    --input results/paper/llm/llm_gemini_31_flash_lite_nlq_20.jsonl
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
EVALUATION_DIR = REPO_ROOT / "evaluation"
EVALUATION_RESULTS_DIR = EVALUATION_DIR / "results"
COMPUTE_METRICS = EVALUATION_DIR / "compute_metrics.py"


def load_json_or_jsonl(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()

    if not text:
        return []

    if text.startswith("["):
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError(f"Expected JSON array in {path}")
        return data

    rows: List[Dict[str, Any]] = []

    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()

            if not line:
                continue

            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_no}: {exc}") from exc

            if not isinstance(obj, dict):
                raise ValueError(f"Expected JSON object at {path}:{line_no}")

            rows.append(obj)

    return rows


def first_present(row: Dict[str, Any], names: Iterable[str], default: str = "") -> str:
    for name in names:
        value = row.get(name)

        if value is not None and str(value).strip():
            return str(value)

    return default


def normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    compute_metrics.py expects records with:
      db_id, nlq, SQL, SQL_pred, MQL, MQL_pred

    The LLM script already writes MQL_pred and prediction_query, but this
    normalizer makes the evaluation robust to either canonical pipeline names
    or JSONL analysis-output names.
    """
    out = dict(row)

    out["db_id"] = first_present(row, ["db_id", "database_id", "database"], "")
    out["nlq"] = first_present(row, ["nlq", "NLQ", "question", "utterance", "query"], "")

    out["SQL"] = first_present(row, ["SQL", "source_gold_sql", "gold_sql"], "")
    out["SQL_pred"] = first_present(row, ["SQL_pred", "source_sql", "pred_sql", "SQL"], "")

    out["MQL"] = first_present(row, ["MQL", "target_query", "gold_mql"], "")
    out["MQL_pred"] = first_present(row, ["MQL_pred", "prediction_query", "pred_mql", "llm_query"], "")

    # Keep both names because some comparison scripts use prediction_query.
    out["prediction_query"] = out["MQL_pred"]

    return out


def write_metric_input(rows: List[Dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def run_compute_metrics(run_name: str, metric_mode: str, extra_args: Optional[List[str]] = None) -> int:
    if not COMPUTE_METRICS.exists():
        raise FileNotFoundError(f"Could not find compute_metrics.py at {COMPUTE_METRICS}")

    cmd = [
        sys.executable,
        str(COMPUTE_METRICS.name),
        "--file_name",
        run_name,
        "--metric-mode",
        metric_mode,
    ]

    if extra_args:
        cmd.extend(extra_args)

    print("Running:", " ".join(cmd), flush=True)

    completed = subprocess.run(
        cmd,
        cwd=str(EVALUATION_DIR),
        text=True,
    )

    return completed.returncode


def print_output_paths(run_name: str) -> None:
    candidates = [
        EVALUATION_RESULTS_DIR / f"{run_name}.json",
        EVALUATION_RESULTS_DIR / f"{run_name}_examples.jsonl",
        EVALUATION_RESULTS_DIR / f"{run_name}_metrics.log",
        EVALUATION_RESULTS_DIR / f"{run_name}_wrong_examples.json",
        EVALUATION_RESULTS_DIR / f"{run_name}_summary_by_db.csv",
        EVALUATION_RESULTS_DIR / f"{run_name}_summary_by_bucket.csv",
        EVALUATION_RESULTS_DIR / f"{run_name}_summary_by_signature.csv",
    ]

    print("\nEvaluation outputs:", flush=True)

    for path in candidates:
        exists = "exists" if path.exists() else "missing"
        print(f"  [{exists}] {path.relative_to(REPO_ROOT)}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare and run metrics for an LLM prediction JSONL file.")
    parser.add_argument("--input", required=True, type=Path, help="LLM prediction JSONL or JSON file.")
    parser.add_argument("--run-name", default=None, help="Name used under evaluation/results. Defaults to input stem.")
    parser.add_argument("--metric-mode", default="enhanced", choices=["enhanced", "tend"])
    parser.add_argument("--limit", type=int, default=None, help="Evaluate only the first N rows.")
    parser.add_argument("--skip-errors", action="store_true", help="Drop rows with llm_error before evaluation.")
    parser.add_argument("--dry-run", action="store_true", help="Only write evaluation input; do not run compute_metrics.py.")
    parser.add_argument(
        "--extra-metric-arg",
        action="append",
        default=[],
        help="Additional argument passed to compute_metrics.py. Repeat for multiple args.",
    )

    args = parser.parse_args()

    input_path = args.input
    if not input_path.is_absolute():
        input_path = REPO_ROOT / input_path

    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    run_name = args.run_name or input_path.stem

    rows = load_json_or_jsonl(input_path)

    if args.skip_errors:
        before = len(rows)
        rows = [row for row in rows if not row.get("llm_error")]
        print(f"Skipped {before - len(rows)} rows with llm_error.", flush=True)

    if args.limit is not None:
        rows = rows[: args.limit]

    normalized = [normalize_row(row) for row in rows]

    missing_required = []
    for i, row in enumerate(normalized):
        missing = [name for name in ["db_id", "nlq", "MQL", "MQL_pred"] if not row.get(name)]
        if missing:
            missing_required.append((i, row.get("db_id"), missing))

    if missing_required:
        print("WARNING: Some rows are missing required evaluation fields:", flush=True)
        for i, db_id, missing in missing_required[:20]:
            print(f"  row={i} db_id={db_id!r} missing={missing}", flush=True)
        if len(missing_required) > 20:
            print(f"  ... {len(missing_required) - 20} more", flush=True)

    output_path = EVALUATION_RESULTS_DIR / f"{run_name}.json"
    write_metric_input(normalized, output_path)

    print(f"Wrote {len(normalized)} rows to {output_path.relative_to(REPO_ROOT)}", flush=True)

    if args.dry_run:
        print_output_paths(run_name)
        return 0

    return_code = run_compute_metrics(
        run_name=run_name,
        metric_mode=args.metric_mode,
        extra_args=args.extra_metric_arg,
    )

    print_output_paths(run_name)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())