import asyncio
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import github_tools
import providers
import secrets_store
import storage

app = FastAPI(title="Claudia")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

SYSTEM_PROMPT_TEMPLATE = (
    "Ты — Claudia, личный ассистент разработчика по имени Макс. "
    "Ты сейчас работаешь в проекте \"{project_name}\". "
    "У тебя есть доступ к его GitHub-репозиторию через инструменты read_file, list_files, propose_file_change — "
    "используй их, когда нужно посмотреть или изменить реальный код, а не воображать его содержимое. "
    "propose_file_change НЕ применяет изменение сразу — пользователь увидит diff и подтвердит сам. "
    "Отвечай кратко и по делу, предлагай конкретный код и конкретные шаги, а не общие рассуждения."
)

MAX_TOOL_ITERATIONS = 8  # предохранитель от бесконечного цикла вызовов инструментов
# Задел побольше, чтобы обрезка истории не попала внутрь незакрытой пары tool_use/tool_result —
# Anthropic API требует, чтобы каждый tool_use сразу сопровождался tool_result.
MAX_HISTORY_MESSAGES = 60


def get_active_project_id() -> str:
    """Читает id активного проекта из настроек, с фоллбеком на первый существующий/дефолтный."""
    saved = storage.get_setting("active_project_id")
    if saved and storage.get_project(saved):
        return saved
    return storage.get_default_project_id()


class ChatRequest(BaseModel):
    message: str


class ResetRequest(BaseModel):
    confirm: bool = False


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/chat")
async def chat(request: ChatRequest):
    project_id = get_active_project_id()
    project = storage.get_project(project_id)
    storage.append_message(project_id, "user", request.message)

    def _encode(event: dict) -> str:
        """Каждая строка — отдельный JSON-объект (NDJSON)."""
        return json.dumps(event, ensure_ascii=False) + "\n"

    async def event_stream():
        try:
            proposed_changes = []
            system_prompt = SYSTEM_PROMPT_TEMPLATE.format(project_name=project["name"])

            for _ in range(MAX_TOOL_ITERATIONS):
                history = storage.get_history(project_id, MAX_HISTORY_MESSAGES)
                client = providers.get_client()
                model = providers.get_active_model()

                yield _encode({"type": "status", "text": "Думаю..."})
                # Явно отдаём управление event loop'у, чтобы ASGI-сервер реально
                # отправил чанк в сокет сейчас, а не после следующего блокирующего вызова.
                await asyncio.sleep(0)

                # client.messages.create — синхронный (блокирующий) вызов SDK; выполняем его
                # в отдельном потоке через to_thread, чтобы не блокировать event loop и не
                # мешать уже отправленным чанкам доходить до клиента вовремя.
                response = await asyncio.to_thread(
                    client.messages.create,
                    model=model,
                    max_tokens=4096,
                    system=system_prompt,
                    tools=github_tools.TOOLS,
                    messages=history,
                )

                if response.stop_reason == "tool_use":
                    storage.append_message(project_id, "assistant", [block.model_dump() for block in response.content])

                    tool_results = []
                    for block in response.content:
                        if block.type == "tool_use":
                            tool_desc = f"{block.name}({', '.join(f'{k}={v!r}' for k, v in block.input.items() if k != 'content')})"
                            yield _encode({"type": "tool_call", "text": f"🔧 {tool_desc}"})
                            await asyncio.sleep(0)

                            if block.name == "propose_file_change":
                                change = github_tools.propose_file_change(
                                    project_id,
                                    block.input["path"],
                                    block.input["content"],
                                    block.input["commit_message"],
                                    block.input.get("branch", "main"),
                                )
                                proposed_changes.append(change)
                                yield _encode({"type": "proposed_change", "change": change})
                                await asyncio.sleep(0)
                                result_text = (
                                    f"Изменение предложено (id: {change['change_id']}) для файла {change['path']}. "
                                    f"Пользователь увидит его в интерфейсе и должен подтвердить применение."
                                )
                            else:
                                result_text = github_tools.execute_tool(project_id, block.name, block.input)

                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result_text,
                            })

                    storage.append_message(project_id, "user", tool_results)
                    continue

                answer = "".join(block.text for block in response.content if block.type == "text")
                storage.append_message(project_id, "assistant", answer)

                yield _encode({"type": "final", "answer": answer})
                return

            storage.append_message(
                project_id,
                "assistant",
                "Остановилась после нескольких шагов с инструментами — уточни, пожалуйста, задачу.",
            )
            yield _encode({
                "type": "final",
                "answer": "Потребовалось слишком много шагов подряд, я остановилась. Уточни задачу или разбей её на части.",
            })

        except Exception as exc:
            storage.pop_last_message(project_id)
            error_details = _summarize_api_error(exc)
            yield _encode({
                "type": "final",
                "answer": (
                    "Не удалось получить ответ от модели. "
                    "Проверь API-ключ выбранного провайдера, баланс или сетевой доступ с сервера.\n\n"
                    f"{error_details}"
                ),
            })

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={
            # Многие PaaS-прокси (в т.ч. перед Railway) буферизуют ответ по умолчанию —
            # эти заголовки просят не копить данные, а отдавать их сразу по мере готовности.
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
        },
    )


def _summarize_api_error(exc: Exception) -> str:
    """
    Достаёт из исключения компактную диагностику вместо длинного HTML-тела ответа,
    которое некоторые прокси (в т.ч. CDN перед OpenRouter) отдают на 404/5xx.
    """
    parts = [f"Тип ошибки: {type(exc).__name__}"]

    status_code = getattr(exc, "status_code", None)
    if status_code:
        parts.append(f"HTTP статус: {status_code}")

    request = getattr(exc, "request", None)
    if request is not None and getattr(request, "url", None):
        parts.append(f"URL запроса: {request.url}")

    # Тело ответа может быть JSON с полем error, или длинным HTML — обрежем и то, и то
    message = str(exc)
    if len(message) > 500:
        message = message[:500] + "... (обрезано)"
    parts.append(f"Сообщение: {message}")

    return "\n".join(parts)


class ChangeActionRequest(BaseModel):
    change_id: str


@app.post("/api/apply-change")
def apply_change(request: ChangeActionRequest):
    result = github_tools.apply_pending_change(request.change_id)
    return JSONResponse({"result": result})


@app.post("/api/discard-change")
def discard_change(request: ChangeActionRequest):
    result = github_tools.discard_pending_change(request.change_id)
    return JSONResponse({"result": result})


# ---------- Проекты ----------

@app.get("/api/projects")
def get_projects():
    return JSONResponse({
        "active_project_id": get_active_project_id(),
        "projects": storage.list_projects(),
    })


class CreateProjectRequest(BaseModel):
    name: str


@app.post("/api/projects")
def create_project(request: CreateProjectRequest):
    if not request.name.strip():
        return JSONResponse({"error": "Имя проекта не может быть пустым"}, status_code=400)
    project_id = storage.create_project(request.name.strip())
    return JSONResponse({"project_id": project_id})


class SwitchProjectRequest(BaseModel):
    project_id: str


@app.post("/api/projects/switch")
def switch_project(request: SwitchProjectRequest):
    if not storage.get_project(request.project_id):
        return JSONResponse({"error": "Проект не найден"}, status_code=404)
    storage.set_setting("active_project_id", request.project_id)
    return JSONResponse({"active_project_id": request.project_id})


class RenameProjectRequest(BaseModel):
    project_id: str
    name: str


@app.post("/api/projects/rename")
def rename_project(request: RenameProjectRequest):
    if not request.name.strip():
        return JSONResponse({"error": "Имя проекта не может быть пустым"}, status_code=400)
    storage.rename_project(request.project_id, request.name.strip())
    return JSONResponse({"status": "renamed"})


class DeleteProjectRequest(BaseModel):
    project_id: str


@app.post("/api/projects/delete")
def delete_project(request: DeleteProjectRequest):
    all_projects = storage.list_projects()
    if len(all_projects) <= 1:
        return JSONResponse({"error": "Нельзя удалить единственный проект"}, status_code=400)

    storage.delete_project(request.project_id)

    # Если удалили активный проект — переключаемся на любой оставшийся
    if get_active_project_id() == request.project_id:
        remaining = storage.list_projects()
        storage.set_setting("active_project_id", remaining[0]["id"])

    return JSONResponse({"status": "deleted"})


class ProjectGithubRequest(BaseModel):
    project_id: str
    github_repo: str
    github_token: str


@app.post("/api/projects/github")
def set_project_github(request: ProjectGithubRequest):
    if not storage.get_project(request.project_id):
        return JSONResponse({"error": "Проект не найден"}, status_code=404)
    if not request.github_repo.strip() or not request.github_token.strip():
        return JSONResponse({"error": "Repo и токен не могут быть пустыми"}, status_code=400)

    secrets_store.save_project_github(
        request.project_id, request.github_repo.strip(), request.github_token.strip()
    )
    return JSONResponse({"status": "saved"})


class DeleteProjectGithubRequest(BaseModel):
    project_id: str


@app.post("/api/projects/github/delete")
def delete_project_github(request: DeleteProjectGithubRequest):
    secrets_store.delete_project_github(request.project_id)
    return JSONResponse({"status": "deleted"})


# ---------- Настройки моделей (общие для всех проектов) ----------

@app.get("/api/settings")
def get_settings():
    return JSONResponse({
        "active_provider": providers.get_active_provider(),
        "active_model": providers.get_active_model(),
        "providers": providers.list_providers(),
    })


class SettingsUpdateRequest(BaseModel):
    provider: str
    model: str | None = None


@app.post("/api/settings")
def update_settings(request: SettingsUpdateRequest):
    try:
        providers.set_active_provider(request.provider, request.model)
        return JSONResponse({
            "active_provider": providers.get_active_provider(),
            "active_model": providers.get_active_model(),
        })
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


class ApiKeyRequest(BaseModel):
    provider: str
    api_key: str


@app.post("/api/settings/api-key")
def save_api_key(request: ApiKeyRequest):
    if request.provider not in providers.PROVIDER_CATALOG:
        return JSONResponse({"error": f"Неизвестный провайдер: {request.provider}"}, status_code=400)
    if not request.api_key.strip():
        return JSONResponse({"error": "Ключ не может быть пустым"}, status_code=400)

    secrets_store.save_api_key(request.provider, request.api_key.strip())
    return JSONResponse({"status": "saved", "provider": request.provider})


class DeleteApiKeyRequest(BaseModel):
    provider: str


@app.post("/api/settings/api-key/delete")
def delete_api_key(request: DeleteApiKeyRequest):
    secrets_store.delete_api_key(request.provider)
    return JSONResponse({"status": "deleted", "provider": request.provider})


@app.post("/api/reset")
def reset(request: ResetRequest):
    if request.confirm:
        storage.clear_history(get_active_project_id())
        return JSONResponse({"status": "cleared"})
    return JSONResponse({"status": "not_cleared"})


@app.get("/api/health")
def health():
    history_len = len(storage.get_history(get_active_project_id(), limit=10_000))
    return JSONResponse({"status": "ok", "history_len": history_len})


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
