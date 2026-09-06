"""
Персистентное хранилище на SQLite. Файл лежит в /app/data/claudia.db —
переживает рестарт процесса, но не пересоздание контейнера без volume.

Данные разделены по project_id: у каждого проекта своя история чата,
свои pending changes и свои GitHub-настройки (репозиторий + зашифрованный токен).
Настройки моделей (API-ключи провайдеров, активный провайдер) общие для всех
проектов — хранятся в settings, см. secrets_store.py и providers.py.
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
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                github_repo TEXT,
                github_token_encrypted TEXT,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_changes (
                change_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
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

        # Миграция для баз до появления проектов — ДО индексов, иначе на старых
        # базах (где CREATE TABLE IF NOT EXISTS не добавил колонку) индекс упадёт.
        _migrate_to_projects(conn)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_project ON messages(project_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_project ON pending_changes(project_id)")


def _migrate_to_projects(conn) -> None:
    default_project_id = None
    for table in ("messages", "pending_changes"):
        cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if "project_id" not in cols:
            if default_project_id is None:
                default_project_id = uuid.uuid4().hex[:8]
                conn.execute(
                    "INSERT OR IGNORE INTO projects (id, name, github_repo, github_token_encrypted, created_at) "
                    "VALUES (?, ?, NULL, NULL, ?)",
                    (default_project_id, "Основной проект", time.time()),
                )
            conn.execute(f"ALTER TABLE {table} ADD COLUMN project_id TEXT")
            conn.execute(f"UPDATE {table} SET project_id = ? WHERE project_id IS NULL", (default_project_id,))


def get_default_project_id() -> str:
    """id первого существующего проекта, создаёт 'Основной проект' при первом обращении."""
    with _connect() as conn:
        row = conn.execute("SELECT id FROM projects ORDER BY created_at ASC LIMIT 1").fetchone()
        if row:
            return row["id"]
        project_id = uuid.uuid4().hex[:8]
        conn.execute(
            "INSERT INTO projects (id, name, github_repo, github_token_encrypted, created_at) "
            "VALUES (?, ?, NULL, NULL, ?)",
            (project_id, "Основной проект", time.time()),
        )
        return project_id


# ---------- Проекты ----------

def create_project(name: str) -> str:
    project_id = uuid.uuid4().hex[:8]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO projects (id, name, github_repo, github_token_encrypted, created_at) "
            "VALUES (?, ?, NULL, NULL, ?)",
            (project_id, name, time.time()),
        )
    return project_id


def list_projects() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, github_repo, created_at FROM projects ORDER BY created_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_project(project_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return dict(row) if row else None


def rename_project(project_id: str, name: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE projects SET name = ? WHERE id = ?", (name, project_id))


def set_project_github(project_id: str, github_repo, github_token_encrypted) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE projects SET github_repo = ?, github_token_encrypted = ? WHERE id = ?",
            (github_repo, github_token_encrypted, project_id),
        )


def delete_project(project_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM messages WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM pending_changes WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


# ---------- История чата ----------

def append_message(project_id: str, role: str, content) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO messages (project_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (project_id, role, json.dumps(content, ensure_ascii=False), time.time()),
        )


def get_history(project_id: str, limit: int = 40) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE project_id = ? ORDER BY id DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()
    return [{"role": r["role"], "content": json.loads(r["content"])} for r in reversed(rows)]


def clear_history(project_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM messages WHERE project_id = ?", (project_id,))


def pop_last_message(project_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM messages WHERE id = (SELECT MAX(id) FROM messages WHERE project_id = ?)",
            (project_id,),
        )


# ---------- Pending changes ----------

def save_pending_change(project_id: str, path: str, content: str, commit_message: str, branch: str, old_content: str) -> str:
    change_id = uuid.uuid4().hex[:8]
    with _connect() as conn:
        conn.execute(
            """INSERT INTO pending_changes
               (change_id, project_id, path, content, commit_message, branch, old_content, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (change_id, project_id, path, content, commit_message, branch, old_content, time.time()),
        )
    return change_id


def get_pending_change(change_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM pending_changes WHERE change_id = ?", (change_id,)).fetchone()
    return dict(row) if row else None


def delete_pending_change(change_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM pending_changes WHERE change_id = ?", (change_id,))


# ---------- Настройки (общие для всех проектов) ----------

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


def delete_setting(key: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))


init_db()
