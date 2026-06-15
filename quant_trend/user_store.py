from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


PBKDF2_ITERATIONS = 260_000
SESSION_DAYS = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_iterations, raw_salt, raw_digest = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(raw_iterations)
        salt = base64.b64decode(raw_salt.encode("ascii"))
        expected = base64.b64decode(raw_digest.encode("ascii"))
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return secrets.compare_digest(actual, expected)


def _session_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _row_to_user(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "username": row["username"],
        "display_name": row["display_name"] or row["username"],
        "is_admin": bool(row["is_admin"]),
    }


def auth_mode_enabled() -> bool:
    return os.getenv("APP_AUTH_MODE", "").strip().lower() in {"users", "db", "sqlite"} or bool(os.getenv("APP_SEED_USERS", "").strip())


class UserStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    display_name TEXT,
                    password_hash TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_state (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL,
                    run_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    kind TEXT,
                    provider TEXT,
                    prompt TEXT,
                    asof TEXT,
                    regime TEXT,
                    regime_score REAL,
                    orders_count INTEGER NOT NULL DEFAULT 0,
                    gross REAL,
                    PRIMARY KEY (user_id, run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_runs_user_created ON runs(user_id, created_at DESC);
                """
            )

    def seed_from_env(self) -> list[dict]:
        raw = os.getenv("APP_SEED_USERS", "").strip()
        if not raw:
            return []
        users = []
        if raw.startswith("["):
            payload = json.loads(raw)
            for index, item in enumerate(payload):
                users.append(
                    self.upsert_user(
                        username=str(item["username"]),
                        password=str(item["password"]),
                        display_name=str(item.get("display_name") or item["username"]),
                        is_admin=bool(item.get("is_admin", index == 0)),
                    )
                )
            return users

        for index, part in enumerate(item.strip() for item in raw.split(",") if item.strip()):
            pieces = part.split(":")
            if len(pieces) < 2:
                continue
            username = pieces[0].strip()
            password = pieces[1].strip()
            display_name = pieces[2].strip() if len(pieces) >= 3 and pieces[2].strip() else username
            is_admin = (len(pieces) >= 4 and pieces[3].strip().lower() in {"1", "true", "yes", "admin"}) or index == 0
            users.append(self.upsert_user(username, password, display_name, is_admin))
        return users

    def upsert_user(self, username: str, password: str, display_name: str | None = None, is_admin: bool = False) -> dict:
        username = username.strip()
        if not username:
            raise ValueError("username is required")
        now = _now_iso()
        password_hash = _hash_password(password)
        with self.connect() as conn:
            existing = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE users
                    SET display_name = ?, password_hash = ?, is_admin = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (display_name or username, password_hash, 1 if is_admin else 0, now, existing["id"]),
                )
                user_id = int(existing["id"])
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO users (username, display_name, password_hash, is_admin, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (username, display_name or username, password_hash, 1 if is_admin else 0, now, now),
                )
                user_id = int(cursor.lastrowid)
        return self.get_user(user_id) or {"id": user_id, "username": username, "display_name": display_name or username, "is_admin": is_admin}

    def get_user(self, user_id: int) -> dict | None:
        with self.connect() as conn:
            return _row_to_user(conn.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone())

    def list_users(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [_row_to_user(row) for row in rows if row is not None]

    def authenticate(self, username: str, password: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
        if not row or not _verify_password(password, row["password_hash"]):
            return None
        return _row_to_user(row)

    def create_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=SESSION_DAYS)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (token_hash, user_id, created_at, last_seen_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (_session_hash(token), int(user_id), now.isoformat(), now.isoformat(), expires.isoformat()),
            )
        return token

    def get_session_user(self, token: str | None) -> dict | None:
        if not token:
            return None
        token_hash = _session_hash(token)
        now = datetime.now(timezone.utc)
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT users.*
                FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ?
                """,
                (token_hash, now.isoformat()),
            ).fetchone()
            if not row:
                return None
            conn.execute("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (now.isoformat(), token_hash))
        return _row_to_user(row)

    def delete_session(self, token: str | None) -> None:
        if not token:
            return
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_session_hash(token),))

    def cleanup_sessions(self) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (datetime.now(timezone.utc).isoformat(),))

    def load_state(self, user_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT state_json FROM user_state WHERE user_id = ?", (int(user_id),)).fetchone()
        return json.loads(row["state_json"]) if row else None

    def save_state(self, user_id: int, state: dict) -> None:
        payload = json.dumps(state, ensure_ascii=False)
        now = _now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO user_state (user_id, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET state_json = excluded.state_json, updated_at = excluded.updated_at
                """,
                (int(user_id), payload, now),
            )

    def save_run(self, user_id: int, run_id: str, run: dict) -> None:
        meta = run.get("run", {})
        regime = run.get("regime", {})
        portfolio = run.get("portfolio", {})
        created_at = meta.get("created_at") or run.get("asof") or _now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (user_id, run_id, run_json, created_at, kind, provider, prompt, asof, regime, regime_score, orders_count, gross)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, run_id) DO UPDATE SET
                    run_json = excluded.run_json,
                    created_at = excluded.created_at,
                    kind = excluded.kind,
                    provider = excluded.provider,
                    prompt = excluded.prompt,
                    asof = excluded.asof,
                    regime = excluded.regime,
                    regime_score = excluded.regime_score,
                    orders_count = excluded.orders_count,
                    gross = excluded.gross
                """,
                (
                    int(user_id),
                    run_id,
                    json.dumps(run, ensure_ascii=False),
                    created_at,
                    meta.get("kind", "manual"),
                    meta.get("provider"),
                    meta.get("prompt", ""),
                    run.get("asof"),
                    regime.get("label"),
                    regime.get("score"),
                    len(run.get("orders", [])),
                    portfolio.get("current_gross_exposure"),
                ),
            )

    def list_runs(self, user_id: int, limit: int = 50) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, created_at, kind, provider, prompt, asof, regime, regime_score, orders_count, gross
                FROM runs
                WHERE user_id = ?
                ORDER BY created_at DESC, run_id DESC
                LIMIT ?
                """,
                (int(user_id), int(limit)),
            ).fetchall()
        return [
            {
                "id": row["run_id"],
                "asof": row["asof"],
                "kind": row["kind"] or "manual",
                "provider": row["provider"],
                "prompt": row["prompt"] or "",
                "regime": row["regime"],
                "regime_score": row["regime_score"],
                "orders_count": row["orders_count"],
                "gross": row["gross"],
                "path": f"sqlite:runs/{row['run_id']}",
            }
            for row in rows
        ]

    def load_run(self, user_id: int, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT run_json FROM runs WHERE user_id = ? AND run_id = ?", (int(user_id), run_id)).fetchone()
        return json.loads(row["run_json"]) if row else None

    def migrate_legacy(self, user_id: int, state_path: Path, portfolio_path: Path, runs_dir: Path) -> None:
        if self.load_state(user_id) is None:
            state = {}
            if state_path.exists():
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                except Exception:
                    state = {}
            if "portfolio" not in state and portfolio_path.exists():
                try:
                    state["portfolio"] = json.loads(portfolio_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            if state:
                self.save_state(user_id, state)

        if not runs_dir.exists():
            return
        for path in runs_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            run_id = payload.get("run", {}).get("id") or path.stem
            if run_id:
                self.save_run(user_id, run_id, payload)
