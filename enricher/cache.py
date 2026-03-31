"""
SQLite cache for Amazon orders.
Prevents redundant Amazon requests and speeds up repeated runs.
Cache file is local-only and excluded from git.
"""
import sqlite3
import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from .config import CACHE_DB

logger = logging.getLogger(__name__)


def _connect() -> sqlite3.Connection:
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CACHE_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id          TEXT PRIMARY KEY,
                order_date        TEXT NOT NULL,
                amount            REAL NOT NULL,
                items             TEXT NOT NULL,
                end_to_end_ref    TEXT,
                fetched_at        TEXT NOT NULL
            )
        """)
        # Migrate: add end_to_end_ref column if upgrading from older schema
        try:
            conn.execute("ALTER TABLE orders ADD COLUMN end_to_end_ref TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS enriched_transactions (
                transaction_id  TEXT PRIMARY KEY,
                order_id        TEXT,
                enriched_at     TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(order_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_ref ON orders(end_to_end_ref)")


def get_order_by_date_amount(order_date: date, amount: float, tolerance: float = 0.01) -> Optional[dict]:
    """Find a cached order matching date (±ORDER_LOOKUP_DAYS) and amount."""
    with _connect() as conn:
        row = conn.execute("""
            SELECT * FROM orders
            WHERE ABS(amount - ?) < ?
              AND order_date = ?
        """, (amount, tolerance, order_date.isoformat())).fetchone()
        if row:
            return {**row, "items": json.loads(row["items"])}
    return None


def get_order_by_reference(end_to_end_ref: str) -> Optional[dict]:
    """Find a cached order by its SEPA end-to-end reference."""
    if not end_to_end_ref:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE end_to_end_ref = ?",
            (end_to_end_ref,)
        ).fetchone()
        if row:
            return {**row, "items": json.loads(row["items"])}
    return None


def get_order_by_id(order_id: str) -> Optional[dict]:
    """Find a cached order by its Amazon order ID."""
    if not order_id:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE order_id = ?",
            (order_id,)
        ).fetchone()
        if row:
            return {**row, "items": json.loads(row["items"])}
    return None


def save_order(order_id: str, order_date: date, amount: float, items: list[str], end_to_end_ref: str = "") -> None:
    with _connect() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO orders (order_id, order_date, amount, items, end_to_end_ref, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (order_id, order_date.isoformat(), amount, json.dumps(items), end_to_end_ref or None, datetime.now().isoformat()))
    logger.debug(f"Cached order {order_id}: {items}")


def is_already_enriched(transaction_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM enriched_transactions WHERE transaction_id = ?",
            (transaction_id,)
        ).fetchone()
        return row is not None


def mark_enriched(transaction_id: str, order_id: str) -> None:
    with _connect() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO enriched_transactions (transaction_id, order_id, enriched_at)
            VALUES (?, ?, ?)
        """, (transaction_id, order_id, datetime.now().isoformat()))
