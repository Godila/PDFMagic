# ─────────────────────────────────────────────────────────────────────────────
# Единый образ: Python 3.12 + Node.js 20 + LiteParse CLI
#
# Почему НЕ используем multi-stage copy Node-модулей:
#   lit использует import.meta.url для разрешения ../cli/parse.js,
#   поэтому нужна полная структура npm global prefix, а не только bin/lit.
#   Проще поставить Node.js через nodesource прямо в финальный образ.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Метаданные
LABEL maintainer="pdf-concept-parser"
LABEL description="PDF Concept Parser – FastAPI service"

# Переменные окружения Python
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

# Системные зависимости + Node.js 20 через nodesource:
#   - curl / gnupg  — для подключения nodesource репозитория
#   - nodejs        — runtime для LiteParse CLI (v20.x)
#   - libvips42     — требуется sharp (используется liteparse для скриншотов)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs libvips42 \
    && rm -rf /var/lib/apt/lists/* \
    # Устанавливаем liteparse глобально — npm сам создаёт правильную структуру
    && npm install -g @llamaindex/liteparse@1.3.0 --no-audit --no-fund \
    && lit --version

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
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/ || exit 1

# Запуск
CMD ["python", "-u", "api.py"]
