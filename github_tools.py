"""
Инструменты для работы с GitHub-репозиторием — привязаны к конкретному проекту.

Каждый проект хранит свой github_repo (открытым текстом, не секрет) и свой
github_token (зашифрованным через secrets_store, как и API-ключи моделей).
Раньше это были глобальные переменные окружения GITHUB_TOKEN/GITHUB_REPO —
теперь у каждого проекта Claudia могут быть свои, задаются через UI.
"""
import base64

import secrets_store
import storage

GITHUB_API = "https://api.github.com"


def _get_credentials(project_id: str) -> tuple[str, str]:
    """Возвращает (token, repo) для проекта, либо бросает понятную ошибку."""
    project = storage.get_project(project_id)
    if not project:
        raise RuntimeError(f"Проект {project_id} не найден.")

    repo = project.get("github_repo")
    token = secrets_store.get_project_github_token(project_id)

    if not repo or not token:
        raise RuntimeError(
            "GitHub не настроен для этого проекта. "
            "Добавь токен и репозиторий в настройках проекта (⚙️ → GitHub)."
        )
    return token, repo


def _headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def read_file(project_id: str, path: str, branch: str = "main") -> str:
    """Читает содержимое файла из репозитория проекта."""
    import httpx
    token, repo = _get_credentials(project_id)
    url = f"{GITHUB_API}/repos/{repo}/contents/{path}"
    with httpx.Client() as client:
        resp = client.get(url, headers=_headers(token), params={"ref": branch})
    if resp.status_code == 404:
        return f"Файл не найден: {path}"
    resp.raise_for_status()
    data = resp.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content


def list_files(project_id: str, path: str = "", branch: str = "main") -> str:
    """Список файлов и папок в указанной директории репозитория проекта."""
    import httpx
    token, repo = _get_credentials(project_id)
    url = f"{GITHUB_API}/repos/{repo}/contents/{path}"
    with httpx.Client() as client:
        resp = client.get(url, headers=_headers(token), params={"ref": branch})
    if resp.status_code == 404:
        return f"Путь не найден: {path or '(корень репозитория)'}"
    resp.raise_for_status()
    items = resp.json()
    if isinstance(items, dict):
        return f"{items['name']} — это файл, не папка"
    lines = [f"{'📁' if i['type'] == 'dir' else '📄'} {i['path']}" for i in items]
    return "\n".join(lines) if lines else "Папка пуста"


def propose_file_change(project_id: str, path: str, content: str, commit_message: str, branch: str = "main") -> dict:
    """
    Не пишет в репозиторий — только готовит предложение изменения и сохраняет
    его в SQLite (привязанным к проекту). Реальная запись происходит через
    apply_pending_change, когда пользователь явно подтвердит в интерфейсе.
    """
    token, repo = _get_credentials(project_id)
    url = f"{GITHUB_API}/repos/{repo}/contents/{path}"

    import httpx
    with httpx.Client() as client:
        existing = client.get(url, headers=_headers(token), params={"ref": branch})

    old_content = ""
    if existing.status_code == 200:
        old_content = base64.b64decode(existing.json()["content"]).decode("utf-8")

    change_id = storage.save_pending_change(project_id, path, content, commit_message, branch, old_content)

    return {
        "change_id": change_id,
        "path": path,
        "old_content": old_content,
        "new_content": content,
        "commit_message": commit_message,
    }


def apply_pending_change(change_id: str) -> str:
    """Реально коммитит ранее предложенное изменение в GitHub (креды берутся из проекта, к которому привязан change)."""
    change = storage.get_pending_change(change_id)
    if not change:
        return "Предложение не найдено — возможно, уже применено, отклонено, или id устарел."

    token, repo = _get_credentials(change["project_id"])
    url = f"{GITHUB_API}/repos/{repo}/contents/{change['path']}"

    import httpx
    with httpx.Client() as client:
        existing = client.get(url, headers=_headers(token), params={"ref": change["branch"]})
        sha = existing.json().get("sha") if existing.status_code == 200 else None

        payload = {
            "message": change["commit_message"],
            "content": base64.b64encode(change["content"].encode("utf-8")).decode("utf-8"),
            "branch": change["branch"],
        }
        if sha:
            payload["sha"] = sha

        resp = client.put(url, headers=_headers(token), json=payload)

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


# Описания инструментов в формате Anthropic tool use.
# project_id НЕ входит в схему — модель не должна его указывать, он подставляется
# сервером автоматически (см. main.py) из текущего активного проекта пользователя.
TOOLS = [
    {
        "name": "read_file",
        "description": "Читает содержимое файла из GitHub-репозитория текущего проекта.",
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
        "description": "Показывает список файлов и папок в директории репозитория текущего проекта.",
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
            "Предлагает изменение файла в GitHub-репозитории текущего проекта — НЕ записывает его сразу. "
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


def execute_tool(project_id: str, name: str, tool_input: dict) -> str:
    """Роутер: вызывает нужную функцию по имени инструмента из ответа модели, подставляя project_id."""
    try:
        if name == "read_file":
            return read_file(project_id, tool_input["path"], tool_input.get("branch", "main"))
        if name == "list_files":
            return list_files(project_id, tool_input.get("path", ""), tool_input.get("branch", "main"))
        return f"Неизвестный инструмент: {name}"
    except Exception as exc:
        return f"Ошибка при выполнении {name}: {exc}"
