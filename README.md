# Text-to-NoSQL via SQL

This repository contains the code, benchmark data, predictions, and evaluation artifacts for *Text-to-NoSQL via SQL: Accurate Query Generation for MongoDB*.

The approach uses SQL as an intermediate representation between natural-language understanding and MongoDB query generation:

```text
Natural-language question
        ↓
Text-to-SQL model
        ↓
SQL
        ↓
Deterministic SQL-to-MQL translator
        ↓
MongoDB query
```

The system is evaluated on the 2,775-example TEND test set using both gold SQL and SQL predicted by DAIL-SQL. The repository also includes direct MQL predictions produced by GPT-5.6-sol, Gemini 3.5 Flash, and Kimi K3.

The evaluation data is derived from the [TEND benchmark](https://arxiv.org/abs/2502.11201v1), introduced in *Bridging the Gap: Enabling Natural Language Queries for NoSQL Databases through Text-to-NoSQL Translation*.

---

## Prerequisites

The translator and evaluation pipeline require:

- **Java Development Kit (JDK) 8 or later** — required to compile the Java server wrapper and run the translator.
- **Python 3.8 or later**.
- **MongoDB** running locally at `mongodb://localhost:27017`.
- **Docker with Compose** if using the provided MongoDB configuration.

The compiled SQL-to-MQL translator is included at:

```text
translator/java/mongodb_unityjdbc_full.jar
```

It does not need to be built separately.

Install the Python dependencies from the repository root:

```powershell
python -m pip install -r requirements.txt
```

API credentials are not required to reproduce the evaluation of the committed predictions. Provider API keys are required only when generating new LLM predictions.

---

## One-time setup

### Step 1 — Start MongoDB

The evaluation pipeline expects MongoDB to be available at:

```text
mongodb://localhost:27017
```

The repository includes a Docker Compose configuration for starting MongoDB. From the repository root:

```bash
cd data/benchmark/tend
docker compose up -d
```

If MongoDB is already running locally at the expected address, skip this command.

### Step 2 — Load the TEND data

The TEND benchmark collections must be imported before running either evaluation protocol. From the repository root, run:

```bash
python data/benchmark/tend/load_data.py

It is also possible to load using a shell script. This requires more setup than the Python code. On Windows, run `load_data.sh` through Git Bash or WSL. The `mongoimport` command must be available on the shell's path.

```bash
./data/benchmark/tend/load_data.sh
```
The loading script imports the JSON files under `flattened_mongodb_collections/`. It creates one MongoDB database for each TEND database directory and one collection for each JSON file.

**Reproducibility note.** The results reported in the paper were produced using `load_data.sh`, which loads the benchmark collections with `mongoimport`. The provided `load_data.py` script offers a more convenient cross-platform alternative using PyMongo and does not require the MongoDB Database Tools. Both scripts load the same benchmark documents, but they may produce different physical insertion orders. Because MongoDB does not guarantee a stable order among tied values, a small number of queries containing ORDER BY with LIMIT and no complete tie-breaking order may return a different tied row. The generated MQL and non-tied query results are unaffected.

No separate translator build or server setup is required. The pipeline automatically compiles `translator/java/TranslateServer.java`, starts the server, runs the translations, and stops the server when processing is complete.

---

## Running the pipeline

The end-to-end translator pipeline is controlled by `translator/run_pipeline.py`. Run all commands from the repository root after MongoDB is running and the TEND data has been loaded.

The pipeline performs the following steps:

1. Prepares a canonical input file from the TEND test set.
2. Selects either gold SQL or DAIL-SQL predicted SQL.
3. Preprocesses the SQL into a form accepted by the translator.
4. Compiles and starts the Java translation server.
5. Translates each SQL query into MQL.
6. Executes the selected evaluation protocol.
7. Stores the evaluation artifacts under `results/evaluation/`.
8. Stops the translation server.

### Option A — Run the complete pipeline

#### Enhanced evaluation

Run the translator using gold SQL:

```powershell
python translator/run_pipeline.py mongotranslator_gold_sql --sql-source gold --metric-mode enhanced
```

Run the complete SQL-mediated pipeline using DAIL-SQL predicted SQL:

```powershell
python translator/run_pipeline.py mongotranslator_pred_sql --sql-source pred --metric-mode enhanced
```

These commands store their output under:

```text
results/evaluation/enhanced/
```

#### TEND-compatible evaluation

Run the translator using gold SQL:

```powershell
python translator/run_pipeline.py mongotranslator_gold_sql_tend --sql-source gold --metric-mode tend
```

Run the complete SQL-mediated pipeline using DAIL-SQL predicted SQL:

```powershell
python translator/run_pipeline.py mongotranslator_pred_sql_tend --sql-source pred --metric-mode tend
```

These commands store their output under:

```text
results/evaluation/tend/
```

The `_tend` suffix distinguishes files produced using the TEND-compatible evaluation protocol.

#### Pipeline outputs

Each run produces an evaluation-ready input file and several derived artifacts. For a run named `<result_name>`, the output family includes:

| Filename | Contents |
|---|---|
| `<result_name>.json` | Canonical input containing gold and predicted SQL and MQL |
| `<result_name>_examples.jsonl` | Per-example metric results |
| `<result_name>_metrics.log` | Aggregate metric output |
| `<result_name>_summary_by_db.csv` | Results grouped by database |
| `<result_name>_summary_by_bucket.csv` | Results grouped by evaluation bucket |
| `<result_name>_summary_by_signature.csv` | Results grouped by query signature |
| `<result_name>_wrong_examples.json` | Examples that fail execution comparison |

Intermediate pipeline files are written to `translator/out/`, and the Java server log is written to `translator/logs/server.log`. These are working files rather than the authoritative reported results.

The translation server uses port `8082`. If the pipeline reports that this port is already in use, stop the previous `TranslateServer` process before starting another run.

---

### Option B — Run the pipeline step by step

The individual pipeline stages can be run separately when debugging preprocessing, translation, or evaluation. The following commands use PowerShell and should be run from the repository root unless otherwise indicated.

#### Terminal 1 — Start the translation server

Move to the Java directory, compile the server wrapper, and start it:

```powershell
cd translator/java

javac -cp ".;mongodb_unityjdbc_full.jar" TranslateServer.java

java -cp ".;mongodb_unityjdbc_full.jar" TranslateServer
```

The server listens at `http://localhost:8082/translate`. Leave this terminal running while completing the remaining steps.

On Linux or macOS, replace the semicolon in the Java classpath with a colon:

```bash
javac -cp ".:mongodb_unityjdbc_full.jar" TranslateServer.java
java -cp ".:mongodb_unityjdbc_full.jar" TranslateServer
```

#### Terminal 2 — Prepare predicted-SQL input

From the repository root, create the canonical input using the DAIL-SQL predictions:

```powershell
python translator/prepare_input.py `
    --benchmark data/benchmark/tend/test.json `
    --predictions data/benchmark/dail/DAILresults.txt `
    --out translator/out/input.json `
    --sql-source pred
```

This creates `translator/out/input.json`, containing the TEND examples and their corresponding predicted SQL.

#### Preprocess the SQL

```powershell
python translator/preprocess_sql.py `
    --in translator/out/input.json `
    --out translator/out/input_clean.json
```

This applies the SQL normalization required by the translator.

#### Translate SQL to MQL

```powershell
python translator/collect_mql_preds.py `
    --in translator/out/input_clean.json `
    --out translator/out/formatted_results.json `
    --url http://localhost:8082/translate `
    --sql-source pred
```

The resulting `translator/out/formatted_results.json` contains the canonical records with `MQL_pred` populated.

#### Prepare the evaluator input

Copy the translated results into the evaluator’s working directory:

```powershell
Copy-Item `
    translator/out/formatted_results.json `
    evaluation/results/debug_pred_sql.json `
    -Force
```

#### Run the enhanced evaluator

```powershell
Push-Location evaluation

python compute_metrics.py `
    --file_name debug_pred_sql `
    --metric-mode enhanced

Pop-Location
```

The evaluator writes the result family under:

```text
evaluation/results/debug_pred_sql*
```

This directory is used as evaluator staging. The all-in-one pipeline moves completed publication runs to `results/evaluation/enhanced/` or `results/evaluation/tend/`.

#### Using gold SQL

To debug translation using gold SQL, prepare a separate input file:

```powershell
python translator/prepare_input.py `
    --benchmark data/benchmark/tend/test.json `
    --out translator/out/input_gold.json `
    --sql-source gold
```

Preprocess it:

```powershell
python translator/preprocess_sql.py `
    --in translator/out/input_gold.json `
    --out translator/out/input_gold_clean.json
```

Translate it:

```powershell
python translator/collect_mql_preds.py `
    --in translator/out/input_gold_clean.json `
    --out translator/out/formatted_results_gold.json `
    --url http://localhost:8082/translate `
    --sql-source gold
```

Copy and evaluate it:

```powershell
Copy-Item `
    translator/out/formatted_results_gold.json `
    evaluation/results/debug_gold_sql.json `
    -Force

Push-Location evaluation

python compute_metrics.py `
    --file_name debug_gold_sql `
    --metric-mode enhanced

Pop-Location
```

Use `--metric-mode tend` and a result name ending in `_tend` when debugging the TEND-compatible protocol.

When finished, return to Terminal 1 and stop the translation server with `Ctrl+C`.

---

## Evaluation metrics

The evaluation code is implemented in `evaluation/compute_metrics.py` and reports results over all 2,775 TEND test examples.

Two execution-evaluation protocols are supported:

| Mode | Paper metric | Description |
|---|---|---|
| `--metric-mode enhanced` | **EX-E** | Enhanced execution comparison that requires ordering only when the query specifies an ordering requirement and allows valid permutations among tied results |
| `--metric-mode tend` | **EX-T** | TEND-compatible execution comparison following the behavior of the original benchmark evaluator |

The enhanced protocol avoids penalizing queries because of incidental MongoDB result order when the SQL query does not contain `ORDER BY`. When ordering is required, the evaluator checks the requested ordering while accounting for rows tied on the ordering keys.

Both evaluation modes report the following metrics:

| Metric | Description |
|---|---|
| **EM** | Exact match between the normalized predicted and gold MQL strings |
| **QSM** | Query-stage match between the predicted and gold MongoDB operations |
| **QFC** | Query-field coverage comparing the fields referenced by the predicted and gold queries |
| **EX** | Execution accuracy under the selected evaluation protocol |
| **EFM** | Execution-field match comparing the fields returned by the predicted and gold queries |
| **EVM** | Execution-value match comparing the values returned by the predicted and gold queries |

The paper reports execution accuracy as **EX-E** for enhanced evaluation and **EX-T** for TEND-compatible evaluation. These values come from separate evaluator runs over the same predictions.

Execution-based metrics require MongoDB to be running and the TEND collections to be loaded. Detailed metric definitions and implementation notes are provided in [`docs/metrics.md`](docs/metrics.md).

---

## Repository layout

| Path | Contents |
|---|---|
| `data/benchmark/tend/` | TEND train and test data, flattened MongoDB collections, Docker Compose configuration, and data-loading scripts |
| `data/benchmark/dail/` | DAIL-SQL predictions used by the SQL-mediated pipeline |
| `translator/` | Python scripts for input preparation, SQL preprocessing, translation, and end-to-end pipeline execution |
| `translator/java/` | Java translation-server wrapper, UnityJDBC translator JAR, and MongoDB schema configurations |
| `evaluation/` | Metric implementation and supporting query-execution utilities |
| `llm/` | Scripts for generating, extracting, and processing direct LLM predictions |
| `predictions/llm/` | Committed LLM outputs before evaluation metrics are computed |
| `results/evaluation/enhanced/` | Final artifacts produced using enhanced evaluation |
| `results/evaluation/tend/` | Final artifacts produced using TEND-compatible evaluation; filenames retain the `_tend` suffix |
| `docs/` | Additional documentation covering the evaluation metrics |

The directories `translator/out/`, `translator/logs/`, and `evaluation/results/` contain intermediate or staging files generated during pipeline execution. The authoritative reported results are stored under `results/`.

---

## Translator availability and licensing

The compiled SQL-to-MQL translator used in this work is included at:

```text
translator/java/mongodb_unityjdbc_full.jar
```

This JAR is a licensed commercial product owned by UnityJDBC. Its source code is not included in this repository, and the JAR is **not** covered by the repository’s BSD 3-Clause License.

The repository authors have permission from UnityJDBC to redistribute this licensed JAR as part of this research artifact. It is included to support reproduction of the experiments reported in the paper. Redistribution with this repository does not grant a license for production use. Users who wish to deploy the translator in a production system must obtain an appropriate license directly from [UnityJDBC](https://unityjdbc.com/mongojdbc/mongo_jdbc.php).

Except for separately identified third-party materials, the Python pipeline and evaluation code and the Java translation-server wrapper are distributed under the repository’s [BSD 3-Clause License](LICENSE).
