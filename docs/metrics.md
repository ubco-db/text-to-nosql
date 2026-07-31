# Evaluation Metrics

This document describes the evaluation implemented in `evaluation/compute_metrics.py`. The evaluator compares a predicted MongoDB query with the corresponding gold query for each of the 2,775 examples in the TEND test set.

Two evaluation protocols are supported:

- **Enhanced evaluation**, reported in the paper as **EX-E**.
- **TEND-compatible evaluation**, reported in the paper as **EX-T**.

Both protocols report the same six metrics, but they use different execution-result comparison rules.

## Input format

The evaluator reads:

```text
evaluation/results/<result_name>.json
```

The file must contain a JSON array with one record per example. The principal fields are:

```json
{
  "db_id": "database_name",
  "nlq": "natural-language question",
  "SQL": "gold SQL",
  "SQL_pred": "predicted SQL",
  "MQL": "gold MongoDB query",
  "MQL_pred": "predicted MongoDB query"
}
```

The required evaluation fields are:

- `db_id`
- `MQL`
- `MQL_pred`

The other fields provide context for analysis. Gold `SQL` should be included when available because enhanced evaluation uses it to determine whether the requested result is ordered.

Each metric is computed as a binary value for every example. The reported aggregate is the mean over all evaluated examples:

```text
metric accuracy = examples receiving 1 / total examples
```

## Metrics

| Metric | Name | An example receives 1 when |
|---|---|---|
| **EM** | Exact Match | The normalized predicted and gold MQL strings are identical |
| **QSM** | Query Stage Match | The predicted and gold queries have the same ordered sequence of MongoDB operations |
| **QFC** | Query Field Coverage | The predicted and gold queries reference the same set of schema fields |
| **EX** | Execution Accuracy | The predicted and gold queries produce equivalent execution results under the selected protocol |
| **EFM** | Execution Field Match | The aligned execution results contain the same set of returned field names |
| **EVM** | Execution Value Match | Every pair of aligned result documents has deeply equal values |

**EX is the primary correctness metric.** EM, QSM, QFC, EFM, and EVM are diagnostic metrics that help characterize why two queries differ.

### Exact Match (EM)

EM compares the predicted and gold MQL strings after mode-specific normalization.

Enhanced normalization:

1. Removes surrounding whitespace and trailing semicolons.
2. Collapses repeated whitespace.
3. Removes quotes around JSON object keys.
4. Removes spaces around structural punctuation such as braces, brackets, commas, and colons.

This normalization reconciles common formatting differences between MongoDB shell syntax and JSON-style output. It does not canonicalize query semantics, reorder pipeline stages, or rewrite operators.

TEND-compatible normalization follows the presentation used by the original TEND queries. Predicted MQL is converted to shell-style object-key formatting before whitespace-normalized string comparison.

EM remains a strict representation-level metric. Semantically equivalent queries can receive `EM = 0`.

### Query Stage Match (QSM)

QSM extracts the ordered sequence of operations used by each query and compares the two sequences for exact equality.

For aggregation pipelines, stages include operations such as:

```text
match → unwind → lookup → group → project → sort → limit
```

For `find()` queries, the sequence can include filtering, projection, sorting, and limiting. Selected nested operators, including regular-expression and negation operations, may also be represented.

Stage names are normalized by removing a leading `$`. Both stage sequences must be non-empty and identical for `QSM = 1`.

### Query Field Coverage (QFC)

QFC extracts the schema fields referenced by each MQL query and compares the resulting sets.

Field extraction uses the MongoDB schema corresponding to the example’s `db_id`, stored under:

```text
data/benchmark/tend/mongodb_schema/
```

QFC is set equality rather than a recall or overlap score:

```text
QFC = 1 if predicted_fields = gold_fields
QFC = 0 otherwise
```

Field order and repeated references do not affect QFC.

### Execution Accuracy (EX)

EX executes both queries against the MongoDB database identified by `db_id` and compares the returned results.

The label used in the paper depends on the selected protocol:

| Evaluator mode | Paper label |
|---|---|
| `--metric-mode enhanced` | **EX-E** |
| `--metric-mode tend` | **EX-T** |

The two values must be produced by separate evaluator runs over the same predictions.

### Execution Field Match (EFM)

EFM recursively collects field names from aligned gold and predicted result documents. It receives 1 when the two collected field-name sets are identical.

EFM examines field names rather than complete field paths. It is intended to diagnose output-shape differences and does not establish full result correctness.

### Execution Value Match (EVM)

EVM compares each aligned pair of gold and predicted result documents using deep equality. Nested objects and arrays are compared recursively.

EVM is diagnostic and should be interpreted with EX. EX additionally accounts for result cardinality and the applicable top-level ordering rules.

## TEND-compatible protocol

TEND-compatible mode follows the original benchmark’s order-sensitive execution comparison.

It:

- Executes queries through the MongoDB shell.
- Requires equal result lengths.
- Compares top-level result documents position by position.
- Recursively compares nested objects and arrays.
- Treats top-level result ordering as significant for every query.
- Does not normalize the case of generated aggregate aliases in execution results.

Consequently, two queries can receive `EX-T = 0` when they return the same documents in a different incidental order, even when neither query specifies an ordering requirement.

Run this protocol with:

```powershell
Push-Location evaluation

python compute_metrics.py `
    --file_name <result_name>_tend `
    --metric-mode tend

Pop-Location
```

The `_tend` suffix is a repository naming convention that distinguishes TEND-compatible artifacts. The evaluator does not add the suffix automatically.

## Enhanced protocol

Enhanced mode distinguishes ordered query results from unordered results.

### Queries without an ordering requirement

If neither the gold SQL nor the gold MQL requires final output ordering, the top-level result lists are compared as multisets.

This means that:

- Result order is ignored.
- Result cardinality must match.
- Duplicate documents remain significant.
- Each document’s nested structure and values must match exactly.
- Ordering inside nested arrays remains significant.

### Queries with an ordering requirement

A result is treated as ordered when:

- The gold SQL contains `ORDER BY`; or
- The gold MQL contains an observable final sort.

For `find()` queries, a chained `.sort()` establishes ordered output.

For aggregation queries, a `$sort` establishes final ordering only when all subsequent stages preserve that ordering. The evaluator treats the following subsequent stages as order preserving:

```text
$project
$match
$limit
$skip
$addFields
$set
$unset
```

When ordered output is required, the evaluator first attempts strict position-by-position comparison. If that fails, it allows permutations only within groups of documents tied on all gold sort keys.

For example, if two documents have the same value for every requested sort key, exchanging their positions does not cause enhanced execution comparison to fail. Documents with different sort-key values must remain in the required order.

### Aggregate alias normalization

Enhanced execution comparison treats generated aggregate aliases case-insensitively. Examples include:

```text
COUNT and count
AVG_salary and avg_salary
SUM_distinct_value and sum_distinct_value
```

This normalization is limited to generated aliases beginning with `count`, `sum`, `avg`, `min`, or `max`. Arbitrary MongoDB field names remain case-sensitive.

Run enhanced evaluation with:

```powershell
Push-Location evaluation

python compute_metrics.py `
    --file_name <result_name> `
    --metric-mode enhanced

Pop-Location
```

## Query execution

Enhanced mode uses PyMongo directly when the query can be parsed as one of the following forms:

- `aggregate(...)`
- `find(...)`
- `find(...).count()`
- `distinct(...)`

Other query forms fall back to execution through `mongosh`.

TEND-compatible mode uses the MongoDB shell for execution to preserve the original evaluation behavior.

The evaluator expects:

```text
mongodb://localhost:27017/
```

It also requires `mongosh` or the legacy `mongo` executable. The executable can be available on the system path or specified through the `MONGOSH_PATH` environment variable.

Execution uses exact structural and scalar comparison; it does not apply numeric tolerances or approximate string matching.

## Errors and timeouts

An unhandled metric or execution exception assigns zero to the affected metrics. An exception that prevents evaluation of an entire example assigns zero to all six metrics for that example.

The shell executor has a default query timeout of 30 seconds. Shell errors, timeouts, and unparsable shell results are represented as empty result lists by the execution helper. These cases should therefore be checked in the per-example analysis file and metric log, particularly when the gold query legitimately returns an empty result.

## Output files

A run named `<result_name>` produces the following files under `evaluation/results/`:

| File | Contents |
|---|---|
| `<result_name>.json` | Canonical evaluator input |
| `<result_name>_metrics.log` | Aggregate metrics, per-example diagnostic logging, timing information, and error messages |
| `<result_name>_examples.jsonl` | One detailed analysis record per example |
| `<result_name>_wrong_examples.json` | Compact list of examples with `EX = 0` |
| `<result_name>_summary_by_db.csv` | Execution outcomes grouped by database and failure category |
| `<result_name>_summary_by_bucket.csv` | Counts grouped by failure category |
| `<result_name>_summary_by_signature.csv` | Counts grouped by gold and predicted query-stage signatures |

The per-example JSONL file includes:

- Normalized gold and predicted queries.
- Extracted stages and fields.
- Execution-result counts and bounded previews.
- Missing and additional result fields.
- Whether ordered comparison was required.
- Strict, unordered, and tie-aware comparison outcomes.
- Individual metric values.
- A coarse failure category.

Analysis JSONL and CSV generation can be disabled with:

```powershell
python compute_metrics.py `
    --file_name <result_name> `
    --metric-mode enhanced `
    --no_analysis
```

The all-in-one translator pipeline initially writes these files to `evaluation/results/` and then moves the completed artifact family to:

```text
results/evaluation/enhanced/
```

or:

```text
results/evaluation/tend/
```

## Interpreting the metrics

EM, QSM, and QFC compare properties of the query text. EX, EFM, and EVM compare query execution results.

A query can therefore:

- Receive `EM = 0` but `EX = 1` when it is written differently but returns the correct result.
- Receive `QSM = 0` but `EX = 1` when it uses a different valid pipeline structure.
- Receive `QFC = 1` but `EX = 0` when it references the correct fields but applies incorrect conditions or operations.
- Receive `EFM = 1` but `EX = 0` when it returns the expected fields with incorrect values, cardinality, or ordering.

For overall system comparisons, use EX-E or EX-T as appropriate and report which protocol was used.