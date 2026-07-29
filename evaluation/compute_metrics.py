#!/usr/bin/env python3                  # Shebang: allow running as an executable script on Unix-like systems with Python 3
"""
Fast, instrumented metrics for MongoDB query evaluation.

What changed vs. your original:
- Single global MongoClient reused across all examples.
- PyMongo "fast path" for aggregate() and find() queries (no mongosh subprocess).
- LRU cache for execution results to avoid re-running identical queries.
- Bounded preview printing to avoid spending time formatting huge JSON.
- Lightweight timing breakdown to pinpoint bottlenecks.

Metrics computed (same semantics):
- EM  : exact string match (after whitespace normalization)
- QSM : query stage sequence match
- QFC : query fields coverage set equality
- EX  : deep result equality (structure + values)
- EFM : result fields equality
- EVM : per-document value equality (1 only if every document equals partner)
"""                                    # Module docstring: high-level description and the metric definitions

import json                             # Standard lib: JSON encoding/decoding
import re                               # Regular expressions used for parsing / normalization
import time                             # Timing utilities for profiling
from dataclasses import dataclass       # Dataclass for clean configuration container
from pathlib import Path                # Filesystem path handling
from typing import List, Dict, Tuple, Any  # Type hints for better clarity / IDE support
from functools import lru_cache         # Memoization decorator for caching expensive function results
from collections import defaultdict, Counter     # Dict subclass with default factory, used for timing buckets
from contextlib import contextmanager, redirect_stdout, redirect_stderr  # Context managers for timing and log redirection

import argparse
import csv
import sys
import hashlib
import demjson3 as demjson              # More lenient JSON parser (handles single quotes / trailing commas, etc.)
from pymongo import MongoClient         # PyMongo client (native driver, faster than shell for exec)
from tqdm import tqdm                   # Progress bar for loop visibility

# Your existing utilities
from extract_fields import extract_fields    # Custom utility: extracts field names from MQL
from extract_stages import get_query_stages  # Custom utility: extracts stage sequence from MQL
from mongosh_exec import MongoShellExecutor  # Custom executor that uses mongosh as a subprocess fallback


# ----------------------------
# Config and simple utilities
# ----------------------------

@dataclass
class MetricConfig:
    """Configuration for evaluation."""
    mongodb_uri: str = 'mongodb://localhost:27017/'             # URI to MongoDB server
    wrong_examples_path: Path = Path('./wrong_examples_icl.json')# Where to dump wrong examples
    metrics_list: List[str] = ('EX', 'EM', 'QSM', 'QFC', 'EFM', 'EVM')  # Which metrics to compute/aggregate

    # Tunables
    cache_size: int = 2048                         # LRU cache size for execution results (not used directly; see @lru_cache below)
    preview_chars: int = 1500                      # Bound how much of results we print to logs to avoid huge dumps
    allow_disk_use: bool = True                    # Pass allowDiskUse to aggregate() for large pipelines

    log_exec_field_details: bool = True            # If True, log field-path sets and sample values from exec results
    value_samples_per_field: int = 3               # For each field path, how many sample values to show
    max_logged_fields: int = 50                    # Cap the number of field paths printed to avoid spam
    metric_mode: str = "enhanced"                  # enhanced | tend
    normalize_aggregate_alias_case: bool = True    # Treat generated aggregate aliases case-insensitively in EX/EFM/EVM
    
    analysis_jsonl_path: Path = Path('./results/examples.jsonl')
    summary_bucket_csv_path: Path = Path('./results/summary_by_bucket.csv')
    summary_signature_csv_path: Path = Path('./results/summary_by_signature.csv')
    summary_db_csv_path: Path = Path('./results/summary_by_db.csv')
    write_analysis_outputs: bool = True


def _format_prediction_for_tend_em(s: str) -> str:
    """
    Render predicted MQL closer to the Mongo shell presentation used by TEND
    before applying the original whitespace-normalized EM comparison.

    Prediction-only formatting. The EM comparison remains unchanged.
    """
    s = (s or "").strip()

    if not s:
        return s

    s = s.rstrip().rstrip(";").rstrip()

    is_aggregate = ".aggregate(" in s
    is_find = ".find(" in s

    # Mongo operators are unquoted in TEND gold.
    s = re.sub(
        r'"(\$[A-Za-z_][A-Za-z0-9_]*)"\s*:',
        r'\1:',
        s,
    )

    if is_aggregate:
        # TEND aggregate gold generally uses shell-style unquoted object keys:
        # { $group: { _id: null, count: { $sum: 1 } } }
        s = re.sub(
            r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:',
            r'\1:',
            s,
        )

    elif is_find:
        # TEND find gold usually uses shell-style unquoted field keys in filters,
        # projections, and sort specifications.
        s = re.sub(
            r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:',
            r'\1:',
            s,
        )

    # TEND-style spacing around structural punctuation.
    s = re.sub(r'\s*([{}])\s*', r' \1 ', s)
    s = re.sub(r'\s*\[\s*', '[ ', s)
    s = re.sub(r'\s*\]\s*', ' ]', s)
    s = re.sub(r'\s*,\s*', ', ', s)
    s = re.sub(r'\s*:\s*', ': ', s)
    s = re.sub(r'\s+', ' ', s).strip()

    s = s.replace('{ }', '{}')
    s = s.replace('[ ]', '[]')
    s = s.replace('( {', '({')
    s = s.replace('} )', '})')
    s = s.replace('( [', '([')
    s = s.replace('] )', '])')

    return s + ";"

def _norm_ws_tend(s: str) -> str:
    return re.sub(r'\s+', ' ', (s or '').strip())

def _norm_ws_enhanced(s: str) -> str:
    """Normalize for EM comparison.
    
    Handles the formatting gap between:
      - Gold (MongoDB shell syntax):  { $group: { _id: null } }
      - Pred (Java BasicDBObject):   {"$group": {"_id": null}}
    
    Steps:
      1. Strip surrounding whitespace and trailing semicolons.
      2. Collapse all whitespace runs to a single space.
      3. Strip quotes from JSON object keys (word chars and $ prefix).
      4. Remove all spaces around structural punctuation ([ ] { } , :)
         so both formats become fully compact for comparison.
    """
    s = (s or '').strip().rstrip(';').strip()
    s = re.sub(r'\s+', ' ', s)
    # Strip quotes from keys: "key": → key:  and  "$key": → $key:
    s = re.sub(r'"(\$?[A-Za-z_][A-Za-z0-9_]*)":', r'\1:', s)
    # Remove spaces around structural punctuation to canonicalize both formats
    s = re.sub(r'\s*([{}\[\],:])\s*', r'\1', s)
    return s

def _normalize_query_for_mode(s: str, metric_mode: str) -> str:
    if metric_mode == "tend":
        return _norm_ws_tend(s)
    return _norm_ws_enhanced(s)

def _preview_blob(obj: Any, max_len: int) -> str:
    """Bounded string preview to avoid time-costly pretty-prints."""
    try:
        s = json.dumps(obj, ensure_ascii=False)    # Try to JSON-serialize the object
    except Exception:
        s = str(obj)                               # Fallback to str() if not JSON-serializable
    return s[:max_len] + (' …<truncated>' if len(s) > max_len else '')  # Truncate with ellipsis if too long


def _iter_field_paths(obj, prefix=""):
    """
    Yield dotted field paths (e.g., 'a.b.c') for all nested keys in dicts/lists.
    For lists/tuples we recurse into items without adding numeric indices,
    since indices aren't stable; we care about field *names* only.
    """
    if isinstance(obj, dict):                      # If it's a dict, iterate keys/values
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else k # Build dotted path
            yield key                              # Yield this path
            yield from _iter_field_paths(v, key)   # Recurse into the value with updated prefix
    elif isinstance(obj, (list, tuple)):           # If it's a list/tuple, recurse into elements
        for item in obj:
            yield from _iter_field_paths(item, prefix)  # Do not add numeric index to the path (names only)


def _collect_fields_and_values(results, max_samples=3, max_chars=200):
    """
    From a list of result documents, return:
      - paths: set of dotted field paths across all docs
      - samples: dict[path] -> list of up to `max_samples` stringified sample values
    Values are stringified and truncated to `max_chars` to keep logs compact.
    """
    paths = set()                                  # Accumulate unique field paths
    samples = {}                                   # Map: path -> list of sample stringified values

    def _add_sample(path, val):
        s = _preview_blob(val, max_chars)          # Stringify & truncate each value for logging
        lst = samples.setdefault(path, [])         # Get/create list for this path
        if s not in lst:                           # Avoid duplicate samples
            if len(lst) < max_samples:             # Respect cap per field
                lst.append(s)                      # Add sample

    def _walk(obj, prefix=""):
        if isinstance(obj, dict):                  # Traverse dicts
            for k, v in obj.items():
                key = f"{prefix}.{k}" if prefix else k
                paths.add(key)                     # Record field path
                _add_sample(key, v)                # Record sample value at this path
                _walk(v, key)                      # Recurse into value
        elif isinstance(obj, (list, tuple)):       # Traverse sequences
            for item in obj:
                _walk(item, prefix)                # Keep the same path prefix
        else:
            # leaf value at current prefix (prefix can be empty if root is a scalar)
            if prefix:                             # Only record if we have a path
                _add_sample(prefix, obj)           # Record leaf sample

    # Iterate all docs (handle both a single dict or a list of docs)
    if isinstance(results, (list, tuple)):
        for doc in results:
            _walk(doc, "")
    else:
        _walk(results, "")

    return paths, samples                          # Return the set of paths and sample values


# ----------------------------
# Analysis helpers
# ----------------------------

_AGG_ALIAS_RE = re.compile(
    r'^(count|sum|avg|min|max)(?:_distinct)?(?:_.*)?$',
    re.IGNORECASE
)


def _canonical_result_key(key: Any) -> Any:
    """
    Normalize only generated aggregate output aliases.

    This intentionally does not normalize arbitrary result field names because
    MongoDB field paths are case-sensitive. It only treats aliases such as
    COUNT/count, avg_Capacity/avg_capacity, and SUM_salary/sum_salary as
    equivalent for result comparison.
    """
    if isinstance(key, str) and _AGG_ALIAS_RE.match(key):
        return key.lower()
    return key


def _canonicalize_aggregate_aliases(obj: Any) -> Any:
    """
    Recursively canonicalize generated aggregate alias keys in result documents.
    Values are not changed.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            new_key = _canonical_result_key(k)
            new_val = _canonicalize_aggregate_aliases(v)

            if new_key in out and out[new_key] != new_val:
                # Collision should be rare. Preserve the original key if lowering
                # would merge two different fields.
                out[k] = new_val
            else:
                out[new_key] = new_val

        return out

    if isinstance(obj, (list, tuple)):
        return [_canonicalize_aggregate_aliases(v) for v in obj]

    return obj

def _json_safe(obj: Any) -> Any:
    """Convert values such as ObjectId to JSON-safe strings for JSONL output."""
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return str(obj)


def _short_hash(*parts: str) -> str:
    """Stable short id for grouping duplicate target/prediction pairs."""
    h = hashlib.sha1()
    for part in parts:
        h.update((part or '').encode('utf-8', errors='replace'))
        h.update(b'\0')
    return h.hexdigest()[:12]


def _extract_collection(query: str) -> str:
    """Extract collection from db.<collection>.<op>(...)."""
    m = re.search(r'\bdb\.([A-Za-z0-9_]+)\.(find|aggregate|distinct)\s*\(', query or '')
    return m.group(1) if m else ''

def _normalize_stage_names(stages: List[str]) -> List[str]:
        """Canonicalize stage names for QSM comparison."""
        normalized = []

        for stage in stages or []:
            name = str(stage).strip()

            if name.startswith("$"):
                name = name[1:]

            normalized.append(name)

        return normalized

def _get_qsm_stages(query: str) -> List[str]:
    aggregate_stages = _extract_aggregate_stage_names(query)

    if aggregate_stages:
        return _normalize_stage_names(aggregate_stages)

    parser_stages = get_query_stages(query=query) or []

    if parser_stages:
        return _normalize_stage_names(parser_stages)

    return _normalize_stage_names(_extract_mql_ops_fallback(query))

def _extract_mql_ops_fallback(query: str) -> List[str]:
    """Regex fallback for MQL operators and stages.

    The existing get_query_stages helper sometimes misses quoted stages such as
    {"$group": ...}. This fallback makes the analysis output useful even when
    the custom parser returns an empty list.
    """
    hits = re.findall(r'"\$(\w+)"|\$(\w+)', query or '')
    ops = [a or b for a, b in hits]
    stage_ops = {
        'match', 'project', 'lookup', 'unwind', 'group', 'sort',
        'limit', 'skip', 'count', 'addFields', 'set', 'unset', 'replaceRoot'
    }
    return [op for op in ops if op in stage_ops]


def _signature(collection: str, stages: List[str]) -> str:
    """Compact signature used for grouping failures."""
    stage_part = '>'.join(stages) if stages else 'none'
    return f'{collection}|{stage_part}'


def _safe_len(obj: Any) -> int:
    try:
        return len(obj)
    except Exception:
        return -1


def _classify_failure(metrics: Dict[str, int], detail: Dict[str, Any]) -> str:
    """Assign a coarse failure bucket for triage."""
    if metrics.get('EX') == 1:
        if metrics.get('EM') == 1:
            return 'correct_exact_match'
        return 'correct_execution_not_exact'

    if detail.get('execution_error'):
        return 'execution_error'

    pred_count = detail.get('prediction_result_count', -1)
    target_count = detail.get('target_result_count', -1)
    missing = detail.get('missing_result_fields', [])
    extra = detail.get('extra_result_fields', [])

    if pred_count == 0 and target_count > 0:
        return 'empty_prediction_result'
    if target_count == 0 and pred_count > 0:
        return 'extra_prediction_result'
    if missing and extra:
        return 'result_field_name_mismatch'
    if missing:
        return 'missing_prediction_fields'
    if extra:
        return 'extra_prediction_fields'
    if metrics.get('QSM') == 1 and metrics.get('QFC') == 1 and metrics.get('EFM') == 1:
        return 'same_shape_fields_wrong_values'
    if metrics.get('QFC') == 1 and metrics.get('EFM') == 1:
        return 'same_fields_wrong_values'
    if metrics.get('QSM') == 0:
        return 'stage_mismatch'
    if metrics.get('QFC') == 0:
        return 'query_field_mismatch'
    return 'other_execution_mismatch'


@contextmanager
def timer(name: str):
    """Context timer used around the main loop."""
    t0 = time.time()                               # Capture start wall-time
    yield                                          # Execute the context block
    print(f"{name} took {time.time() - t0:.2f} seconds")  # On exit, print elapsed time


# ----------------------------
# Timing decorators (profiling)
# ----------------------------

_TIMINGS = defaultdict(float)                      # Accumulate per-label timing across calls

def timed(label: str):
    """Decorator to accumulate time spent in labeled sections."""
    def deco(fn):
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()               # High-resolution timer start
            out = fn(*args, **kwargs)              # Invoke wrapped function
            _TIMINGS[label] += time.perf_counter() - t0  # Add elapsed to label bucket
            return out                             # Return original result
        return wrapper
    return deco


# ----------------------------
# PyMongo fast-path parsing
# ----------------------------

# Matches: db.collection.aggregate([...])
AGG_RE = re.compile(
    r'^db\.([A-Za-z0-9_]+)\.aggregate\(\s*(\[.*\])\s*\)\s*;?\s*$',
    re.DOTALL                                    # DOTALL so '.' matches newlines (pipelines often span lines)
)

# Matches: db.collection.find({filter}[, {projection}])
FIND_RE = re.compile(
    r'^db\.([A-Za-z0-9_]+)\.find\(\s*'          # Start with db.<coll>.find(
    r'(\{.*?\})'                                 # Capture group 1: filter object (non-greedy)
    r'(?:\s*,\s*(\{.*?\}))?'                     # Optional group 2: projection object (non-greedy)
    r'\s*\)\s*;?\s*$',                           # Close paren, optional semicolon, end
    re.DOTALL
)

# Matches: db.collection.find({filter}[, {projection}]).count()
FIND_COUNT_RE = re.compile(
    r'^db\.([A-Za-z0-9_]+)\.find\(\s*'          # Start with db.<coll>.find(
    r'(\{.*?\})'                                 # Capture group 1: filter object
    r'(?:\s*,\s*(\{.*?\}))?'                     # Optional group 2: projection object
    r'\s*\)\.count\(\)\s*;?\s*$',               # .count() at the end
    re.DOTALL
)

# Matches: db.collection.distinct("field"[, {filter}])
DISTINCT_RE = re.compile(
    r'^db\.([A-Za-z0-9_]+)\.distinct\(\s*'      # Start with db.<coll>.distinct(
    r'"([^"]+)"'                                 # Capture group 1: field name (quoted string)
    r'(?:\s*,\s*(\{.*?\}))?'                     # Optional group 2: filter object
    r'\s*\)\s*;?\s*$',                           # Close paren, optional semicolon, end
    re.DOTALL
)


def _maybe_json_load(text: str):
    """Try json.loads, then demjson as a fallback for non-strict JSON."""
    try:
        return json.loads(text)                   # Strict JSON first (fastest/cleanest)
    except json.JSONDecodeError:
        return demjson.decode(text)               # Fallback: demjson handles lenient JSON


def _try_parse_aggregate(mql: str):
    """Parse 'db.coll.aggregate([...])' → (collection, pipeline:list) or (None, None)."""
    m = AGG_RE.match(mql.strip())                 # Try to match aggregate() shape
    if not m:
        return None, None                         # Not an aggregate call
    coll = m.group(1)                             # Extract collection name
    pipeline_text = m.group(2)                    # Extract raw pipeline text (JSON array)
    try:
        pipeline = _maybe_json_load(pipeline_text)# Parse pipeline text to Python list/dicts
        if not isinstance(pipeline, list):        # Validate list
            return None, None
        return coll, pipeline                     # Success: return collection & pipeline
    except Exception:
        return None, None                         # On any parse error, signal failure


def _try_parse_find(mql: str):
    """Parse 'db.coll.find({...}, {...})' → (collection, filter:dict, proj:dict|None) or (None, None, None)."""
    m = FIND_RE.match(mql.strip())                # Try to match find() shape
    if not m:
        return None, None, None                   # Not a find call
    coll = m.group(1)                             # Extract collection name
    filter_text = m.group(2)                      # Extract filter JSON
    proj_text = m.group(3)                        # Extract optional projection JSON
    try:
        filt = _maybe_json_load(filter_text)      # Parse filter to dict
        proj = _maybe_json_load(proj_text) if proj_text else None  # Parse projection if present
        if not isinstance(filt, dict):            # Validate filter is dict
            return None, None, None
        if proj is not None and not isinstance(proj, dict):  # Validate projection if present
            proj = None
        return coll, filt, proj                   # Success: return parsed pieces
    except Exception:
        return None, None, None                   # On parse error, signal failure
  
def _sql_requires_ordered_output(sql: str) -> bool:
    """Return True if the gold SQL explicitly requires ordered output."""
    if not sql:
        return False

    return re.search(r'\border\s+by\b', sql, flags=re.IGNORECASE) is not None


def _extract_aggregate_stage_names(mql: str) -> List[str]:
    """Extract top-level aggregate stage names in order from an MQL aggregate pipeline."""
    if not mql:
        return []

    coll, pipeline = _try_parse_aggregate(mql)

    if not coll or not isinstance(pipeline, list):
        return []

    stages = []

    for stage in pipeline:
        if isinstance(stage, dict) and len(stage) == 1:
            stages.append(next(iter(stage.keys())))

    return stages


def _mql_requires_ordered_output(mql: str) -> bool:
    """
    Return True if the gold MQL has an observable final ordering requirement.

    A find(...).sort(...) is ordered.

    For aggregate([...]), the last $sort defines output order only if all later
    stages preserve ordering. A later $group, $lookup, or $unwind can change the
    output stream enough that the earlier sort should not be treated as final
    result ordering.
    """
    if not mql:
        return False

    if re.search(r'\.find\s*\(.*\)\s*\.sort\s*\(', mql, flags=re.DOTALL):
        return True

    stages = _extract_aggregate_stage_names(mql)

    if not stages:
        return False

    last_sort_idx = -1

    for idx, stage in enumerate(stages):
        if stage == "$sort":
            last_sort_idx = idx

    if last_sort_idx < 0:
        return False

    order_preserving_after_sort = {
        "$project",
        "$match",
        "$limit",
        "$skip",
        "$addFields",
        "$set",
        "$unset",
    }

    for stage in stages[last_sort_idx + 1:]:
        if stage not in order_preserving_after_sort:
            return False

    return True


def _requires_ordered_output(sql_gold: str, mql_gold: str) -> bool:
    """
    Gold SQL is the primary signal because ORDER BY is the user-visible semantic.
    Gold MQL is a fallback for cases where the SQL text is unavailable or where
    the gold MQL encodes a required final sort.
    """
    sql_ordered = _sql_requires_ordered_output(sql_gold)
    mql_ordered = _mql_requires_ordered_output(mql_gold)

    if _sql_requires_ordered_output(sql_gold):
        return True

    return _mql_requires_ordered_output(mql_gold)



def extract_final_sort_spec(mql: str):

    coll, pipeline = _try_parse_aggregate(mql)

    if coll and isinstance(pipeline, list):
        last_sort = None

        for idx, stage in enumerate(pipeline):

            if isinstance(stage, dict) and "$sort" in stage:
                last_sort = stage["$sort"]
            elif last_sort is not None:
                preserving = {
                    "$project",
                    "$match",
                    "$limit",
                    "$skip",
                    "$addFields",
                    "$set",
                    "$unset",
                }

                is_preserving = any(k in preserving for k in stage.keys()) if isinstance(stage, dict) else False

                if not is_preserving:                    
                    last_sort = None


        if isinstance(last_sort, dict):
            spec = [(str(k), int(v)) for k, v in last_sort.items()]
            return spec

    m = re.search(r'\.sort\s*\(\s*(\{.*?\})\s*\)', mql or '', flags=re.DOTALL)

    if m:
        try:
            spec = _maybe_json_load(m.group(1))

            if isinstance(spec, dict):
                final_spec = [(str(k), int(v)) for k, v in spec.items()]
                return final_spec
        except Exception as e:
            return []

    return []

def get_result_path_value(doc, path: str):
    cur = doc

    for part in path.split("."):
        if not isinstance(cur, dict):
            return None

        cur = cur.get(part)

    return cur

def ordered_equal_allowing_sort_ties(gold_result, pred_result, sort_spec, set_equal_fn):

    if not isinstance(gold_result, list) or not isinstance(pred_result, list):
        return False

    if len(gold_result) != len(pred_result):
        return False

    if not sort_spec:
        return False

    def sort_key(doc):
        return tuple(get_result_path_value(doc, field) for field, _direction in sort_spec)

    start = 0

    while start < len(gold_result):
        key = sort_key(gold_result[start])
        end = start + 1

        while end < len(gold_result) and sort_key(gold_result[end]) == key:
            end += 1

        gold_group = gold_result[start:end]
        pred_group = pred_result[start:end]

        if any(sort_key(doc) != key for doc in pred_group):            
            return False

        group_equal = set_equal_fn(gold_group, pred_group)

        if not group_equal:
            return False

        start = end

    return True

# ----------------------------
# Core comparator
# ----------------------------

class QueryComparator:
    """Compares two MQL strings with structural and execution-based metrics."""

    def __init__(self, config: MetricConfig):
        self.config = config                      # Keep config for later use
        # One MongoClient for the whole run — avoids reconnect costs per example.
        self.client = MongoClient(config.mongodb_uri)
        # Keep your shell executor, but we only use it as a fallback.
        self.executor = MongoShellExecutor()

    # Freeze/thaw utilities make cached results hashable for lru_cache
    @staticmethod
    def _freeze(obj: Any):
        if isinstance(obj, dict):                 # For dict: convert to sorted tuple of (key, frozen(value))
            return tuple(sorted((k, QueryComparator._freeze(v)) for k, v in obj.items()))
        if isinstance(obj, list):                 # For list: convert to tuple of frozen elements
            return tuple(QueryComparator._freeze(v) for v in obj)
        if isinstance(obj, tuple):                # For tuple: recursively freeze items
            return tuple(QueryComparator._freeze(v) for v in obj)
        return obj                                # For scalars: return as-is

    @staticmethod
    def _thaw(obj: Any):
        if isinstance(obj, tuple):                # If tuple, it might represent dict or list
            # Heuristic: dict-like if members are (str, value) pairs
            if all(isinstance(i, tuple) and len(i) == 2 and isinstance(i[0], str) for i in obj):
                return {k: QueryComparator._thaw(v) for k, v in obj}  # Convert back to dict
            return [QueryComparator._thaw(v) for v in obj]            # Else convert back to list
        return obj                                # Scalars unchanged

    def _norm_cache_key(self, db_id: str, query: str) -> str:
        # Use normalized whitespace to de-duplicate semantically identical query text.       
        return f"{db_id}||{_norm_ws_enhanced(query)}"   
    
    @lru_cache(maxsize=10000)
    @timed("exec_tend")
    def _cached_exec_tend(self, db_id: str, query: str) -> tuple:
        result = self.executor.execute_query(db_id, query)

        if isinstance(result, str):
            result = result.replace('"""', '"')
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                try:
                    result = demjson.decode(result)
                except Exception:
                    print(f"Warning: Unable to parse result for query: {query}")
                    result = []

        return QueryComparator._freeze(result)

    @lru_cache(maxsize=10000)
    @timed("exec")                                # Measure time spent executing (fast-path or shell)
    def _cached_exec(self, cache_key: str) -> tuple:
        """
        Cached execution path that:
        1) Tries PyMongo fast path for aggregate/find.
        2) Falls back to mongosh for anything else.
        Returns a FROZEN (hashable) structure to fit lru_cache.
        """
        db_id, query = cache_key.split("||", 1)   # Split cache key back into db + query
        db = self.client[db_id]                   # Get DB handle from global client

        # --- Fast path #1: aggregate([...]) ---
        coll, pipeline = _try_parse_aggregate(query)
        if coll and pipeline is not None:         # If it parses as aggregate()
            try:
                docs = list(db[coll].aggregate(   # Run natively with PyMongo (fast)
                    pipeline,
                    allowDiskUse=self.config.allow_disk_use
                ))
                return QueryComparator._freeze(docs)  # Freeze results for caching
            except Exception:
                # Fall through to shell if pipeline/parse failed at runtime
                pass

        # --- Fast path #2: find(filter, projection).count() ---
        m = FIND_COUNT_RE.match(query.strip())
        if m:
            try:
                coll_name = m.group(1)
                filt = _maybe_json_load(m.group(2))
                count = db[coll_name].count_documents(filt if isinstance(filt, dict) else {})
                # Return as [{"count": N}] to match aggregate COUNT(*) format
                return QueryComparator._freeze([{"count": count}])
            except Exception:
                pass

        # --- Fast path #3: distinct("field", filter) ---
        m = DISTINCT_RE.match(query.strip())
        if m:
            try:
                coll_name = m.group(1)
                field_name = m.group(2)
                filt_text = m.group(3)
                filt = _maybe_json_load(filt_text) if filt_text else {}
                values = db[coll_name].distinct(field_name, filt if isinstance(filt, dict) else {})
                # Return as [{"field": val}, ...] to match aggregate DISTINCT format
                # Extract the bare field name (e.g., "wine.Winery" -> "Winery")
                bare_field = field_name.split(".")[-1]
                docs = [{bare_field: v} for v in values]
                return QueryComparator._freeze(docs)
            except Exception:
                pass

        # --- Fast path #4: find(filter, projection) ---
        coll, filt, proj = _try_parse_find(query)
        if coll and filt is not None:             # If it parses as find()
            try:
                cur = db[coll].find(filt, proj)   # Run natively with PyMongo
                docs = list(cur)                  # Materialize cursor
                return QueryComparator._freeze(docs)
            except Exception:
                # Fall through to shell if something is unsupported
                pass

        # --- Fallback: use mongosh executor (likely slower) ---
        result = self.executor.execute_query(db_id, query)  # Use shell-based executor
        if isinstance(result, str):               # If result is a raw string, attempt to parse into Python objects
            result = result.replace('"""', '"')   # Normalize triple quotes if present
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                try:
                    result = demjson.decode(result)  # Lenient fallback
                except Exception:
                    result = []                   # If parsing fails, default to empty

        return QueryComparator._freeze(result)    # Freeze before returning to satisfy lru_cache
    
    def _get_query_result(self, db_id: str, query: str) -> List[Dict]:
        if self.config.metric_mode == "tend":
            return self._get_query_result_tend(db_id, query)

        frozen = self._cached_exec(self._norm_cache_key(db_id, query))
        return QueryComparator._thaw(frozen)

    def _get_query_result_tend(self, db_id: str, query: str) -> List[Dict]:
        frozen = self._cached_exec_tend(db_id, query)
        return QueryComparator._thaw(frozen)
    
    @staticmethod
    def _deep_equal(a: Any, b: Any) -> bool:
        """Deep equality for nested dicts/lists/tuples."""
        if isinstance(a, dict) and isinstance(b, dict):
            if set(a.keys()) != set(b.keys()):      # Dict keys must match
                return False
            return all(QueryComparator._deep_equal(a[k], b[k]) for k in a)  # Recurse per key
        if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
            if len(a) != len(b):                    # Lengths must match
                return False
            return all(QueryComparator._deep_equal(x, y) for x, y in zip(a, b))  # Recurse per element
        return a == b                                # Scalar equality

    @staticmethod
    def _set_equal(a: Any, b: Any) -> bool:
        """Order-insensitive comparison for result lists.
        Sorts both lists by their frozen representation and compares element-wise.
        Handles cases where queries return the same documents in different order."""
        if not isinstance(a, (list, tuple)) or not isinstance(b, (list, tuple)):
            return QueryComparator._deep_equal(a, b)
        if len(a) != len(b):
            return False
        if len(a) == 0:
            return True
        try:
            # Freeze each document for hashable comparison, then sort
            frozen_a = sorted(QueryComparator._freeze(doc) for doc in a)
            frozen_b = sorted(QueryComparator._freeze(doc) for doc in b)
            return frozen_a == frozen_b
        except TypeError:
            # If sorting fails (unhashable/uncomparable types), fall back
            return False

    def compare(self, query_gold: str, query_pred: str, db_id: str, return_details: bool = False, sql_gold: str = ""):
        """Compute all metrics for a pair of MQL strings on the given db.        
        - When return_details=True, return (metrics, detail) where detail is a
          JSON-serializable record with normalized queries, parser output,
          execution previews, field differences, signatures, and failure bucket.
        - When return_details=False, preserve metric2.py behavior and return
          just the metrics dict.
        """
        metrics = {m: 0 for m in self.config.metrics_list}
        detail: Dict[str, Any] = {
            'db_id': db_id,
            'target_query': query_gold,
            'prediction_query': query_pred,
            'target_norm': _normalize_query_for_mode(query_gold, self.config.metric_mode),
            'prediction_norm': (_norm_ws_tend(_format_prediction_for_tend_em(query_pred)) if self.config.metric_mode == "tend" else _norm_ws_enhanced(query_pred)),
            'target_collection': _extract_collection(query_gold),
            'prediction_collection': _extract_collection(query_pred),            
            'execution_error': '',
            'metric_mode': self.config.metric_mode,
            'expected_ordered_output': _requires_ordered_output(sql_gold, query_gold),
            'strict_result_equal': False,
            'unordered_result_equal': False,
            'order_sensitive_comparison': False,
        }

        print("\n" + "=" * 60)
        print(f"[DB: {db_id}]")
        print(f"TARGET QUERY:\n{query_gold}")
        print(f"PREDICTION QUERY:\n{query_pred}")

        # -------- EM: exact string match after whitespace normalization --------
        metrics['EM'] = int(detail['target_norm'] == detail['prediction_norm'])
        print(f"Target query after normalization: {detail['target_norm']}")
        print(f"Prediction query after normalization: {detail['prediction_norm']}")
        print(f"Exact Match (EM): {bool(metrics['EM'])}")

        # -------- QSM: stage sequence equality --------
        stages1, stages2 = [], []
        stages1_regex = _extract_mql_ops_fallback(query_gold)
        stages2_regex = _extract_mql_ops_fallback(query_pred)
        try:
            stages1 = get_query_stages(query=query_gold)
            stages2 = get_query_stages(query=query_pred)
            print(f"TARGET stages: {stages1}")
            print(f"PREDICT stages: {stages2}")
            # Original comparison that does not handle if get_query_stages returns None or empty lists.
            # metrics['QSM'] = int(stages1 == stages2)
            qsm_stages1 = _get_qsm_stages(query_gold)
            qsm_stages2 = _get_qsm_stages(query_pred)

            metrics['QSM'] = int(bool(qsm_stages1) and bool(qsm_stages2) and qsm_stages1 == qsm_stages2)
        except Exception as e:
            print(f"QSM error for {db_id}: {e}")
            metrics['QSM'] = 0

        detail.update({
            'target_stages': stages1,
            'prediction_stages': stages2,
            'target_stages_regex': stages1_regex,
            'prediction_stages_regex': stages2_regex,
            # Prefer parser stages when non-empty; fall back to regex stages for grouping.
            'target_signature': _signature(detail['target_collection'], stages1 or stages1_regex),
            'prediction_signature': _signature(detail['prediction_collection'], stages2 or stages2_regex),
        })

        # -------- QFC: field coverage set equality --------
        fields1, fields2 = [], []
        try:
            fields1 = extract_fields(MQL=query_gold, db_name=db_id)
            fields2 = extract_fields(MQL=query_pred, db_name=db_id)
            print(f"TARGET fields: {fields1}")
            print(f"PREDICT fields: {fields2}")
            metrics['QFC'] = int(set(fields1) == set(fields2))
        except Exception as e:
            qfc_error = str(e)
            print(f"QFC error for {db_id}: {qfc_error}")
            metrics['QFC'] = 0

        detail.update({
            'target_query_fields': sorted(set(fields1)),
            'prediction_query_fields': sorted(set(fields2)),
            'missing_query_fields': sorted(set(fields1) - set(fields2)),
            'extra_query_fields': sorted(set(fields2) - set(fields1)),
        })

        # -------- Execution-based metrics: EX, EFM, EVM --------
        result_gold, result_pred = [], []
        paths_gold, paths_pred = set(), set()
        try:
            result_gold = self._get_query_result(db_id, query_gold)
            pred_start = time.perf_counter()
            result_pred = self._get_query_result(db_id, query_pred)
            pred_elapsed = time.perf_counter() - pred_start

            if pred_elapsed >= 2.0:
                tqdm.write(f"\nSlow predicted query: " f"db_id={db_id}, " f"elapsed={pred_elapsed:.2f}s, " f"query={query_pred}")

            if (self.config.metric_mode == "enhanced" and self.config.normalize_aggregate_alias_case):
                result_gold = _canonicalize_aggregate_aliases(result_gold)
                result_pred = _canonicalize_aggregate_aliases(result_pred)   

            print(f"TARGET result (preview):   {_preview_blob(result_gold, self.config.preview_chars)}")
            print(f"PREDICT result (preview):  {_preview_blob(result_pred, self.config.preview_chars)}")

            paths_gold, samples_gold = _collect_fields_and_values(
                result_gold,
                max_samples=self.config.value_samples_per_field,
                max_chars=self.config.preview_chars // 6
            )
            paths_pred, samples_pred = _collect_fields_and_values(
                result_pred,
                max_samples=self.config.value_samples_per_field,
                max_chars=self.config.preview_chars // 6
            )

            missing_in_pred = sorted(paths_gold - paths_pred)
            extra_in_pred = sorted(paths_pred - paths_gold)
            shared_paths = sorted(paths_gold & paths_pred)

            if self.config.log_exec_field_details:
                print(f"TARGET field path count:  {len(paths_gold)}")
                print(f"PREDICT field path count: {len(paths_pred)}")
                max_fields = self.config.max_logged_fields

                if missing_in_pred:
                    print(f"Missing in PREDICT ({min(len(missing_in_pred), max_fields)} shown):")
                    for pth in missing_in_pred[:max_fields]:
                        print(f"  - {pth}")

                if extra_in_pred:
                    print(f"Extra in PREDICT ({min(len(extra_in_pred), max_fields)} shown):")
                    for pth in extra_in_pred[:max_fields]:
                        print(f"  + {pth}")

                if shared_paths:
                    print(f"Sample values for shared fields ({min(len(shared_paths), max_fields)} shown):")
                    for pth in shared_paths[:max_fields]:
                        g_vals = samples_gold.get(pth, [])
                        p_vals = samples_pred.get(pth, [])
                        print(f"  {pth}:")
                        if g_vals:
                            print(f"    TARGET samples : {g_vals}")
                        if p_vals:
                            print(f"    PREDICT samples: {p_vals}")

            # EX: strict equality first. In enhanced mode, only allow unordered
            # result-set equality when the gold query does not require ordered output.
            strict_result_equal = self._deep_equal(result_gold, result_pred)
            detail['strict_result_equal'] = bool(strict_result_equal)

            if self.config.metric_mode == "tend":
                detail['order_sensitive_comparison'] = True
                metrics['EX'] = int(strict_result_equal)
            else:
                if strict_result_equal:
                    metrics['EX'] = 1
                elif detail.get('expected_ordered_output'):
                    detail['order_sensitive_comparison'] = True

                    sort_spec = extract_final_sort_spec(query_gold)
                    detail['expected_sort_spec'] = sort_spec

                    tie_aware_equal = ordered_equal_allowing_sort_ties(
                        result_gold,
                        result_pred,
                        sort_spec,
                        self._set_equal
                    )

                    detail['tie_aware_ordered_result_equal'] = bool(tie_aware_equal)
                    metrics['EX'] = int(tie_aware_equal)
                else:
                    unordered_result_equal = self._set_equal(result_gold, result_pred)
                    detail['unordered_result_equal'] = bool(unordered_result_equal)
                    metrics['EX'] = int(unordered_result_equal)

            # EFM/EVM: field sets and value equality across aligned documents.
            fields_gold, fields_pred = set(), set()
            metrics['EFM'] = 1
            metrics['EVM'] = 1

            def collect_fields(d: Any, acc: set):
                if isinstance(d, dict):
                    for k, v in d.items():
                        acc.add(k)
                        collect_fields(v, acc)
                elif isinstance(d, (list, tuple)):
                    for it in d:
                        collect_fields(it, acc)

            for g, p in zip(result_gold, result_pred):
                collect_fields(g, fields_gold)
                collect_fields(p, fields_pred)
                if not self._deep_equal(g, p):
                    metrics['EVM'] = 0

            if fields_gold != fields_pred:
                metrics['EFM'] = 0

            detail.update({
                'target_result_count': _safe_len(result_gold),
                'prediction_result_count': _safe_len(result_pred),
                'target_result_preview': _preview_blob(result_gold, self.config.preview_chars),
                'prediction_result_preview': _preview_blob(result_pred, self.config.preview_chars),
                'target_result_fields': sorted(paths_gold),
                'prediction_result_fields': sorted(paths_pred),
                'missing_result_fields': missing_in_pred,
                'extra_result_fields': extra_in_pred,
                'shared_result_fields': shared_paths,
                'sample_values': _json_safe({
                    pth: {
                        'target': samples_gold.get(pth, []),
                        'prediction': samples_pred.get(pth, [])
                    }
                    for pth in shared_paths[:self.config.max_logged_fields]
                }),
            })

        except Exception as e:
            print(f"Execution error for {db_id}: {e}")
            metrics['EX'] = metrics['EFM'] = metrics['EVM'] = 0
            detail.update({
                'execution_error': str(e),
                'target_result_count': _safe_len(result_gold),
                'prediction_result_count': _safe_len(result_pred),
                'target_result_preview': _preview_blob(result_gold, self.config.preview_chars),
                'prediction_result_preview': _preview_blob(result_pred, self.config.preview_chars),
                'target_result_fields': sorted(paths_gold),
                'prediction_result_fields': sorted(paths_pred),
                'missing_result_fields': [],
                'extra_result_fields': [],
                'shared_result_fields': [],
                'sample_values': {},
            })

        detail['metrics'] = dict(metrics)
        detail['metric_pattern'] = ''.join(str(int(metrics.get(m, 0))) for m in self.config.metrics_list)
        detail['query_pair_id'] = _short_hash(db_id, detail['target_norm'], detail['prediction_norm'])
        detail['target_query_id'] = _short_hash(db_id, detail['target_norm'])
        detail['failure_bucket'] = _classify_failure(metrics, detail)

        print(f"Final metrics: {metrics}")
        print(f"Failure bucket: {detail['failure_bucket']}")
        print("=" * 60 + "\n")

        if return_details:
            return metrics, detail
        return metrics


# Wrap heavy helpers with timers so you can see breakdowns
get_query_stages = timed("qsm")(get_query_stages)        # Wrap stage-extractor to time its total cost
extract_fields = timed("qfc")(extract_fields)            # Wrap field-extractor to time its total cost


# ----------------------------
# Aggregator
# ----------------------------

class AccuracyCalculator:
    """Aggregates metrics and writes analysis-grade JSONL/CSV output."""

    def __init__(self, config: MetricConfig):
        self.config = config
        self.comparator = QueryComparator(config)

    def _format_example(self, example: Dict, acc: Dict) -> Dict:
        """Minimal info for wrong-case logging."""
        return {
            "NLQ": example['NLQ'],
            "db_id": example['db_id'],
            "prediction": example['prediction'],
            "target": example['target'],
            "flag": acc['EX'] == 1
        }

    def _format_metrics_string(self, metrics: Dict[str, float]) -> str:
        return f"""
    Exact Match (EM): {metrics['EM']}
    Query Stages Match (QSM): {metrics['QSM']}
    Query Fields Coverage (QFC): {metrics['QFC']}
    Execution Accuracy (EX): {metrics['EX']}
    Execution Fields Match (EFM): {metrics['EFM']}
    Execution Value Match (EVM): {metrics['EVM']}
"""

    def _save_wrong_examples(self, wrong_examples: List[Dict]):
        self.config.wrong_examples_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config.wrong_examples_path, "w", encoding="utf-8") as f:
            json.dump(wrong_examples, f, indent=2, ensure_ascii=False)

    def _write_jsonl(self, records: List[Dict[str, Any]]):
        path = self.config.analysis_jsonl_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            for rec in records:
                f.write(json.dumps(_json_safe(rec), ensure_ascii=False, sort_keys=True) + '\n')

    def _write_counter_csv(self, path: Path, counter: Counter, header: List[str]):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(header)
            for key, count in counter.most_common():
                if not isinstance(key, tuple):
                    key = (key,)
                writer.writerow(list(key) + [count])

    def _write_summary_csvs(self, records: List[Dict[str, Any]]):
        by_bucket = Counter()
        by_signature = Counter()
        by_db = Counter()

        for rec in records:
            m = rec.get('metrics', {})
            passed = int(m.get('EX', 0) == 1)
            failed = 1 - passed
            bucket = rec.get('failure_bucket', 'unknown')
            by_bucket[(bucket, passed, failed)] += 1
            by_signature[(
                bucket,
                rec.get('target_signature', ''),
                rec.get('prediction_signature', ''),
                passed,
                failed,
            )] += 1
            by_db[(rec.get('db_id', ''), bucket, passed, failed)] += 1

        self._write_counter_csv(
            self.config.summary_bucket_csv_path,
            by_bucket,
            ['failure_bucket', 'passed_EX', 'failed_EX', 'count']
        )
        self._write_counter_csv(
            self.config.summary_signature_csv_path,
            by_signature,
            ['failure_bucket', 'target_signature', 'prediction_signature', 'passed_EX', 'failed_EX', 'count']
        )
        self._write_counter_csv(
            self.config.summary_db_csv_path,
            by_db,
            ['db_id', 'failure_bucket', 'passed_EX', 'failed_EX', 'count']
        )

    def calculate(
        self,
        examples: List[Dict],
        need_print: bool = False,
        need_save: bool = False,
        need_analysis: bool = True
    ) -> Tuple[Dict[str, float], str]:

        metrics_sum = {metric: 0 for metric in self.config.metrics_list}
        wrong_examples = []
        analysis_records: List[Dict[str, Any]] = []
        total = len(examples)

        with timer("Processing examples"):
            for idx, ex in enumerate(tqdm(examples, desc="Processing examples", file=sys.__stdout__)):
                try:                
                    return_details = need_analysis and self.config.write_analysis_outputs

                    result = self.comparator.compare(
                        ex['target'],
                        ex['prediction'],
                        ex['db_id'],
                        return_details=return_details,
                        sql_gold=ex.get('SQL', ''),
                    )

                    if return_details:
                        ex_metrics, detail = result
                    else:
                        ex_metrics = result
                        detail = None

                    for k, v in ex_metrics.items():
                        metrics_sum[k] += 1 if v else 0

                    if ex_metrics.get('EX', 1) == 0:
                        wrong_examples.append(self._format_example(ex, ex_metrics))

                    if detail is not None:
                        detail.update({
                            'example_id': idx,
                            'NLQ': ex.get('NLQ', ''),
                            'source_sql': ex.get('SQL_pred', ''),
                            'source_gold_sql': ex.get('SQL', ''),
                        })
                        analysis_records.append(detail)

                except Exception as e:
                    print(f"\nExample error on db_id={ex.get('db_id')}: {e}\n")
                    failed_metrics = {m: 0 for m in self.config.metrics_list}
                    failed_detail = {
                        'example_id': idx,
                        'db_id': ex.get('db_id', ''),
                        'NLQ': ex.get('NLQ', ''),
                        'SQL': ex.get('SQL', ''),
                        'SQL_pred': ex.get('SQL_pred', ''),
                        'target_query': ex.get('target', ''),
                        'prediction_query': ex.get('prediction', ''),
                        'metrics': failed_metrics,
                        'failure_bucket': 'metric_exception',
                        'execution_error': str(e),
                        'metric_mode': self.config.metric_mode,
                    }
                    analysis_records.append(failed_detail)

        metrics_mean = {
            k: (metrics_sum[k] / total if total else 0.0)
            for k in self.config.metrics_list
        }

        acc_str = self._format_metrics_string(metrics_mean)

        if need_print:
            print(acc_str)
            if wrong_examples:
                print(f"\nTotal errors: {len(wrong_examples)} out of {total} examples")

            if _TIMINGS:
                print("\nTiming breakdown (seconds):")
                for k, v in sorted(_TIMINGS.items()):
                    print(f"  {k:>6}: {v:.3f}")

        if need_save:
            self._save_wrong_examples(wrong_examples)

        if need_analysis and self.config.write_analysis_outputs:
            self._write_jsonl(analysis_records)
            self._write_summary_csvs(analysis_records)
            if need_print:
                print(f"\nAnalysis JSONL saved to: {self.config.analysis_jsonl_path}")
                print(f"Summary by bucket saved to: {self.config.summary_bucket_csv_path}")
                print(f"Summary by signature saved to: {self.config.summary_signature_csv_path}")
                print(f"Summary by database saved to: {self.config.summary_db_csv_path}")

        return metrics_mean, acc_str


if __name__ == "__main__":      
    script_start = time.perf_counter()

    # Parse command-line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no_analysis",
        action="store_true",
        help="Disable JSONL/CSV analysis outputs."
    )
    parser.add_argument(
        "--file_name",
        default="sample",  # default value if none is given
        help="Base name of results file (without .json) inside results/"
    )
    parser.add_argument(
        "--metric-mode",
        choices=["enhanced", "tend"],
        default="enhanced",
        help="Metric semantics to use. enhanced is the default; tend reproduces the original TEND metric behavior."
    )
    args = parser.parse_args()

    file_name = args.file_name                               # Base name for input/output paths
    script_dir = Path(__file__).resolve().parent
    results_dir = script_dir / "results"    
    results_dir.mkdir(parents=True, exist_ok=True)
    predictions_path = results_dir / f"{file_name}.json"
    log_path = results_dir / f"{file_name}_metrics.log"

    with open(log_path, "w", encoding="utf-8") as log_file:  # Open log file for writing
        with redirect_stdout(log_file), redirect_stderr(log_file):  # Redirect both stdout/stderr to file
            print(f"File name: {file_name}")                 # First line in the log

            config = MetricConfig(                           # Build config with desired knobs    
                metric_mode=args.metric_mode,
                wrong_examples_path=results_dir / f"{file_name}_wrong_examples.json",
                preview_chars=1500,                          # Limit preview size
                allow_disk_use=True,                         # Allow large aggregations
                analysis_jsonl_path=results_dir / f"{file_name}_examples.jsonl",
                summary_bucket_csv_path=results_dir / f"{file_name}_summary_by_bucket.csv",
                summary_signature_csv_path=results_dir / f"{file_name}_summary_by_signature.csv",
                summary_db_csv_path=results_dir / f"{file_name}_summary_by_db.csv",
                write_analysis_outputs=not args.no_analysis,
            )

            calculator = AccuracyCalculator(config)          # Create calculator instance

            with open(predictions_path, 'r', encoding='utf-8') as f:  # Read the predictions dataset
                predictions = json.load(f)

            # Re-map to the evaluator's expected structure
            # Each example requires: db_id, NLQ, target (gold MQL), prediction (pred MQL)
            results = [{                                       # Build list of evaluation examples
                "db_id": ex['db_id'],                          # Database identifier
                "NLQ": ex.get('nlq', ''),                      # Natural-language query (optional)
                "SQL": ex.get('SQL', ''),
                "SQL_pred": ex.get('SQL_pred', ''),
                "target": ex['MQL'],                           # Gold Mongo pipeline (string)
                "prediction": ex['MQL_pred'],                  # Predicted Mongo pipeline (string)
            } for ex in predictions]

            # Run metrics (and print summary to the log file)
            metric, metric_str = calculator.calculate(results, need_print=True, need_save=True, need_analysis=not args.no_analysis)

    elapsed = time.perf_counter() - script_start

    print(f"Log saved to {log_path}")
    if not args.no_analysis:
        print(f"Analysis files saved under ./results/ for {file_name}")

    print(f"Total runtime: {elapsed:.2f} seconds ({elapsed / 60:.2f} minutes)")

    # Append runtime to the log file
    with open(log_path, "a", encoding="utf-8") as log_file:
        print(f"\nTotal runtime: {elapsed:.2f} seconds ({elapsed / 60:.2f} minutes)", file=log_file)