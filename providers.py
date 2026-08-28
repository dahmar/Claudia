"""
Provider-agnostic слой для Claudia.

Оба провайдера (Anthropic напрямую и OpenRouter) используют один и тот же
Anthropic Python SDK — OpenRouter совместим с форматом Anthropic Messages API
через свой base_url, так что клиент и вызовы одинаковые, отличается только
base_url и то, откуда берётся ключ.

Приоритет получения ключа: сначала сохранённый через UI (зашифрованный,
secrets_store.py), если его нет — переменная окружения. Так можно и завести
ключ прямо в интерфейсе с телефона, и по-прежнему настраивать через env vars
на сервере, если так удобнее.

Активный провайдер хранится в SQLite (storage.py) — переживает рестарт,
переключается через /api/settings.
"""
import os

import storage
import secrets_store

# Каталог провайдеров: имя -> метаданные, включая откуда брать env var и base_url для клиента
PROVIDER_CATALOG = {
    "anthropic": {
        "label": "Anthropic (напрямую)",
        "default_model": "claude-sonnet-5",
        "env_var": "ANTHROPIC_API_KEY",
        "base_url": None,  # None = дефолтный base_url самого SDK
    },
    "openrouter": {
        "label": "OpenRouter",
        "default_model": "anthropic/claude-sonnet-4-5",
        "env_var": "OPENROUTER_API_KEY",
        # ВАЖНО: для Anthropic SDK (anthropic.Anthropic) нужен путь /api, БЕЗ /v1 —
        # именно так OpenRouter подключает свой "Anthropic Skin" (нативный формат Messages API).
        # /api/v1 — это отдельный путь под OpenAI-совместимый формат, для другого SDK.
        "base_url": "https://openrouter.ai/api",
    },
}

DEFAULT_PROVIDER = "anthropic"


def get_active_provider() -> str:
    """Читает текущий выбранный провайдер из настроек (SQLite), с фоллбеком на дефолт."""
    saved = storage.get_setting("active_provider")
    if saved and saved in PROVIDER_CATALOG:
        return saved
    return DEFAULT_PROVIDER


def get_active_model() -> str:
    """Читает выбранную модель, с фоллбеком на дефолтную модель активного провайдера."""
    provider = get_active_provider()
    saved = storage.get_setting("active_model")
    if saved:
        return saved
    return PROVIDER_CATALOG[provider]["default_model"]


def set_active_provider(provider: str, model: str | None = None) -> None:
    if provider not in PROVIDER_CATALOG:
        raise ValueError(f"Неизвестный провайдер: {provider}")
    storage.set_setting("active_provider", provider)
    storage.set_setting("active_model", model or PROVIDER_CATALOG[provider]["default_model"])


def _resolve_api_key(provider: str) -> str | None:
    """Сохранённый через UI ключ имеет приоритет над env var."""
    from_ui = secrets_store.get_api_key(provider)
    if from_ui:
        return from_ui
    env_var = PROVIDER_CATALOG[provider]["env_var"]
    return os.environ.get(env_var)


def get_client():
    """
    Возвращает готовый клиент под текущий активный провайдер.
    Импорт anthropic сделан здесь (а не в топе модуля), чтобы providers.py
    можно было использовать (например, для чтения настроек) даже без установленного SDK.
    """
    import anthropic

    provider = get_active_provider()
    info = PROVIDER_CATALOG[provider]

    api_key = _resolve_api_key(provider)
    if not api_key:
        raise RuntimeError(
            f"Нет API-ключа для провайдера {info['label']}. "
            f"Добавь его в настройках (⚙️) или через переменную окружения {info['env_var']}."
        )

    kwargs = {"api_key": api_key}
    if info["base_url"]:
        kwargs["base_url"] = info["base_url"]

    return anthropic.Anthropic(**kwargs)


def list_providers() -> list[dict]:
    """Для эндпоинта настроек — какие провайдеры доступны и откуда взят ключ."""
    result = []
    for name, info in PROVIDER_CATALOG.items():
        has_ui_key = secrets_store.has_api_key(name)
        has_env_key = bool(os.environ.get(info["env_var"]))
        result.append({
            "id": name,
            "label": info["label"],
            "default_model": info["default_model"],
            "configured": has_ui_key or has_env_key,
            "key_source": "ui" if has_ui_key else ("env" if has_env_key else None),
        })
    return result
