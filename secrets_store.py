"""
Шифрованное хранение API-ключей провайдеров (Anthropic, OpenRouter и т.д.).

Мастер-ключ шифрования генерируется автоматически при первом запуске и
хранится отдельным файлом на диске (не в git, не в SQLite вместе с данными,
которые он же и защищает). Сами API-ключи хранятся в SQLite зашифрованными —
даже если файл базы утечёт, ключи в нём бесполезны без master.key.

Модель угроз, которую это закрывает:
- Случайный доступ к содержимому SQLite (бэкап, лог, дамп) не раскрывает ключи.
Модель угроз, которую это НЕ закрывает:
- Полный доступ к файловой системе сервера (получит и master.key, и базу) —
  это защищает только "разделение" секретов, а не компрометацию всего сервера.
"""
import os
from pathlib import Path

from cryptography.fernet import Fernet

import storage

DATA_DIR = Path(os.environ.get("CLAUDIA_DATA_DIR", "/app/data"))
MASTER_KEY_PATH = DATA_DIR / "master.key"


def _get_or_create_master_key() -> bytes:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if MASTER_KEY_PATH.exists():
        return MASTER_KEY_PATH.read_bytes()

    key = Fernet.generate_key()
    MASTER_KEY_PATH.write_bytes(key)
    # Права доступа только для владельца процесса — насколько это возможно на конкретной ОС/ФС.
    try:
        os.chmod(MASTER_KEY_PATH, 0o600)
    except OSError:
        pass  # На некоторых ФС (например, при монтировании volume в Windows-хосте) chmod не сработает — не критично.
    return key


def _get_fernet() -> Fernet:
    return Fernet(_get_or_create_master_key())


def save_api_key(provider: str, api_key: str) -> None:
    """Шифрует и сохраняет API-ключ для провайдера в SQLite."""
    encrypted = _get_fernet().encrypt(api_key.encode("utf-8")).decode("utf-8")
    storage.set_setting(f"api_key:{provider}", encrypted)


def get_api_key(provider: str) -> str | None:
    """
    Возвращает расшифрованный API-ключ для провайдера, если он был сохранён через UI.
    Возвращает None, если ключ не сохранён (тогда main.py должен фолбэкнуться на env var).
    """
    encrypted = storage.get_setting(f"api_key:{provider}")
    if not encrypted:
        return None
    try:
        return _get_fernet().decrypt(encrypted.encode("utf-8")).decode("utf-8")
    except Exception:
        # Ключ шифрования сменился/повреждён — считаем, что сохранённого ключа нет,
        # не роняем всё приложение из-за этого.
        return None


def delete_api_key(provider: str) -> None:
    storage.delete_setting(f"api_key:{provider}")


def has_api_key(provider: str) -> bool:
    return storage.get_setting(f"api_key:{provider}") is not None
