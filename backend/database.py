import os
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from urllib.parse import urlparse, unquote

FREE_SEARCH_LIMIT = 3

_CREATE_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        full_name TEXT DEFAULT '',
        agency_name TEXT DEFAULT '',
        stripe_customer_id TEXT,
        subscription_status TEXT DEFAULT 'free',
        subscription_plan TEXT DEFAULT '',
        searches_used INTEGER DEFAULT 0,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS leads (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        business_type TEXT,
        address TEXT,
        phone TEXT,
        website TEXT,
        presence_score INTEGER DEFAULT 0,
        presence_label TEXT DEFAULT 'No website',
        status TEXT DEFAULT 'new',
        notes TEXT DEFAULT '',
        created_at TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS outreach (
        id SERIAL PRIMARY KEY,
        lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL,
        email_subject TEXT,
        email_body TEXT,
        sms_text TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS searches (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        query TEXT,
        location TEXT,
        results_count INTEGER DEFAULT 0,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )
    """,
]


def _connect():
    url = os.getenv("DATABASE_URL", "")
    p = urlparse(url)
    return psycopg2.connect(
        host=p.hostname,
        port=p.port or 5432,
        dbname=(p.path or "/postgres").lstrip("/"),
        user=unquote(p.username or ""),
        password=unquote(p.password or ""),
        sslmode="require",
        cursor_factory=psycopg2.extras.RealDictCursor,
        connect_timeout=10,
    )


_ALTER_TABLES = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS scanner_addon_status TEXT DEFAULT 'inactive'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS scanner_stripe_subscription_id TEXT",
]


def init_db():
    conn = _connect()
    try:
        with conn.cursor() as cur:
            for stmt in _CREATE_TABLES:
                cur.execute(stmt)
            for stmt in _ALTER_TABLES:
                cur.execute(stmt)
        conn.commit()
    finally:
        conn.close()


class _Conn:
    """Thin psycopg2 wrapper that mimics SQLite's connection.execute() interface."""

    def __init__(self, conn):
        self._conn = conn
        self._cur = conn.cursor()

    def execute(self, sql, params=None):
        self._cur.execute(sql, params or ())
        return self._cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        try:
            self._cur.close()
        finally:
            self._conn.close()


@contextmanager
def get_conn():
    conn = _connect()
    wrapped = _Conn(conn)
    try:
        yield wrapped
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        wrapped.close()
