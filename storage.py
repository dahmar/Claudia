"""
Персистентное хранилище на SQLite.
Переживает рестарт сервиса (в отличие от простых dict/list в памяти),
но остаётся простым файлом — без отдельной БД для личного однопользовательского инструмента.

Файл базы данных лежит в /app/data/claudia.db внутри контейнера.
На Railway и Hetzner при обычном docker run это не переживёт полное пересоздание
контейнера без volume — см. README про подключение постоянного диска.
"""
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.environ.get("CLAUDIA_DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "claudia.db"


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_changes (
                change_id TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                content TEXT NOT NULL,
                commit_message TEXT NOT NULL,
                branch TEXT NOT NULL,
                old_content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)


# ---------- История чата ----------

def append_message(role: str, content) -> None:
    """content может быть строкой или списком блоков (для tool_use/tool_result) — храним как JSON."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO messages (role, content, created_at) VALUES (?, ?, ?)",
            (role, json.dumps(content, ensure_ascii=False), time.time()),
        )


def get_history(limit: int = 40) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    # Разворачиваем обратно в хронологический порядок
    return [{"role": r["role"], "content": json.loads(r["content"])} for r in reversed(rows)]


def clear_history() -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM messages")


def pop_last_message() -> None:
    """Удаляет последнюю запись — используется для отката при ошибке API."""
    with _connect() as conn:
        conn.execute("DELETE FROM messages WHERE id = (SELECT MAX(id) FROM messages)")


# ---------- Pending changes ----------

def save_pending_change(path: str, content: str, commit_message: str, branch: str, old_content: str) -> str:
    change_id = uuid.uuid4().hex[:8]
    with _connect() as conn:
        conn.execute(
            """INSERT INTO pending_changes
               (change_id, path, content, commit_message, branch, old_content, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (change_id, path, content, commit_message, branch, old_content, time.time()),
        )
    return change_id


def get_pending_change(change_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM pending_changes WHERE change_id = ?", (change_id,)
        ).fetchone()
    return dict(row) if row else None


def delete_pending_change(change_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM pending_changes WHERE change_id = ?", (change_id,))


# ---------- Настройки (провайдер, модель и т.п.) ----------

def get_setting(key: str) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


init_db()
