"""Create (or upgrade) the local holdings database from schema.sql.

Usage: python init_db.py [db_path]   (default: holdings.db)

Safe to re-run: every statement in schema.sql uses IF NOT EXISTS, so this
creates missing tables without touching existing data.
"""
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE / "schema.sql"


def init_db(db_path):
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(schema)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else str(HERE / "holdings.db")
    init_db(db_path)
    print(f"Initialized {db_path}")
