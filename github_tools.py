"""
Инструменты для работы с GitHub-репозиторием.
Используются как tools в Anthropic Messages API — Клавдия сама решает,
когда прочитать файл или закоммитить изменение.

Нужна переменная окружения GITHUB_TOKEN (Personal Access Token с правами repo)
и GITHUB_REPO в формате "owner/repo", например "maks/devagent".
"""
import base64
import os

import httpx

import storage

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")  # например "maks/devagent"
GITHUB_API = "https://api.github.com"

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def _check_config():
    if not GITHUB_TOKEN or not GITHUB_REPO:
        raise RuntimeError(
            "GITHUB_TOKEN или GITHUB_REPO не заданы в переменных окружения сервера."
        )


def read_file(path: str, branch: str = "main") -> str:
    """Читает содержимое файла из репозитория."""
    _check_config()
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
    with httpx.Client() as client:
        resp = client.get(url, headers=HEADERS, params={"ref": branch})
    if resp.status_code == 404:
        return f"Файл не найден: {path}"
    resp.raise_for_status()
    data = resp.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content


def list_files(path: str = "", branch: str = "main") -> str:
    """Список файлов и папок в указанной директории репозитория."""
    _check_config()
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
    with httpx.Client() as client:
        resp = client.get(url, headers=HEADERS, params={"ref": branch})
    if resp.status_code == 404:
        return f"Путь не найден: {path or '(корень репозитория)'}"
    resp.raise_for_status()
    items = resp.json()
    if isinstance(items, dict):
        return f"{items['name']} — это файл, не папка"
    lines = [f"{'📁' if i['type'] == 'dir' else '📄'} {i['path']}" for i in items]
    return "\n".join(lines) if lines else "Папка пуста"


def propose_file_change(path: str, content: str, commit_message: str, branch: str = "main") -> dict:
    """
    Не пишет в репозиторий — только готовит предложение изменения и сохраняет
    его в SQLite. Реальная запись происходит через apply_pending_change,
    когда пользователь явно подтвердит в интерфейсе. Переживает рестарт сервиса.
    """
    _check_config()
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"

    with httpx.Client() as client:
        existing = client.get(url, headers=HEADERS, params={"ref": branch})

    old_content = ""
    if existing.status_code == 200:
        old_content = base64.b64decode(existing.json()["content"]).decode("utf-8")

    change_id = storage.save_pending_change(path, content, commit_message, branch, old_content)

    return {
        "change_id": change_id,
        "path": path,
        "old_content": old_content,
        "new_content": content,
        "commit_message": commit_message,
    }


def apply_pending_change(change_id: str) -> str:
    """Реально коммитит ранее предложенное изменение в GitHub."""
    _check_config()
    change = storage.get_pending_change(change_id)
    if not change:
        return "Предложение не найдено — возможно, уже применено, отклонено, или id устарел."

    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{change['path']}"

    with httpx.Client() as client:
        existing = client.get(url, headers=HEADERS, params={"ref": change["branch"]})
        sha = existing.json().get("sha") if existing.status_code == 200 else None

        payload = {
            "message": change["commit_message"],
            "content": base64.b64encode(change["content"].encode("utf-8")).decode("utf-8"),
            "branch": change["branch"],
        }
        if sha:
            payload["sha"] = sha

        resp = client.put(url, headers=HEADERS, json=payload)

    if resp.status_code not in (200, 201):
        return f"Ошибка записи файла: {resp.status_code} {resp.text[:300]}"

    storage.delete_pending_change(change_id)
    action = "обновлён" if sha else "создан"
    return f"Файл {change['path']} {action}, коммит: {change['commit_message']}"


def discard_pending_change(change_id: str) -> str:
    """Отклоняет предложенное изменение без записи в репозиторий."""
    if storage.get_pending_change(change_id):
        storage.delete_pending_change(change_id)
        return "Изменение отклонено."
    return "Предложение не найдено — возможно, уже применено или отклонено."


# Описания инструментов в формате Anthropic tool use
TOOLS = [
    {
        "name": "read_file",
        "description": "Читает содержимое файла из GitHub-репозитория пользователя.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Путь к файлу в репозитории, например 'agent.py'"},
                "branch": {"type": "string", "description": "Ветка, по умолчанию main", "default": "main"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_files",
        "description": "Показывает список файлов и папок в директории репозитория.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Путь к папке, пусто для корня репозитория"},
                "branch": {"type": "string", "description": "Ветка, по умолчанию main", "default": "main"},
            },
        },
    },
    {
        "name": "propose_file_change",
        "description": (
            "Предлагает изменение файла в GitHub-репозитории — НЕ записывает его сразу. "
            "Пользователь увидит diff в интерфейсе и сам нажмёт 'Применить' или 'Отклонить'. "
            "Используй это всегда, когда меняешь существующий код или создаёшь новый файл — "
            "никогда не пиши в репозиторий напрямую."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Путь к файлу в репозитории"},
                "content": {"type": "string", "description": "Полное новое содержимое файла"},
                "commit_message": {"type": "string", "description": "Сообщение коммита"},
                "branch": {"type": "string", "description": "Ветка, по умолчанию main", "default": "main"},
            },
            "required": ["path", "content", "commit_message"],
        },
    },
]


def execute_tool(name: str, tool_input: dict) -> str:
    """Роутер: вызывает нужную функцию по имени инструмента из ответа модели."""
    try:
        if name == "read_file":
            return read_file(tool_input["path"], tool_input.get("branch", "main"))
        if name == "list_files":
            return list_files(tool_input.get("path", ""), tool_input.get("branch", "main"))
        if name == "propose_file_change":
            result = propose_file_change(
                tool_input["path"],
                tool_input["content"],
                tool_input["commit_message"],
                tool_input.get("branch", "main"),
            )
            # Модели возвращаем компактное текстовое подтверждение — полный diff уйдёт
            # пользователю отдельно, через специальное поле ответа API (см. main.py)
            return (
                f"Изменение предложено (id: {result['change_id']}) для файла {result['path']}. "
                f"Пользователь увидит его в интерфейсе и должен подтвердить применение."
            )
        return f"Неизвестный инструмент: {name}"
    except Exception as exc:
        return f"Ошибка при выполнении {name}: {exc}"
