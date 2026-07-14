# Text-to-NoSQL: Natural Language Interface for MongoDB

Translate **natural language questions** into **MongoDB queries (MQL)** via SQL as an intermediate representation:

```
NLQ  →  [Text-to-SQL model]  →  SQL  →  TranslateServer (UnityJDBC + MongoBuilder)  →  MQL  →  Metrics
```

> Based on: [Bridging the Gap: Enabling Natural Language Queries for NoSQL Databases through Text-to-NoSQL Translation](https://arxiv.org/pdf/2502.11201)

---

## Prerequisites

- **Java 8+** — to build the translator JAR and run `TranslateServer.java`
- **Python 3.8+**
- **MongoDB** running locally on `mongodb://localhost:27017` (Docker is the easiest option, see Step 2 below)
- **`mongoimport`** — bundled with the MongoDB Database Tools; needed by `TEND/load_data.sh`
- Python packages:
  ```bash
  pip install -r requirements.txt
  # or, minimally:
  pip install pymongo tqdm demjson3 requests
  ```

Note: DAIL results generated with GPT-3.5 turbo.
---

## One-time setup

### Step 1 — Build the translator JAR

The pipeline uses `mongodb_unityjdbc_full.jar`, produced by the Ant build in the `mongotranslator` repo.

From wherever you have `mongotranslator` checked out:

```bash
cd /path/to/mongotranslator
ant dist
# Produces: dist/mongo/mongodb_unityjdbc_full.jar
```

Copy (or symlink) the JAR into `text-to-nosql/example/` so the pipeline scripts can find it:

```bash
cp dist/mongo/mongodb_unityjdbc_full.jar /path/to/text-to-nosql/example/
```

> Skip this step if `text-to-nosql/example/mongodb_unityjdbc_full.jar` already exists and you haven't changed the Java sources.

### Step 2 — Load the TEND dataset into MongoDB

The TEND benchmark ships as flattened JSON collections under `TEND/flattened_mongodb_collections/`. Load them into a local MongoDB instance.

```bash
cd TEND

# Start MongoDB locally (Docker; uses docker-compose.yml in this directory)
docker-compose up -d

# Import every flattened collection into MongoDB
chmod +x load_data.sh   # first time only
./load_data.sh
```

`load_data.sh` walks `flattened_mongodb_collections/*` and runs `mongoimport` for each `*.json` file, creating one MongoDB database per subdirectory and one collection per file.

> If you run MongoDB another way (Homebrew service, system install, etc.), skip `docker-compose up -d` — just make sure something is listening on `mongodb://localhost:27017` before running `load_data.sh`.

---

## Running the pipeline

There are two ways to run it:

- **All-in-one** with `run_pipeline.sh` (recommended for routine runs)
- **Step-by-step** (helpful for debugging or development)

### Option A — All-in-one (`run_pipeline.sh`)

```bash
cd example
chmod +x run_pipeline.sh        # first time only
./run_pipeline.sh <run_name>    # e.g. ./run_pipeline.sh result4
```

The script:
1. Compiles `TranslateServer.java` against `mongodb_unityjdbc_full.jar`.
2. Starts the TranslateServer on `http://localhost:8082` in the background (logs → `example/logs/server.log`).
3. Preprocesses the SQL predictions in `out/merged.jsonl` (e.g., normalises double-quoted string literals) → `out/merged_clean.jsonl`.
4. Calls the TranslateServer for each SQL → `out/output.json`.
5. Formats predictions for the metrics suite → `out/formatted_results.json`.
6. Copies the formatted file to `../metric/results/<run_name>.json`.
7. Runs `metric2.py --file_name <run_name>` on the copy.
8. Stops the TranslateServer on exit.

**Outputs**

| Path | Contents |
|------|----------|
| `example/out/output.json` | Raw TranslateServer responses |
| `example/out/formatted_results.json` | Per-NLQ rows (`count, db_id, nlq, SQL, SQL_pred, MQL, MQL_pred`) |
| `metric/results/<run_name>.json` | Copy used by the metrics |
| `metric/utils/logs/<run_name>.log` | Per-example metric logs |

The metrics take ~5 minutes for the 2,775 test examples.

### Option B — Step-by-step (debugging)

**Terminal 1 — start the TranslateServer**
```bash
cd example
javac -cp .:mongodb_unityjdbc_full.jar TranslateServer.java
java  -cp .:mongodb_unityjdbc_full.jar TranslateServer
# Listens on http://localhost:8082; leave running
```

**Terminal 2 — run pipeline steps**
```bash
cd example

# 1. (Only if you don't already have out/merged.jsonl)
#    Pair each predicted SQL with its db_id by line order.
python3 merge_sql_dbid.py \
  --sql_txt databaseContents/DAILresults.txt \
  --db_json databaseContents/dbidQuest.json \
  --out    out/merged.jsonl \
  --out_format jsonl

# 2. Preprocess SQL (e.g. fix double-quoted string literals)
python3 preprocess_sql.py \
  --in  out/merged.jsonl \
  --out out/merged_clean.jsonl

# 3. Translate SQL → MQL via the TranslateServer
python3 collect_mql_preds.py \
  --in  out/merged_clean.jsonl \
  --out out/output.json \
  --url http://localhost:8082/translate \
  --method get \
  --debug \
  --probe

# 4. Format for metrics
python3 formatter.py
# Reads:  out/output.json
# Writes: out/formatted_results.json

# 5. Copy into metric/results/ under your chosen run name
cp -f out/formatted_results.json ../metric/results/<run_name>.json

# 6. Run metrics
cd ../metric/utils
python3 metric2.py --file_name <run_name>
# Reads: ../results/<run_name>.json
# Logs:  ./logs/<run_name>.log
```

When done, return to Terminal 1 and stop the server with `Ctrl+C`.

---

## Metrics

`metric/utils/metric2.py` computes six metrics over the 2,775 test examples:

- **EM** — Exact Match (normalised string equality of MQL)
- **QSM** — Query Stage Match (pipeline stage sequence)
- **QFC** — Query Fields Coverage (referenced-field set)
- **EX** — Execution Accuracy (deep equality of result sets) — *the headline metric*
- **EFM** — Execution Fields Match (returned field sets)
- **EVM** — Execution Value Match (per-document deep equality)

EX, EFM, and EVM execute the predicted MQL against the loaded MongoDB instance — so MongoDB must be running and the TEND data must be loaded (Step 2).

---

## Repository layout

| Path | Role |
|------|------|
| `example/run_pipeline.sh` | End-to-end orchestrator |
| `example/TranslateServer.java` | HTTP wrapper around UnityJDBC's MongoBuilder |
| `example/preprocess_sql.py` | SQL cleanup before translation |
| `example/collect_mql_preds.py` | Batch SQL → MQL collection |
| `example/formatter.py` | Aligns predictions with gold MQL |
| `example/databaseContents/` | DAIL-SQL predictions + db_id assignments |
| `metric/utils/metric2.py` | Metric computation |
| `metric/results/` | Per-run formatted prediction files |
| `TEND/` | Benchmark data + Docker compose + `load_data.sh` |
| `SMART/` | LLM-based extensions (RAG, optimisation, debugging) |
| `baselines/` | Zero-shot, ICL, RAG, SQL→NoSQL baselines |
| `mongotranslator` (separate repo) | Java sources for the MongoBuilder + Ant build that produces `mongodb_unityjdbc_full.jar` |
