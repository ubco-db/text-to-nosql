#!/usr/bin/env python3
"""
Preprocess SQL before sending to the translator.

Fixes known SQL output issues:
  1. Double-quoted string literals -> single-quoted values.
     LLMs often produce WHERE col = "value" instead of WHERE col = 'value'.
     The parser treats "..." as identifiers, not string literals.

  2. Bare reserved-word column names -> quoted identifiers.
     For example, Rank is tokenised as the SQL:2003 RANK() keyword unless quoted.

Supported input formats:
  1. Canonical JSON array:
     [
       {
         "count": 1,
         "record_id": "1861_1",
         "db_id": "school_bus",
         "nlq": "...",
         "SQL": "gold SQL",
         "SQL_pred": "predicted SQL",
         "MQL": "gold MongoDB",
         "MQL_pred": ""
       }
     ]

  2. Legacy JSONL:
     {"db_id": "...", "sql": "..."}

By default, preprocesses any of these fields when present:
  SQL_pred, SQL, sql

Usage:
  python translator/preprocess_sql.py --in translator/out/input.json --out translator/out/input_clean.json

Legacy usage:
  python translator/preprocess_sql.py --in translator/out/merged.jsonl --out translator/out/merged_clean.jsonl
"""

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple


DEFAULT_SQL_FIELDS = ("SQL_pred", "SQL", "sql")


def fix_double_quoted_literals(sql: str) -> str:
    """Replace likely double-quoted string literals with single-quoted ones.

    Targets patterns where a double-quoted string appears as a value, not as
    a qualified identifier.

    Examples:
      WHERE col = "foo"         -> WHERE col = 'foo'
      WHERE col LIKE "foo%"     -> WHERE col LIKE 'foo%'
      WHERE col IN ("a", "b")   -> WHERE col IN ('a', 'b')
      WHERE col <> "bar"        -> WHERE col <> 'bar'
      T1."FieldName"            -> T1."FieldName"
    """
    # Strategy: replace likely double-quoted values while preserving qualified
    # identifiers such as T1."FieldName".

    def replacer(match):
        prefix = match.group(1)
        content = match.group(2)

        # If preceded by a dot, keep as an identifier: T1."Field"
        if prefix and prefix.endswith("."):
            return match.group(0)

        return prefix + "'" + content + "'"

    return re.sub(
        r'((?:^|[^.])\s*)"([^"]*)"',
        replacer,
        sql,
    )


_RESERVED_AS_COLUMN = ("Rank",)


def quote_reserved_identifiers(sql: str) -> str:
    """Double-quote bare reserved-word column references.

    Translator tokenises identifiers like `Rank` as the SQL:2003 `RANK()`
    window-function keyword, which breaks queries such as
    `SELECT Rank FROM captain` or `WHERE Rank = 'Professor'`.

    Skips already-qualified references (`T1.Rank`), suffixes inside larger
    identifiers (`Captain_Rank`), already-quoted (`"Rank"`), and genuine
    window-function calls (`RANK(...)`).
    """
    for word in _RESERVED_AS_COLUMN:
        pattern = rf'(?i)(?<![.\w"]){word}\b(?!\s*\()'
        sql = re.sub(pattern, lambda m: f'"{m.group(0)}"', sql)

    return sql

_SQL_IDENT = (
    r'(?:[A-Za-z_][A-Za-z0-9_$]*|'
    r'"(?:[^"]|"")+"|`[^`]+`|\[[^\]]+\])'
)
_SQL_TABLE = rf'{_SQL_IDENT}(?:\s*\.\s*{_SQL_IDENT})*'


_DEFERRED_INNER_JOIN_RE = re.compile(
    rf"""
    (?P<head>
        \bFROM\s+
        (?P<table1>{_SQL_TABLE})
        (?:\s+(?:AS\s+)?(?P<alias1>{_SQL_IDENT}))?
        \s+(?:INNER\s+)?JOIN\s+
        (?P<table2>{_SQL_TABLE})
        (?:\s+(?:AS\s+)?(?P<alias2>{_SQL_IDENT}))?
    )
    \s+
    (?P<third_join>
        (?:INNER\s+)?JOIN\s+
        (?P<table3>{_SQL_TABLE})
        (?:\s+(?:AS\s+)?(?P<alias3>{_SQL_IDENT}))?
        \s+ON\s+
    )
    (?P<condition>.*?)
    (?=
        \s+(?:
            WHERE\b |
            GROUP\s+BY\b |
            HAVING\b |
            ORDER\s+BY\b |
            LIMIT\b |
            UNION\b |
            INTERSECT\b |
            EXCEPT\b
        )
        |
        \s*;?\s*$
    )
    """,
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)


_QUALIFIED_EQUALITY_RE = re.compile(
    rf"""
    ^\s*\(?\s*
    ({_SQL_IDENT})\s*\.\s*({_SQL_IDENT})
    \s*=\s*
    ({_SQL_IDENT})\s*\.\s*({_SQL_IDENT})
    \s*\)?\s*$
    """,
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)


_QUALIFIER_RE = re.compile(
    rf"({_SQL_IDENT})\s*\.",
    re.IGNORECASE,
)


def _normalize_sql_identifier(value: str) -> str:
    """Normalize an identifier for case-insensitive comparison."""
    value = value.strip()

    if len(value) >= 2:
        if value[0] == value[-1] and value[0] in ('"', "`"):
            value = value[1:-1]
        elif value[0] == "[" and value[-1] == "]":
            value = value[1:-1]

    return value.lower()


def _effective_table_name(table: str, alias: str | None) -> str:
    """Return the alias, or the unqualified table name if no alias exists."""
    if alias:
        return _normalize_sql_identifier(alias)

    unqualified = re.split(r"\s*\.\s*", table)[-1]
    return _normalize_sql_identifier(unqualified)


def fix_deferred_inner_join_conditions(sql: str) -> str:
    """Move deferred inner-join predicates to their proper JOIN clauses.

    SQLite accepts constructs such as:

        T1 JOIN T2 JOIN T3
          ON T1.a = T2.a AND T1.b = T3.b

    Standard SQL requires:

        T1 JOIN T2 ON T1.a = T2.a
           JOIN T3 ON T1.b = T3.b

    The rewrite is performed only when:
      * all joins are inner joins;
      * a simple qualified equality connects table 1 and table 2; and
      * a remaining condition connects table 3 to table 1 or table 2.
    """

    def rewrite(match: re.Match) -> str:
        first = _effective_table_name(
            match.group("table1"), match.group("alias1")
        )
        second = _effective_table_name(
            match.group("table2"), match.group("alias2")
        )
        third = _effective_table_name(
            match.group("table3"), match.group("alias3")
        )

        moved = []
        remaining = []

        # The affected TEND queries use top-level AND conjunctions.
        for conjunct in re.split(
            r"(?i)\s+AND\s+", match.group("condition")
        ):
            equality = _QUALIFIED_EQUALITY_RE.fullmatch(conjunct)

            if equality:
                qualifiers = {
                    _normalize_sql_identifier(equality.group(1)),
                    _normalize_sql_identifier(equality.group(3)),
                }
            else:
                qualifiers = set()

            if qualifiers == {first, second}:
                moved.append(conjunct.strip())
            else:
                remaining.append(conjunct.strip())

        if not moved or not remaining:
            return match.group(0)

        later_qualifiers = {
            _normalize_sql_identifier(qualifier)
            for conjunct in remaining
            for qualifier in _QUALIFIER_RE.findall(conjunct)
        }

        # Do not rewrite unless the remaining ON condition genuinely
        # connects the third table to one of the earlier tables.
        if (
            third not in later_qualifiers
            or not later_qualifiers.intersection({first, second})
        ):
            return match.group(0)

        return (
            f"{match.group('head')} "
            f"ON {' AND '.join(moved)} "
            f"{match.group('third_join')}"
            f"{' AND '.join(remaining)}"
        )

    return _DEFERRED_INNER_JOIN_RE.sub(rewrite, sql)

def preprocess(sql: str) -> str:
    """Apply all preprocessing fixes to a SQL string."""
    sql = fix_deferred_inner_join_conditions(sql)
    sql = fix_double_quoted_literals(sql)
    sql = quote_reserved_identifiers(sql)
    return sql


def detect_input_format(path: Path) -> str:
    """Detect JSON array vs JSONL based on the first non-whitespace character."""
    with path.open("r", encoding="utf-8-sig") as f:
        while True:
            ch = f.read(1)
            if ch == "":
                raise RuntimeError(f"{path} is empty.")
            if not ch.isspace():
                return "json" if ch == "[" else "jsonl"


def load_json_array(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise RuntimeError(f"{path} must contain a top-level JSON array.")

    records = []
    for row_num, rec in enumerate(data, start=1):
        if not isinstance(rec, dict):
            raise RuntimeError(f"Record {row_num} in {path} is not a JSON object.")
        records.append(rec)

    return records


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    records = []

    with path.open("r", encoding="utf-8-sig") as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue

            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"Invalid JSON on line {line_num} in {path}: {e}") from e

            if not isinstance(rec, dict):
                raise RuntimeError(f"Line {line_num} in {path} is not a JSON object.")

            records.append(rec)

    return records


def load_records(path: Path) -> Tuple[List[Dict[str, Any]], str]:
    input_format = detect_input_format(path)

    if input_format == "json":
        return load_json_array(path), input_format

    if input_format == "jsonl":
        return load_jsonl(path), input_format

    raise RuntimeError(f"Unsupported input format: {input_format}")


def preprocess_records(
    records: List[Dict[str, Any]],
    sql_fields: List[str],
    require_any_field: bool,
    preserve_originals: bool,
) -> Tuple[int, int]:
    """Preprocess configured SQL fields.

    Returns:
      record_count_with_changes, field_count_with_changes
    """
    changed_records = 0
    changed_fields = 0

    for row_num, rec in enumerate(records, start=1):
        present_fields = [field for field in sql_fields if field in rec]

        if require_any_field and not present_fields:
            fields = ", ".join(sql_fields)
            raise RuntimeError(
                f"Record {row_num} does not contain any configured SQL field: {fields}"
            )

        record_changed = False

        for field in present_fields:
            original = rec[field]

            if original is None:
                continue

            if not isinstance(original, str):
                raise RuntimeError(
                    f"Field '{field}' in record {row_num} must be a string or null."
                )

            cleaned = preprocess(original)

            if cleaned != original:
                if preserve_originals:
                    original_field = f"{field}_original"
                    if original_field not in rec:
                        rec[original_field] = original

                rec[field] = cleaned
                changed_fields += 1
                record_changed = True

        if record_changed:
            changed_records += 1

    return changed_records, changed_fields


def write_records(records: List[Dict[str, Any]], path: Path, output_format: str) -> None:
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
    parser = argparse.ArgumentParser(description="Preprocess SQL before translation.")

    parser.add_argument("--in", dest="infile", required=True, help="Input JSON or JSONL file.")
    parser.add_argument("--out", required=True, help="Output JSON or JSONL file.")

    parser.add_argument(
        "--fields",
        nargs="+",
        default=list(DEFAULT_SQL_FIELDS),
        help=(
            "SQL fields to preprocess when present. "
            "Default: SQL_pred SQL sql"
        ),
    )

    parser.add_argument(
        "--input-format",
        choices=("auto", "json", "jsonl"),
        default="auto",
        help="Input format. Default: auto-detect.",
    )

    parser.add_argument(
        "--output-format",
        choices=("same", "json", "jsonl"),
        default="same",
        help="Output format. Default: same as input.",
    )

    parser.add_argument(
        "--require-any-field",
        action="store_true",
        help="Fail if a record has none of the configured SQL fields.",
    )

    parser.add_argument(
        "--preserve-originals",
        action="store_true",
        help="When a field changes, preserve the original in <field>_original.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    inpath = Path(args.infile)
    outpath = Path(args.out)

    if args.input_format == "auto":
        records, detected_format = load_records(inpath)
    elif args.input_format == "json":
        records = load_json_array(inpath)
        detected_format = "json"
    else:
        records = load_jsonl(inpath)
        detected_format = "jsonl"

    output_format = detected_format if args.output_format == "same" else args.output_format

    changed_records, changed_fields = preprocess_records(
        records=records,
        sql_fields=args.fields,
        require_any_field=args.require_any_field,
        preserve_originals=args.preserve_originals,
    )

    write_records(records, outpath, output_format)

    print(f"[OK] Preprocessed {len(records)} records")
    print(f"[OK] Modified records: {changed_records}")
    print(f"[OK] Modified fields:  {changed_fields}")
    print(f"[OK] Output:           {outpath.resolve()}")


if __name__ == "__main__":
    main()