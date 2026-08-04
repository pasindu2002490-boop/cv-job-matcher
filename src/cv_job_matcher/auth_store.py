from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from werkzeug.security import check_password_hash, generate_password_hash


@dataclass(frozen=True)
class User:
    id: str
    email: str
    password_hash: str
    created_at: str
    is_admin: bool = False
    free_runs_used: int = 0


@dataclass(frozen=True)
class Subscription:
    id: str
    user_id: str
    status: str
    amount_lkr: int
    currency: str
    payment_method: str
    reference: str
    note: str
    created_at: str
    starts_at: str | None
    ends_at: str | None


class AuthStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    free_runs_used INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    amount_lkr INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    payment_method TEXT NOT NULL,
                    reference TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    starts_at TEXT,
                    ends_at TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
                CREATE INDEX IF NOT EXISTS idx_subscriptions_user
                    ON subscriptions(user_id, status, ends_at);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(users)").fetchall()
            }
            if "free_runs_used" not in columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN free_runs_used INTEGER NOT NULL DEFAULT 0"
                )

    def create_user(self, email: str, password: str, *, is_admin: bool = False) -> User:
        user = User(
            id=uuid4().hex,
            email=email.strip().lower(),
            password_hash=generate_password_hash(password),
            created_at=_utcnow(),
            is_admin=is_admin,
            free_runs_used=0,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users (id, email, password_hash, is_admin, free_runs_used, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user.id,
                    user.email,
                    user.password_hash,
                    1 if is_admin else 0,
                    user.free_runs_used,
                    user.created_at,
                ),
            )
        return user

    def get_user_by_email(self, email: str) -> User | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE email = ? COLLATE NOCASE",
                (email.strip().lower(),),
            ).fetchone()
        return _user_from_row(row) if row else None

    def get_user(self, user_id: str) -> User | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return _user_from_row(row) if row else None

    def authenticate(self, email: str, password: str) -> User | None:
        user = self.get_user_by_email(email)
        if user is None or not check_password_hash(user.password_hash, password):
            return None
        return user

    def free_runs_remaining(self, user_id: str, limit: int) -> int:
        user = self.get_user(user_id)
        if user is None:
            return 0
        return max(0, int(limit) - int(user.free_runs_used))

    def consume_free_run(self, user_id: str, limit: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT free_runs_used FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                return False
            used = int(row["free_runs_used"] or 0)
            if used >= int(limit):
                return False
            connection.execute(
                "UPDATE users SET free_runs_used = ? WHERE id = ?",
                (used + 1, user_id),
            )
            return True

    def create_payment_request(
        self,
        user_id: str,
        *,
        amount_lkr: int,
        payment_method: str,
        reference: str = "",
        note: str = "",
    ) -> Subscription:
        subscription = Subscription(
            id=uuid4().hex,
            user_id=user_id,
            status="pending",
            amount_lkr=amount_lkr,
            currency="LKR",
            payment_method=payment_method,
            reference=reference.strip(),
            note=note.strip(),
            created_at=_utcnow(),
            starts_at=None,
            ends_at=None,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO subscriptions (
                    id, user_id, status, amount_lkr, currency, payment_method,
                    reference, note, created_at, starts_at, ends_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    subscription.id,
                    subscription.user_id,
                    subscription.status,
                    subscription.amount_lkr,
                    subscription.currency,
                    subscription.payment_method,
                    subscription.reference,
                    subscription.note,
                    subscription.created_at,
                    subscription.starts_at,
                    subscription.ends_at,
                ),
            )
        return subscription

    def activate_subscription(
        self,
        subscription_id: str,
        *,
        days: int = 30,
        payment_reference: str | None = None,
    ) -> Subscription | None:
        starts = datetime.now(timezone.utc)
        ends = starts + timedelta(days=days)
        with self._connect() as connection:
            if payment_reference:
                connection.execute(
                    """
                    UPDATE subscriptions
                    SET status = 'active', starts_at = ?, ends_at = ?, reference = ?
                    WHERE id = ?
                    """,
                    (
                        starts.isoformat(),
                        ends.isoformat(),
                        payment_reference.strip(),
                        subscription_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE subscriptions
                    SET status = 'active', starts_at = ?, ends_at = ?
                    WHERE id = ?
                    """,
                    (starts.isoformat(), ends.isoformat(), subscription_id),
                )
            row = connection.execute(
                "SELECT * FROM subscriptions WHERE id = ?",
                (subscription_id,),
            ).fetchone()
        return _subscription_from_row(row) if row else None

    def get_subscription(self, subscription_id: str) -> Subscription | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM subscriptions WHERE id = ?",
                (subscription_id,),
            ).fetchone()
        return _subscription_from_row(row) if row else None

    def list_pending_subscriptions(self) -> list[Subscription]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM subscriptions
                WHERE status = 'pending'
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [_subscription_from_row(row) for row in rows]

    def active_subscription(self, user_id: str) -> Subscription | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM subscriptions
                WHERE user_id = ?
                  AND status = 'active'
                  AND ends_at IS NOT NULL
                  AND ends_at > ?
                ORDER BY ends_at DESC
                LIMIT 1
                """,
                (user_id, now),
            ).fetchone()
        return _subscription_from_row(row) if row else None

    def latest_subscription(self, user_id: str) -> Subscription | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM subscriptions
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        return _subscription_from_row(row) if row else None


def _user_from_row(row: sqlite3.Row) -> User:
    keys = row.keys()
    return User(
        id=row["id"],
        email=row["email"],
        password_hash=row["password_hash"],
        created_at=row["created_at"],
        is_admin=bool(row["is_admin"]),
        free_runs_used=int(row["free_runs_used"] if "free_runs_used" in keys else 0),
    )


def _subscription_from_row(row: sqlite3.Row) -> Subscription:
    return Subscription(
        id=row["id"],
        user_id=row["user_id"],
        status=row["status"],
        amount_lkr=int(row["amount_lkr"]),
        currency=row["currency"],
        payment_method=row["payment_method"],
        reference=row["reference"] or "",
        note=row["note"] or "",
        created_at=row["created_at"],
        starts_at=row["starts_at"],
        ends_at=row["ends_at"],
    )


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
