"""Make a consistent SQLite backup while the production bot is running."""

import argparse
from datetime import UTC, datetime
from pathlib import Path
import sqlite3


def backup_database(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"worldsbot-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}.db"
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as database:
        with sqlite3.connect(target) as backup:
            database.backup(backup)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(backup_database(args.source, args.destination))
