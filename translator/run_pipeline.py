# run_pipeline.py
import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

PORT = 8082
JAR_NAME = "mongodb_unityjdbc_full.jar"

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

JAVA_DIR = SCRIPT_DIR / "java"
OUT_DIR = SCRIPT_DIR / "out"
LOG_DIR = SCRIPT_DIR / "logs"

PREPARE_SCRIPT = SCRIPT_DIR / "prepare_input.py"
PREPROCESS_SCRIPT = SCRIPT_DIR / "preprocess_sql.py"
COLLECT_SCRIPT = SCRIPT_DIR / "collect_mql_preds.py"

DEFAULT_BENCHMARK = REPO_ROOT / "data" / "benchmark" / "tend" / "test.json"
DEFAULT_PREDICTIONS = REPO_ROOT / "data" / "benchmark" / "dail" / "DAILresults.txt"

INPUT_JSON = OUT_DIR / "input.json"
INPUT_CLEAN_JSON = OUT_DIR / "input_clean.json"
FORMATTED_JSON = OUT_DIR / "formatted_results.json"

EVALUATION_DIR = REPO_ROOT / "evaluation"
EVALUATION_RESULTS_DIR = EVALUATION_DIR / "results"


def run(cmd, cwd=None):
    print(" ".join(str(x) for x in cmd))
    subprocess.run(cmd, cwd=cwd, check=True)


def wait_for_port(host, port, timeout_seconds=30):
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                print("Server is up.")
                return True
        except OSError:
            time.sleep(1)

    return False

def is_port_open(host, port):
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def stop_process_tree(proc):
    if proc is None:
        return

    if proc.poll() is not None:
        return

    print(f"Stopping TranslateServer PID {proc.pid}")

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

def require_file(path, description):
    if not path.exists():
        raise RuntimeError(f"Missing {description}: {path}")
    

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("result_name", help="Result name without .json extension")

    parser.add_argument(
        "--sql-source",
        choices=["pred", "gold"],
        default="pred",
        help="SQL source to translate: pred uses SQL_pred; gold uses SQL.",
    )

    parser.add_argument(
        "--benchmark",
        default=str(DEFAULT_BENCHMARK),
        help="Benchmark file for prepare_input.py.",
    )

    parser.add_argument(
        "--predictions",
        default=str(DEFAULT_PREDICTIONS),
        help="SQL prediction file for prepare_input.py when --sql-source pred.",
    )

    parser.add_argument(
        "--prepared-input",
        default=str(INPUT_JSON),
        help="Canonical prepared input path.",
    )

    parser.add_argument(
        "--clean-input",
        default=str(INPUT_CLEAN_JSON),
        help="Preprocessed canonical input path.",
    )

    parser.add_argument(
        "--skip-prepare",
        action="store_true",
        help="Skip prepare_input.py and use --prepared-input.",
    )

    parser.add_argument(
        "--skip-preprocess",
        action="store_true",
        help="Skip preprocess_sql.py.",
    )

    args = parser.parse_args()

    result_name = args.result_name

    benchmark_path = Path(args.benchmark)
    predictions_path = Path(args.predictions)
    prepared_input_path = Path(args.prepared_input)
    clean_input_path = Path(args.clean_input)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    EVALUATION_RESULTS_DIR.mkdir(parents=True, exist_ok=True)    

    require_file(PREPARE_SCRIPT, "prepare script")
    require_file(PREPROCESS_SCRIPT, "preprocess script")
    require_file(COLLECT_SCRIPT, "collect script")

    if not args.skip_prepare:
        require_file(benchmark_path, "benchmark file")

        prepare_cmd = [
            sys.executable,
            str(PREPARE_SCRIPT),
            "--benchmark", str(benchmark_path),
            "--out", str(prepared_input_path),
            "--sql-source", args.sql_source,
        ]

        if args.sql_source == "pred":
            require_file(predictions_path, "prediction file")
            prepare_cmd.extend(["--predictions", str(predictions_path)])

        print("Preparing canonical pipeline input ...")
        run(prepare_cmd)
    else:
        require_file(prepared_input_path, "prepared canonical input")

    collect_input_path = prepared_input_path

    if not args.skip_preprocess:
        print("Preprocessing SQL ...")
        run([
            sys.executable,
            str(PREPROCESS_SCRIPT),
            "--in", str(prepared_input_path),
            "--out", str(clean_input_path),
        ])
        collect_input_path = clean_input_path
        
    classpath_sep = ";" if os.name == "nt" else ":"
    classpath = f".{classpath_sep}{JAR_NAME}"

    javac = shutil.which("javac")
    java = shutil.which("java")

    if javac is None:
        raise RuntimeError("javac not found on PATH.")
    if java is None:
        raise RuntimeError("java not found on PATH.")

    print("Compiling TranslateServer.java ...")
    class_file = JAVA_DIR / "TranslateServer.class"
    if class_file.exists():
        class_file.unlink()

    run([javac, "-cp", classpath, "TranslateServer.java"], cwd=JAVA_DIR)

    if is_port_open("localhost", PORT):
        raise RuntimeError(
            f"Port {PORT} is already in use. Stop the old TranslateServer before running again."
        )
    
    print(f"Starting TranslateServer on port {PORT} ...")
    server_log_path = LOG_DIR / "server.log"
    log_file = open(server_log_path, "w", encoding="utf-8")

    server = subprocess.Popen(
        [java, "-cp", classpath, "TranslateServer"],
        cwd=JAVA_DIR,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    print(f"TranslateServer PID: {server.pid}")

    try:
        print(f"Waiting for server to be ready at http://localhost:{PORT}/ ...")
        if not wait_for_port("localhost", PORT, timeout_seconds=30):
            raise RuntimeError(f"TranslateServer did not start on port {PORT}. Check {server_log_path}.")

        print("Collecting MongoDB predictions ...")
        run([
            sys.executable,
            str(COLLECT_SCRIPT),
            "--in", str(collect_input_path),
            "--out", str(FORMATTED_JSON),
            "--url", f"http://localhost:{PORT}/translate",
            "--sql-source", args.sql_source,
        ])

        metric_input_path = EVALUATION_RESULTS_DIR / f"{result_name}.json"

        print(f"Copying {FORMATTED_JSON} -> {metric_input_path}")
        shutil.copyfile(FORMATTED_JSON, metric_input_path)

        print("Running metrics ...")
        run([sys.executable, "compute_metrics.py", "--file_name", result_name], cwd=EVALUATION_DIR)

        print(f"Pipeline completed successfully. Results in {metric_input_path}")

    finally:
        stop_process_tree(server)

        if log_file is not None:
            log_file.close()


if __name__ == "__main__":
    main()