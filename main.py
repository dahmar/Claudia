import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import anthropic

app = FastAPI(title="Claudiya")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# Ключ читается из переменной окружения ANTHROPIC_API_KEY (задаётся в Railway/Hetzner, не в коде)
client = anthropic.Anthropic()

SYSTEM_PROMPT = (
    "Ты — Claudiya, личный ассистент разработчика по имени Макс. "
    "Ты помогаешь разрабатывать его проекты: MakeApp (AI-агент для вайб-кодинга приложений на smolagents + Qwen Coder), "
    "а также другие его pet-проекты. "
    "Отвечай кратко и по делу, предлагай конкретный код и конкретные шаги, "
    "а не общие рассуждения. Если не хватает контекста (например, содержимого файла), "
    "прямо скажи, что нужно прислать файл или его фрагмент."
)

# Простая история в памяти процесса (на старте этого достаточно — сбрасывается при рестарте сервиса)
conversation_history: list[dict] = []
MAX_HISTORY_MESSAGES = 40  # ограничиваем, чтобы не раздувать контекст и стоимость


class ChatRequest(BaseModel):
    message: str


class ResetRequest(BaseModel):
    confirm: bool = False


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/manifest.json")
def manifest():
    return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")


@app.post("/api/chat")
def chat(request: ChatRequest):
    global conversation_history

    conversation_history.append({"role": "user", "content": request.message})
    conversation_history = conversation_history[-MAX_HISTORY_MESSAGES:]

    try:
        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=conversation_history,
        )
        answer = "".join(
            block.text for block in response.content if block.type == "text"
        )
        conversation_history.append({"role": "assistant", "content": answer})
        return JSONResponse({"answer": answer})

    except Exception as exc:
        # Откатываем последнее сообщение пользователя, раз ответа не получилось
        conversation_history.pop()
        return JSONResponse(
            {
                "answer": (
                    "Не удалось получить ответ от Claude. "
                    "Проверь API-ключ, баланс или сетевой доступ с сервера. "
                    f"Ошибка: {exc}"
                )
            },
            status_code=200,  # 200, чтобы фронтенд просто показал текст ошибки в чате
        )


@app.post("/api/reset")
def reset(request: ResetRequest):
    global conversation_history
    if request.confirm:
        conversation_history = []
        return JSONResponse({"status": "cleared"})
    return JSONResponse({"status": "not_cleared"})


@app.get("/api/health")
def health():
    return JSONResponse({"status": "ok", "history_len": len(conversation_history)})


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
