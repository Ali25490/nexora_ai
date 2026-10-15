FROM python:3.12-slim

WORKDIR /app

# Копируем requirements и устанавливаем зависимости
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь backend
COPY backend/ .

# Создаём папку для базы
RUN mkdir -p /data

ENV DATABASE_URL=sqlite:////data/nexora.db
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
