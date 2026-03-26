# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: установка Node.js зависимостей (LiteParse CLI)
# ─────────────────────────────────────────────────────────────────────────────
FROM node:20-slim AS node-builder

# Устанавливаем @llamaindex/liteparse глобально.
# --prefer-offline нет смысла без кэша, --no-audit ускоряет сборку.
RUN npm install -g @llamaindex/liteparse@1.3.0 --no-audit --no-fund \
    && lit --version


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: финальный образ (Python + Node runtime + приложение)
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Метаданные
LABEL maintainer="pdf-concept-parser"
LABEL description="PDF Concept Parser – FastAPI service"

# Переменные окружения Python
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    # Путь к npm global bin (куда liteparse установлен в stage 1)
    PATH="/usr/local/lib/node_modules/.bin:/usr/local/bin:$PATH"

# Системные зависимости:
#   - nodejs  — runtime для LiteParse CLI
#   - libvips  — требуется sharp (используется liteparse для скриншотов)
#   - curl    — healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
        nodejs \
        libvips42 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Копируем node_modules с установленным liteparse из stage 1
COPY --from=node-builder /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=node-builder /usr/local/bin/lit           /usr/local/bin/lit

# Рабочая директория
WORKDIR /app

# Устанавливаем Python-зависимости (отдельный слой — кэшируется)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код приложения
COPY api.py extractor.py llm_extractor.py excel_handler.py main.py ./
COPY static/ ./static/

# Директория для временных файлов заданий (монтируется как volume в compose)
RUN mkdir -p /app/jobs

# Открываем порт приложения
EXPOSE 8000

# Healthcheck: проверяем что сервер отвечает
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/ || exit 1

# Запуск
CMD ["python", "-u", "api.py"]
