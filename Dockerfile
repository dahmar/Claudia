FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Директория для SQLite-базы — при деплое сюда стоит примонтировать persistent volume,
# иначе история и pending changes всё равно потеряются при пересоздании контейнера.
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
