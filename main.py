import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
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

SYSTEM_PROMPT = (
    "Ты — Claudia, личный ассистент разработчика по имени Макс. "
    "Ты помогаешь разрабатывать его проекты: MakeApp (AI-агент для вайб-кодинга приложений на smolagents + Qwen Coder), "
    "а также другие его pet-проекты. "
    "У тебя есть доступ к его GitHub-репозиторию через инструменты read_file, list_files, propose_file_change — "
    "используй их, когда нужно посмотреть или изменить реальный код, а не воображать его содержимое. "
    "propose_file_change НЕ применяет изменение сразу — пользователь увидит diff и подтвердит сам. "
    "Отвечай кратко и по делу, предлагай конкретный код и конкретные шаги, а не общие рассуждения."
)

MAX_TOOL_ITERATIONS = 8  # предохранитель от бесконечного цикла вызовов инструментов
# Задел побольше, чтобы обрезка истории не попала внутрь незакрытой пары tool_use/tool_result —
# Anthropic API требует, чтобы каждый tool_use сразу сопровождался tool_result.
MAX_HISTORY_MESSAGES = 60


class ChatRequest(BaseModel):
    message: str


class ResetRequest(BaseModel):
    confirm: bool = False


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/chat")
def chat(request: ChatRequest):
    storage.append_message("user", request.message)

    try:
        tool_log = []  # какие инструменты вызывались — покажем пользователю кратко
        proposed_changes = []  # изменения, предложенные за этот запрос — уйдут фронтенду отдельно

        for _ in range(MAX_TOOL_ITERATIONS):
            history = storage.get_history(MAX_HISTORY_MESSAGES)
            client = providers.get_client()
            model = providers.get_active_model()

            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=github_tools.TOOLS,
                messages=history,
            )

            # Модель захотела воспользоваться инструментом
            if response.stop_reason == "tool_use":
                storage.append_message("assistant", [block.model_dump() for block in response.content])

                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        tool_log.append(f"🔧 {block.name}({', '.join(f'{k}={v!r}' for k, v in block.input.items() if k != 'content')})")

                        if block.name == "propose_file_change":
                            # Особый случай: нужен полный diff для интерфейса, а не только текст модели
                            change = github_tools.propose_file_change(
                                block.input["path"],
                                block.input["content"],
                                block.input["commit_message"],
                                block.input.get("branch", "main"),
                            )
                            proposed_changes.append(change)
                            result_text = (
                                f"Изменение предложено (id: {change['change_id']}) для файла {change['path']}. "
                                f"Пользователь увидит его в интерфейсе и должен подтвердить применение."
                            )
                        else:
                            result_text = github_tools.execute_tool(block.name, block.input)

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_text,
                        })

                storage.append_message("user", tool_results)
                continue  # даём модели ещё шанс — либо снова инструмент, либо финальный ответ

            # Обычный финальный текстовый ответ
            answer = "".join(
                block.text for block in response.content if block.type == "text"
            )
            storage.append_message("assistant", answer)

            if tool_log:
                answer = "\n".join(tool_log) + "\n\n" + answer

            return JSONResponse({"answer": answer, "proposed_changes": proposed_changes})

        # Слишком много итераций подряд — останавливаемся, чтобы не жечь токены впустую
        storage.append_message(
            "assistant",
            "Остановилась после нескольких шагов с инструментами — уточни, пожалуйста, задачу.",
        )
        return JSONResponse({
            "answer": "Потребовалось слишком много шагов подряд, я остановилась. Уточни задачу или разбей её на части.",
            "proposed_changes": proposed_changes,
        })

    except Exception as exc:
        # Откатываем последнее сообщение пользователя, раз ответа не получилось
        storage.pop_last_message()
        return JSONResponse(
            {
                "answer": (
                    "Не удалось получить ответ от модели. "
                    "Проверь API-ключ выбранного провайдера, баланс или сетевой доступ с сервера. "
                    f"Ошибка: {exc}"
                ),
                "proposed_changes": [],
            },
            status_code=200,  # 200, чтобы фронтенд просто показал текст ошибки в чате
        )


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
        storage.clear_history()
        return JSONResponse({"status": "cleared"})
    return JSONResponse({"status": "not_cleared"})


@app.get("/api/health")
def health():
    history_len = len(storage.get_history(limit=10_000))
    return JSONResponse({"status": "ok", "history_len": history_len})


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
