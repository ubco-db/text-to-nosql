#!/usr/bin/env python3
"""
Load the flattened TEND MongoDB collections using PyMongo.

Each subdirectory under flattened_mongodb_collections becomes a MongoDB
database. Each JSON file in that directory becomes a collection.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from bson import json_util
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.errors import BulkWriteError, PyMongoError


DEFAULT_MONGO_URI = "mongodb://localhost:27017/"
DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "flattened_mongodb_collections"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load the flattened TEND collections into MongoDB."
    )
    parser.add_argument(
        "--uri",
        default=DEFAULT_MONGO_URI,
        help=f"MongoDB connection URI. Default: {DEFAULT_MONGO_URI}",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help=(
            "Directory containing one subdirectory per MongoDB database. "
            f"Default: {DEFAULT_DATA_DIR}"
        ),
    )
    parser.add_argument(
        "--keep-existing",
        action="store_true",
        help=(
            "Append documents to existing collections instead of replacing them. "
            "By default, each collection is dropped before loading."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Number of documents inserted per batch. Default: 1000",
    )
    return parser.parse_args()


def read_json_array(json_file: Path) -> list[dict[str, Any]]:
    try:    
        with json_file.open("r", encoding="utf-8") as file:
             documents = json_util.loads(file.read())
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON in {json_file}: line {exc.lineno}, "
            f"column {exc.colno}: {exc.msg}"
        ) from exc

    if not isinstance(documents, list):
        raise ValueError(
            f"{json_file} must contain a top-level JSON array, "
            f"but found {type(documents).__name__}."
        )

    invalid_indexes = [
        index for index, document in enumerate(documents)
        if not isinstance(document, dict)
    ]
    if invalid_indexes:
        preview = ", ".join(str(index) for index in invalid_indexes[:5])
        raise ValueError(
            f"{json_file} contains non-object entries at indexes: {preview}"
        )

    return documents


def insert_in_batches(
    collection: Collection,
    documents: list[dict[str, Any]],
    batch_size: int,
) -> int:
    inserted = 0

    for start in range(0, len(documents), batch_size):
        batch = documents[start : start + batch_size]
        if not batch:
            continue

        result = collection.insert_many(batch, ordered=True)
        inserted += len(result.inserted_ids)

    return inserted


def load_collections(
    client: MongoClient,
    data_dir: Path,
    keep_existing: bool,
    batch_size: int,
) -> tuple[int, int]:
    database_count = 0
    document_count = 0

    database_dirs = sorted(path for path in data_dir.iterdir() if path.is_dir())

    if not database_dirs:
        raise ValueError(f"No database directories found under {data_dir}")

    for database_dir in database_dirs:
        database_name = database_dir.name
        json_files = sorted(database_dir.glob("*.json"))

        if not json_files:
            print(f"Skipping database '{database_name}': no JSON files found.")
            continue

        database_count += 1
        database = client[database_name]

        for json_file in json_files:
            collection_name = json_file.stem
            collection = database[collection_name]

            print(
                f"Importing collection '{collection_name}' "
                f"into database '{database_name}'..."
            )

            documents = read_json_array(json_file)

            # Replacing collections makes repeated setup runs deterministic and
            # prevents duplicate benchmark records.
            if not keep_existing:
                collection.drop()
                collection = database[collection_name]

            inserted = insert_in_batches(
                collection=collection,
                documents=documents,
                batch_size=batch_size,
            )
            document_count += inserted

            print(f"  Inserted {inserted:,} documents.")

    return database_count, document_count


def main() -> int:
    args = parse_args()
    data_dir = args.data_dir.resolve()
    
    if args.batch_size <= 0:
        print("Error: --batch-size must be greater than zero.", file=sys.stderr)
        return 2

    if not data_dir.is_dir():
        print(
            f"Error: data directory does not exist: {data_dir}",
            file=sys.stderr,
        )
        return 2

    client: MongoClient | None = None

    try:
        client = MongoClient(
            args.uri,
            serverSelectionTimeoutMS=5000,
        )

        # Force an immediate connection check instead of waiting until insertion.
        client.admin.command("ping")
        print(f"Connected to MongoDB at {args.uri}")

        database_count, document_count = load_collections(
            client=client,
            data_dir=data_dir,
            keep_existing=args.keep_existing,
            batch_size=args.batch_size,
        )

        print(
            f"\nLoad complete: {database_count} databases and "
            f"{document_count:,} documents."
        )
        return 0

    except (ValueError, BulkWriteError, PyMongoError, OSError) as exc:
        print(f"\nLoad failed: {exc}", file=sys.stderr)
        return 1

    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())