"""
Provider-agnostic слой для Claudia.

Оба провайдера (Anthropic напрямую и OpenRouter) используют один и тот же
Anthropic Python SDK — OpenRouter совместим с форматом Anthropic Messages API
через свой base_url, так что клиент и вызовы одинаковые, отличается только
конструктор клиента и имя модели.

Активный провайдер хранится в SQLite (storage.py) — переживает рестарт,
переключается через /api/settings.
"""
import os

import storage

# Каталог провайдеров: имя -> (человекочитаемое название, дефолтная модель, фабрика клиента)
PROVIDER_CATALOG = {
    "anthropic": {
        "label": "Anthropic (напрямую)",
        "default_model": "claude-sonnet-5",
        "requires_env": ["ANTHROPIC_API_KEY"],
    },
    "openrouter": {
        "label": "OpenRouter",
        "default_model": "anthropic/claude-sonnet-4-5",
        "requires_env": ["OPENROUTER_API_KEY"],
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


def get_client():
    """
    Возвращает готовый клиент под текущий активный провайдер.
    Оба используют anthropic.Anthropic — разница только в base_url и ключе.
    Импорт anthropic сделан здесь (а не в топе модуля), чтобы providers.py
    можно было использовать (например, для чтения настроек) даже без установленного SDK.
    """
    import anthropic

    provider = get_active_provider()

    if provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY не задан в переменных окружения сервера.")
        return anthropic.Anthropic(api_key=api_key)

    if provider == "openrouter":
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY не задан в переменных окружения сервера.")
        return anthropic.Anthropic(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
        )

    raise ValueError(f"Неизвестный провайдер: {provider}")


def list_providers() -> list[dict]:
    """Для эндпоинта настроек — какие провайдеры доступны и настроен ли для них ключ."""
    result = []
    for name, info in PROVIDER_CATALOG.items():
        configured = all(os.environ.get(var) for var in info["requires_env"])
        result.append({
            "id": name,
            "label": info["label"],
            "default_model": info["default_model"],
            "configured": configured,
        })
    return result
