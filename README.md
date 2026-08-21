# Claudiya

Личный dev-ассистент на Claude — доступ через браузер (PWA) с любого устройства, включая телефон. Не публичный продукт, только для личного использования.

## Локальный запуск (для проверки на Mac)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
uvicorn main:app --reload
```

Открой http://localhost:8000

## Деплой на Hetzner (рекомендуется — сервер физически не в России, обращение к Anthropic API идёт с "чистого" IP)

1. Создай VPS на Hetzner (самый дешёвый CX22 достаточно для старта)
2. Установи Docker на сервере:
   ```bash
   curl -fsSL https://get.docker.com | sh
   ```
3. Скопируй проект на сервер (git clone или scp)
4. Собери и запусти:
   ```bash
   docker build -t claudiya .
   docker run -d -p 80:8000 \
     -e ANTHROPIC_API_KEY=sk-ant-... \
     --restart unless-stopped \
     --name claudiya \
     claudiya
   ```
5. Открой `http://<IP-сервера>` в браузере телефона

### Рекомендуется: HTTPS через домен

PWA и "Добавить на экран" работают надёжнее с HTTPS. Проще всего — Caddy как реверс-прокси перед контейнером, он сам получает сертификат Let's Encrypt:

```bash
# на сервере, отдельным контейнером или через apt install caddy
caddy reverse-proxy --from claudiya.твой-домен.ru --to localhost:8000
```

Для этого домен должен указывать на IP сервера (A-запись).

## Деплой на Railway (проще, но помни про гео-ограничения Anthropic — сервис Railway обычно тоже вне России, что и нужно)

1. Новый проект → Deploy from GitHub repo
2. В Variables добавить `ANTHROPIC_API_KEY`
3. Railway сам подхватит Dockerfile

## Важно

- API-ключ никогда не хранится в коде — только в переменных окружения сервера
- История чата сейчас хранится в памяти процесса (сбрасывается при рестарте) — этого достаточно для личного использования; если нужна персистентность, следующим шагом можно добавить SQLite
- Это личный инструмент, не для доступа посторонних пользователей — не давай ссылку на сервис никому
