#!/usr/bin/env python3
"""
Direct LLM baseline for Text-to-MongoDB translation.

Inputs:
  - JSON or JSONL examples containing NLQ/question, db_id, and optionally SQL/SQL_pred.
  - A schema directory or schema JSON file containing MongoDB schema text per db_id.

Outputs:
  - JSONL records copied from input with MQL_pred/prediction_query populated.
  - Raw model output and prompt are also stored for reproducibility.

Environment:
  OPENAI_API_KEY   required for --provider openai
  GEMINI_API_KEY   required for --provider gemini

Examples:
  python llm/llm_mql_baseline.py ^
    --provider openai ^
    --model gpt-5.6 ^
    --input ../data/input_clean.json ^
    --schema-dir ../TEND/mongodb_schema ^
    --output ../results/llm_openai_direct.jsonl ^
    --limit 100

  python llm/llm_mql_baseline.py ^
    --provider gemini ^
    --model gemini-3.5-flash ^
    --input ../data/input_clean.json ^
    --schema-dir ../TEND/mongodb_schema ^
    --output ../results/llm_gemini_direct.jsonl

  python llm/llm_mql_baseline.py ^
    --provider openai ^
    --model gpt-5.6 ^
    --mode nlq_sql ^
    --sql-field SQL_pred ^
    --input ../data/input_clean.json ^
    --schema-dir ../TEND/mongodb_schema ^
    --output ../results/llm_openai_nlq_sql.jsonl
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from mql_extractor import extract_first_mongo_query

SYSTEM_INSTRUCTION = """You are an expert database query translator.
Translate natural-language database questions into MongoDB queries.

The first characters of your response must be db.
Return exactly one MongoDB shell query and nothing else.
Do not number steps.
Do not include headings.
Do not include prose before or after the query.
Do not place semicolons inside an aggregate pipeline array or document.
Only place an optional semicolon after the complete query.
Do not include Markdown fences.
Do not explain.
Do not include comments.
The query must be executable JavaScript-style MongoDB shell syntax:
db.<collection>.find(...)
or
db.<collection>.aggregate([...])

Use only collections and fields that appear in the supplied MongoDB schema.
Preserve exact field and collection names from the schema.
Prefer aggregate pipelines when joins, grouping, nested arrays, sorting with limits, or distinct over nested data are required.
For nested array tables, use $unwind before filtering, grouping, sorting, or projecting nested fields.
For SQL-style COUNT(*), use {$sum: 1} inside $group.
For DISTINCT, use $group or $addToSet as appropriate.
For ORDER BY with LIMIT, place $sort before $limit.
For output fields, project only the fields requested by the question.
Suppress _id unless it is explicitly requested.
Final output documents must use flat field names.
Do not use dotted keys as output field names in the final $project stage.
When projecting a field from a nested document, joined document, or unwound array, map it to a flat output field using the field's leaf name unless the question explicitly requests a different alias.
For example, project a nested source path as {leafName: "$path.to.leafName"}, not {"path.to.leafName": 1}.
Do not expose internal lookup aliases, temporary collection aliases, or nested path prefixes in final output field names.
For aggregate output fields, use simple SQL-style aliases: count for COUNT(*), avg_<field> for AVG(field), sum_<field> for SUM(field), min_<field> for MIN(field), and max_<field> for MAX(field), unless the question explicitly requests a different output name. Preserve the original field name after the aggregate prefix when possible.
When translating SQL-style COUNT over joined or related rows stored in an array, prefer $unwind followed by $group and {$sum: 1}. Use $size only when the question asks for the size of an array directly or when no join-row semantics are implied.
Translate SQL LIKE string matching as case-insensitive MongoDB regex using $regex with $options: "i", unless the SQL explicitly requires case-sensitive matching.
For final output field names, prefer the original MongoDB schema leaf field names unless the natural-language question explicitly requests a different alias.
"""


def build_prompt(
    *,
    db_id: str,
    nlq: str,
    schema_text: str,
    mode: str = "nlq",
    sql: Optional[str] = None,
) -> str:
    if mode == "nlq":
        return f"""Database id:
{db_id}

MongoDB schema:
{schema_text}

Natural-language question:
{nlq}

Generate the MongoDB query equivalent to the question.
Return only the query."""

    if mode == "nlq_sql":
        return f"""Database id:
{db_id}

MongoDB schema:
{schema_text}

Natural-language question:
{nlq}

SQL query:
{sql or ""}

Generate the MongoDB query equivalent to the SQL query and the natural-language question.
If the SQL and natural-language question disagree, prefer the SQL query, but use the question to disambiguate output field names.
Return only the query."""

    raise ValueError(f"Unsupported mode: {mode}")


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
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_no}: {exc}") from exc
    return rows


def dump_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def first_present(row: Dict[str, Any], names: List[str], default: str = "") -> str:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value)
    return default


def read_schema_text(db_id: str, schema_dir: Optional[Path], schema_file: Optional[Path]) -> str:
    if schema_file is not None:
        data = json.loads(schema_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            value = data.get(db_id)
            if isinstance(value, str):
                return value
            if value is not None:
                return json.dumps(value, ensure_ascii=False, indent=2)
        raise KeyError(f"Could not find db_id={db_id!r} in schema file {schema_file}")

    if schema_dir is None:
        raise ValueError("Either --schema-dir or --schema-file is required.")

    candidates = [
        schema_dir / f"{db_id}.json",
        schema_dir / f"{db_id}.txt",
        schema_dir / db_id / "schema.json",
        schema_dir / db_id / "mongodb_schema.json",
        schema_dir / db_id / "schema.txt",
    ]

    for candidate in candidates:
        if candidate.exists():
            if candidate.suffix.lower() == ".json":
                try:
                    return json.dumps(json.loads(candidate.read_text(encoding="utf-8")), ensure_ascii=False, indent=2)
                except json.JSONDecodeError:
                    return candidate.read_text(encoding="utf-8")
            return candidate.read_text(encoding="utf-8")

    # Fallback: search recursively for files containing the db_id.
    matches = sorted(schema_dir.rglob(f"*{db_id}*"))
    for match in matches:
        if match.is_file() and match.suffix.lower() in {".json", ".txt"}:
            if match.suffix.lower() == ".json":
                try:
                    return json.dumps(json.loads(match.read_text(encoding="utf-8")), ensure_ascii=False, indent=2)
                except json.JSONDecodeError:
                    return match.read_text(encoding="utf-8")
            return match.read_text(encoding="utf-8")

    raise FileNotFoundError(f"No schema file found for db_id={db_id!r} under {schema_dir}")


class RateLimitBackoff:
    def __init__(self, base_sleep: float = 2.0, max_sleep: float = 60.0) -> None:
        self.base_sleep = base_sleep
        self.max_sleep = max_sleep

    def sleep(self, attempt: int) -> None:
        delay = min(self.max_sleep, self.base_sleep * (2 ** attempt))
        jitter = random.uniform(0, min(1.0, delay * 0.1))
        time.sleep(delay + jitter)


def call_openai(
    *,
    model: str,
    prompt: str,
    temperature: float,
    max_output_tokens: int,
    timeout_retries: int,
) -> str:
    from openai import OpenAI

    client = OpenAI()
    backoff = RateLimitBackoff()

    last_exc: Optional[BaseException] = None
    for attempt in range(timeout_retries + 1):
        try:
            response = client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": SYSTEM_INSTRUCTION},
                    {"role": "user", "content": prompt},
                ],
                temperature=temperature,
                max_output_tokens=max_output_tokens,
            )
            return getattr(response, "output_text", "") or ""
        except Exception as exc:
            last_exc = exc
            if attempt >= timeout_retries:
                break
            backoff.sleep(attempt)

    raise RuntimeError(f"OpenAI request failed after retries: {last_exc}") from last_exc


def call_gemini(
    *,
    model: str,
    prompt: str,
    temperature: float,
    max_output_tokens: int,
    timeout_retries: int,
) -> str:
    import sys
    import traceback
    from google import genai
    from google.genai import types

    client = genai.Client()
    backoff = RateLimitBackoff(base_sleep=2.0, max_sleep=10.0)

    last_exc = None

    for attempt in range(timeout_retries + 1):
        try:
            print(
                f"[Gemini] model={model} attempt={attempt + 1}/{timeout_retries + 1} "
                f"prompt_chars={len(prompt)} max_output_tokens={max_output_tokens}",
                file=sys.stderr,
                flush=True,
            )

            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                ),
            )

            text = getattr(response, "text", "") or ""

            try:
                candidates = getattr(response, "candidates", None) or []
                if candidates:
                    finish_reason = getattr(candidates[0], "finish_reason", None)
                    print(f"[Gemini] finish_reason={finish_reason}", file=sys.stderr, flush=True)
            except Exception:
                pass

            return text

        except Exception as exc:
            last_exc = exc
            msg = str(exc)
            exc_type = type(exc).__name__

            print("\n[Gemini ERROR]", file=sys.stderr, flush=True)
            print(f"  type: {exc_type}", file=sys.stderr, flush=True)
            print(f"  model: {model}", file=sys.stderr, flush=True)
            print(f"  attempt: {attempt + 1}/{timeout_retries + 1}", file=sys.stderr, flush=True)
            print(f"  message: {msg}", file=sys.stderr, flush=True)

            # Many Google SDK exceptions expose extra structured fields.
            for attr in ["code", "status", "message", "details", "response"]:
                if hasattr(exc, attr):
                    try:
                        print(f"  {attr}: {getattr(exc, attr)}", file=sys.stderr, flush=True)
                    except Exception:
                        pass

            print("  traceback:", file=sys.stderr, flush=True)
            traceback.print_exception(type(exc), exc, exc.__traceback__, limit=5, file=sys.stderr)

            lower_msg = msg.lower()

            # Do not retry permanent or currently-useless conditions during benchmarking.
            non_retriable = (
                "400" in msg
                or "401" in msg
                or "403" in msg
                or "404" in msg
                or "429" in msg
                or "quota" in lower_msg
                or "not_found" in lower_msg
                or "no longer available" in lower_msg
                or "not supported" in lower_msg
                or "invalid" in lower_msg
                or "permission" in lower_msg
                or "api key" in lower_msg
                or "503" in msg
                or "unavailable" in lower_msg
                or "high demand" in lower_msg
            )

            if non_retriable:
                raise RuntimeError(
                    f"Gemini request failed without retry "
                    f"(type={exc_type}, model={model}, message={msg})"
                ) from exc

            if attempt >= timeout_retries:
                break

            backoff.sleep(attempt)

    raise RuntimeError(
        f"Gemini request failed after retries "
        f"(type={type(last_exc).__name__}, model={model}, message={last_exc})"
    ) from last_exc

def call_model(
    *,
    provider: str,
    model: str,
    prompt: str,
    temperature: float,
    max_output_tokens: int,
    retries: int,
) -> str:
    if provider == "openai":
        return call_openai(
            model=model,
            prompt=prompt,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            timeout_retries=retries,
        )

    if provider == "gemini":
        return call_gemini(
            model=model,
            prompt=prompt,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            timeout_retries=retries,
        )

    raise ValueError(f"Unsupported provider: {provider}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Direct LLM Text-to-MongoDB baseline.")
    parser.add_argument("--provider", choices=["openai", "gemini"], required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--schema-dir", type=Path, default=None)
    parser.add_argument("--schema-file", type=Path, default=None)
    parser.add_argument("--mode", choices=["nlq", "nlq_sql"], default="nlq")
    parser.add_argument("--nlq-field", default=None, help="Override NLQ field name.")
    parser.add_argument("--sql-field", default="SQL_pred", help="Used only with --mode nlq_sql.")
    parser.add_argument("--db-field", default="db_id")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-output-tokens", type=int, default=10000)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--sleep", type=float, default=0.0, help="Seconds to sleep between requests.")
    parser.add_argument("--store-prompt", action="store_true")
    args = parser.parse_args()

    if args.provider == "openai" and not os.getenv("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY is not set.", file=sys.stderr)
        return 2

    if args.provider == "gemini" and not os.getenv("GEMINI_API_KEY"):
        print("ERROR: GEMINI_API_KEY is not set.", file=sys.stderr)
        return 2

    rows = load_json_or_jsonl(args.input)

    if args.start:
        rows = rows[args.start:]

    if args.limit is not None:
        rows = rows[: args.limit]

    done_keys = set()
    if args.resume and args.output.exists():
        for row in load_json_or_jsonl(args.output):
            key = row.get("example_id", row.get("count", row.get("id")))
            if key is not None:
                done_keys.add(str(key))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.resume and args.output.exists() else "w"

    with args.output.open(mode, encoding="utf-8") as out:
        for idx, row in enumerate(rows):
            key = row.get("example_id", row.get("count", row.get("id", idx)))
            if args.resume and str(key) in done_keys:
                continue

            db_id = first_present(row, [args.db_field, "db_id"])
            if not db_id:
                raise ValueError(f"Missing db_id in row {idx}: {row.keys()}")

            if args.nlq_field:
                nlq = first_present(row, [args.nlq_field])
            else:
                nlq = first_present(row, ["NLQ", "nlq", "question", "query", "utterance"])

            if not nlq:
                raise ValueError(f"Missing NLQ/question in row {idx}, db_id={db_id}")

            sql = first_present(row, [args.sql_field, "SQL_pred", "source_sql", "SQL", "source_gold_sql"])
            schema_text = read_schema_text(db_id, args.schema_dir, args.schema_file)

            prompt = build_prompt(
                db_id=db_id,
                nlq=nlq,
                schema_text=schema_text,
                mode=args.mode,
                sql=sql,
            )

            started = time.time()
            error = None
            raw_output = ""
            cleaned_query = ""

            try:
                raw_output = call_model(
                    provider=args.provider,
                    model=args.model,
                    prompt=prompt,
                    temperature=args.temperature,
                    max_output_tokens=args.max_output_tokens,
                    retries=args.retries,
                )
                cleaned_query = extract_first_mongo_query(raw_output)
            except Exception as exc:
                error = repr(exc)

            elapsed = time.time() - started

            result = copy.deepcopy(row)
            result["llm_provider"] = args.provider
            result["llm_model"] = args.model
            result["llm_mode"] = args.mode
            result["llm_temperature"] = args.temperature
            result["llm_elapsed_seconds"] = round(elapsed, 3)
            result["llm_raw_output"] = raw_output
            result["llm_error"] = error
            result["MQL_pred"] = cleaned_query
            result["prediction_query"] = cleaned_query

            if args.store_prompt:
                result["llm_prompt"] = prompt

            out.write(json.dumps(result, ensure_ascii=False) + "\n")
            out.flush()

            status = "OK" if error is None else "ERR"
            print(f"[{idx + 1}/{len(rows)}] {status} {db_id} key={key} {elapsed:.2f}s", file=sys.stderr)

            if args.sleep > 0:
                time.sleep(args.sleep)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())