#!/usr/bin/env python3
"""
collect_mql_preds.py  —  Send SQLs to the Java SQL→Mongo translator server and save results.

INPUT
  • JSONL: one JSON object per line
  • JSON : an array of objects
Each object must contain:
  - "db_id": <Mongo database name>
  - one of "question" | "sql" | "ref_sql"  (we normalize to "sql")

OUTPUT
  • JSON array with objects of the form:
      {
        "db_id": "<db name>",
        "sql":   "<original SQL>",
        "mongodb": "<MongoDB query string, or 'ERROR: ...'>"
      }

SERVER CONTRACT
  - Endpoint: /translate
  - Method:   GET
  - Params:   db=<db_id>, sql=<SQL string URL-encoded>
    e.g. http://localhost:8082/translate?db=tpch&sql=SELECT%20*%20FROM%20nation

Run example:
  python collect_mql_preds.py 
  --in out/merged.jsonl 
  --out out/output.json 
  --url http://localhost:8082/translate   
  --debug  
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Tuple

import requests 

# ---------------- Defaults ----------------
DEFAULT_URL: str = "http://localhost:8082/translate"
DEFAULT_METHOD: str = "get"        
DEFAULT_TIMEOUT: float = 90.0      
DEFAULT_RETRIES: int = 2           


def load_records(path: Path) -> List[Dict[str, Any]]:
    """
    Read a JSONL file or JSON array file.

    For canonical pipeline input, records are preserved as-is so MQL_pred
    can be filled in without losing benchmark fields.

    Legacy minimal input is still supported.
    """
    text = path.read_text(encoding="utf-8-sig").strip()
    raw: List[Any] = []

    if not text:
        raise SystemExit(f"No records found in input: {path}")

    # Detect JSON array vs JSON Lines by first non-space char.
    if text.startswith("["):
        data = json.loads(text)
        if not isinstance(data, list):
            raise SystemExit("Top-level JSON must be an array.")
        raw = data
    else:
        for ln, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise SystemExit(f"Invalid JSON on line {ln}: {e}")

    records: List[Dict[str, Any]] = []
    for i, obj in enumerate(raw, start=1):
        if not isinstance(obj, dict):
            raise SystemExit(f"Record #{i} is not an object (got {type(obj)}).")
        if not obj.get("db_id"):
            raise SystemExit(f"Record #{i} missing 'db_id'. Object={obj}")
        records.append(obj)

    return records

def is_canonical_record(rec: Dict[str, Any]) -> bool:
    return (
        "MQL" in rec
        or "MQL_pred" in rec
        or "SQL" in rec
        or "SQL_pred" in rec
        or "nlq" in rec
        or "record_id" in rec
    )


def choose_sql(rec: Dict[str, Any], sql_source: str, idx: int) -> str:
    if sql_source == "gold":
        candidates = ("SQL", "SQL_gold", "sql_gold", "ref_sql", "query", "sql")
    else:
        candidates = ("SQL_pred", "sql_pred", "prediction", "pred", "sql", "query")

    for field in candidates:
        value = rec.get(field)
        if value is None:
            continue

        sql = str(value).strip()
        if sql:
            return sql

    raise SystemExit(
        f"Record #{idx} missing SQL for --sql-source {sql_source}. "
        f"Tried fields: {', '.join(candidates)}. Object={rec}"
    )

# ===================== HTTP helpers =====================
def _prepare_url_for_get(url: str, params: Dict[str, str]) -> str:
    """
    Build the exact URL that requests will hit for GET (with proper URL encoding)
    """
    req = requests.Request("GET", url, params=params)
    prepped = req.prepare()
    return prepped.url  # fully encoded


def _call_get(url: str, db: str, sql: str, timeout: float, debug: bool) -> Dict[str, Any]:
    """
    Call Java server using GET with ?db=<db>&sql=<url-encoded>.
    Returns JSON body as dict (or raises with status+body on failure).
    """
    params = {"db": db, "sql": sql}
    if debug:
        print("[DEBUG] GET URL ->", _prepare_url_for_get(url, params))
    
    # Server always returns JSON; if not, show raw text for debugging.
    data = {}
    r = None
    try:
        r = requests.get(url, params=params, timeout=timeout)
        if debug:
            print(r)
        data = r.json()
        if debug:        
            print(data)
    except Exception as e:
        print(e)  # Show first 500 chars
        data["mongo"] = f"ERROR: {e}"  # If we can't parse JSON, return error in "mongo" field        

    if r is not None and r.status_code >= 400:        
        err = data.get("error") or f"HTTP {r.status_code}"
        raise requests.HTTPError(f"{err} | body={data}")
    return data


def fetch_mql(url: str, method: str, db: str, sql: str,
              timeout: float, retries: int, debug: bool) -> str:
    """
    Try to fetch the translated Mongo string for one (db, sql).    
    """
    last_err: Optional[BaseException] = None
    for attempt in range(retries + 1):
        try:            
            if method == "get":                
                data = _call_get(url, db, sql, timeout, debug)
            
            mongo = data.get("mongo", "")
            # normalize to string
            return mongo if isinstance(mongo, str) else str(mongo)
        except Exception as e:
            last_err = e
            if debug:
                print(f"[DEBUG] Attempt {attempt+1}/{retries+1} failed: {e}")
    return f"ERROR: {last_err}"


# ===================== Batch driver =====================
def run_batch(
    records: List[Dict[str, Any]],
    server_url: str,
    method: str,
    timeout: float,
    retries: int,
    debug: bool,
    sql_source: str,
) -> List[Dict[str, Any]]:
    """
    Translate records and build output.

    Canonical input:
      preserve the full record and fill MQL_pred.

    Legacy minimal input:
      write {"db_id", "sql", "mongodb"} for backward compatibility.
    """
    out: List[Dict[str, Any]] = []

    for idx, rec in enumerate(records, start=1):
        db = str(rec.get("db_id", "")).strip()
        sql = choose_sql(rec, sql_source, idx)

        if debug:
            print(f"\n[DEBUG] ---- Record #{idx} ----")
            print("[DEBUG] db_id =", db)
            print("[DEBUG] sql_source =", sql_source)
            print("[DEBUG] sql   =", sql[:500], "..." if len(sql) > 500 else "")

        mql = fetch_mql(server_url, method, db, sql, timeout, retries, debug)

        if is_canonical_record(rec):
            out_rec = dict(rec)
            out_rec["MQL_pred"] = mql
        else:
            out_rec = {
                "db_id": db,
                "sql": sql,
                "mongodb": mql,
            }

        out.append(out_rec)

    return out

def check_server_available(url: str, timeout: float) -> None:
    try:
        requests.get(url, timeout=timeout)
    except requests.exceptions.ConnectionError as e:
        raise SystemExit(
            f"Translator server is not reachable at {url}.\n"
            "Start TranslateServer first, or run through run_pipeline.py.\n"
            f"Original error: {e}"
        ) from e
    except requests.exceptions.Timeout as e:
        raise SystemExit(
            f"Translator server timed out at {url}.\n"
            f"Original error: {e}"
        ) from e
    except Exception:
        # The endpoint may return an error because db/sql params are missing.
        # That is fine; it still proves the server is reachable.
        return
    
# ===================== CLI =====================
def main() -> None:
    script_start = time.perf_counter()
    ap = argparse.ArgumentParser(description="Collect MQL translations from Java server.")
    ap.add_argument("--in", dest="in_path", required=True,
                    help="Path to canonical JSON/JSONL input or legacy JSONL input.")
    ap.add_argument("--out", dest="out_path", default="out/output.json",
                    help="Path to write output JSON (default: out/output.json).")
    ap.add_argument("--url", default=DEFAULT_URL,
                    help=f"Translator endpoint (default: {DEFAULT_URL})")    
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help=f"HTTP timeout seconds (default: {DEFAULT_TIMEOUT})")
    ap.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                    help=f"Retry attempts on failure (default: {DEFAULT_RETRIES})")
    ap.add_argument("--debug", action="store_true",
                    help="Print full prepared request (URL/payload) and verbose errors.")   
    ap.add_argument("--sql-source", choices=("pred", "gold"), default="pred", help="SQL field to translate: pred uses SQL_pred; gold uses SQL. Default: pred.",) 
    args = ap.parse_args()

    in_path = Path(args.in_path)
    out_path = Path(args.out_path)

    # Load and normalize input records
    records = load_records(in_path)
    # So, records is now a list of {"db_id", "sql"} dicts
    if not records:
        raise SystemExit("No records found in input.")    

    # Fail fast if TranslateServer is not running.
    check_server_available(args.url, args.timeout)

    # Batch translate
    preds = run_batch(
        records,
        args.url,
        "get",
        args.timeout,
        args.retries,
        args.debug,
        args.sql_source,
    )

    # Write output JSON
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(preds, f, ensure_ascii=False, indent=2)
    print(f"[OK] Wrote {len(preds)} rows to {out_path}")

    elapsed = time.perf_counter() - script_start
    print(f"Total runtime: {elapsed:.2f} seconds")

if __name__ == "__main__":
    main()
