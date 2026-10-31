"""
One-time migration from the supplied ecosystem.db SQLite file to Render PostgreSQL.

Usage (PowerShell):
  $env:DATABASE_URL="YOUR_RENDER_EXTERNAL_DATABASE_URL"
  python migrate_sqlite_to_postgres.py "ecosystem.db"

If the destination already contains app_state, the script stops to prevent
accidental overwriting. Add --replace only when you intentionally want to
replace the destination's saved application state.
"""
import argparse
import os
import sqlite3
import sys

import psycopg2


def normalise_url(url):
    return "postgresql://" + url[len("postgres://"):] if url.startswith("postgres://") else url


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("sqlite_file", help="Path to the backed-up ecosystem.db file")
    parser.add_argument("--replace", action="store_true",
                        help="Replace an existing app_state row in the destination database")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        sys.exit("ERROR: Set DATABASE_URL to your Render PostgreSQL External Database URL first.")
    if not os.path.isfile(args.sqlite_file):
        sys.exit(f"ERROR: SQLite file not found: {args.sqlite_file}")

    source = sqlite3.connect(args.sqlite_file)
    source.row_factory = sqlite3.Row
    try:
        state_row = source.execute("SELECT data FROM app_state WHERE id = 1").fetchone()
        if not state_row:
            sys.exit("ERROR: The SQLite file has no saved app_state row; nothing was migrated.")
        token_tables = ("password_reset_tokens", "email_verification_tokens")
        tokens = {}
        for table in token_tables:
            exists = source.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            tokens[table] = source.execute(
                f"SELECT token_hash, user_id, expires_at FROM {table}"
            ).fetchall() if exists else []
    finally:
        source.close()

    destination = psycopg2.connect(normalise_url(database_url), connect_timeout=20)
    try:
        with destination:
            with destination.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS app_state (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        data TEXT NOT NULL
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS password_reset_tokens (
                        token_hash TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        expires_at DOUBLE PRECISION NOT NULL
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS email_verification_tokens (
                        token_hash TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        expires_at DOUBLE PRECISION NOT NULL
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_password_reset_user_id ON password_reset_tokens(user_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_email_verification_user_id ON email_verification_tokens(user_id)")
                cur.execute("SELECT 1 FROM app_state WHERE id = 1")
                already_has_state = cur.fetchone() is not None
                if already_has_state and not args.replace:
                    raise RuntimeError(
                        "Destination already has app_state. Stop here; use --replace only if you intend to overwrite it."
                    )
                cur.execute("""
                    INSERT INTO app_state (id, data) VALUES (1, %s)
                    ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data
                """, (state_row["data"],))
                for table, rows in tokens.items():
                    for row in rows:
                        cur.execute(
                            f"""INSERT INTO {table} (token_hash, user_id, expires_at)
                                VALUES (%s, %s, %s)
                                ON CONFLICT (token_hash) DO UPDATE
                                SET user_id = EXCLUDED.user_id, expires_at = EXCLUDED.expires_at""",
                            (row["token_hash"], row["user_id"], row["expires_at"])
                        )
        print("Migration completed successfully.")
        print(f"Application state bytes migrated: {len(state_row['data'].encode('utf-8'))}")
        print(f"Password reset tokens migrated: {len(tokens['password_reset_tokens'])}")
        print(f"Email verification tokens migrated: {len(tokens['email_verification_tokens'])}")
        print("The app_state JSON includes the platform's saved users, businesses, reviews, competitions and payment records.")
    finally:
        destination.close()


if __name__ == "__main__":
    main()
