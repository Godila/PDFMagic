"""
api.py — FastAPI бэкенд для UI парсинга PDF → Excel.

Запуск:
  python api.py
  # или
  uvicorn api:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Optional

import aiofiles
from dotenv import load_dotenv
from fastapi import Body, FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


class ProcessRequest(BaseModel):
    custom_prompt: Optional[str] = None

# ---------------------------------------------------------------------------
# Инициализация
# ---------------------------------------------------------------------------
_script_dir = Path(__file__).parent
load_dotenv(_script_dir / ".env")

app = FastAPI(title="PDF Concept Parser", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Папка для временных файлов заданий
JOBS_DIR = _script_dir / "jobs"
JOBS_DIR.mkdir(exist_ok=True)

# Хранилище активных WebSocket соединений и очередей прогресса
_ws_queues: dict[str, asyncio.Queue] = {}

# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------

def _job_dir(job_id: str) -> Path:
    d = JOBS_DIR / job_id
    d.mkdir(exist_ok=True)
    return d


def _job_status(job_id: str) -> dict:
    f = JOBS_DIR / job_id / "status.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return {}


def _save_status(job_id: str, data: dict):
    f = JOBS_DIR / job_id / "status.json"
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


async def _push(job_id: str, msg: dict):
    """Отправляет сообщение в очередь WebSocket."""
    q = _ws_queues.get(job_id)
    if q:
        await q.put(msg)


# ---------------------------------------------------------------------------
# Эндпоинты
# ---------------------------------------------------------------------------

@app.post("/api/upload")
async def upload_files(
    pdf: UploadFile = File(...),
    xlsx: UploadFile = File(...),
):
    """Загружает PDF и XLSX, возвращает job_id."""
    job_id = str(uuid.uuid4())
    d = _job_dir(job_id)

    pdf_path = d / "input.pdf"
    xlsx_path = d / "template.xlsx"

    async with aiofiles.open(pdf_path, "wb") as f:
        await f.write(await pdf.read())
    async with aiofiles.open(xlsx_path, "wb") as f:
        await f.write(await xlsx.read())

    _save_status(job_id, {
        "state": "uploaded",
        "pdf_name": pdf.filename,
        "xlsx_name": xlsx.filename,
        "progress": 0,
        "log": [],
    })

    return {"job_id": job_id, "pdf_name": pdf.filename, "xlsx_name": xlsx.filename}


@app.get("/api/prompt")
async def get_prompt():
    """Получить текущий системный промпт."""
    from llm_extractor import SYSTEM_PROMPT
    return {"prompt": SYSTEM_PROMPT}


@app.post("/api/process/{job_id}")
async def start_process(job_id: str, req: ProcessRequest = Body(default=ProcessRequest())):
    """Запускает обработку задания в фоне."""
    status = _job_status(job_id)
    if not status:
        return JSONResponse({"error": "job not found"}, status_code=404)
    if status.get("state") in ("running", "done"):
        return JSONResponse({"error": "already running or done"}, status_code=400)

    # Сохраняем кастомный промпт если передан
    if req.custom_prompt:
        status["custom_prompt"] = req.custom_prompt
        _save_status(job_id, status)

    # Гарантируем существование очереди ДО запуска пайплайна
    if job_id not in _ws_queues:
        _ws_queues[job_id] = asyncio.Queue()

    # Запускаем фоновую задачу
    asyncio.create_task(_run_pipeline(job_id))
    return {"status": "started"}


@app.websocket("/api/ws/{job_id}")
async def websocket_progress(ws: WebSocket, job_id: str):
    """WebSocket для получения прогресса в реальном времени."""
    await ws.accept()

    # Создаём очередь если ещё нет
    if job_id not in _ws_queues:
        _ws_queues[job_id] = asyncio.Queue()

    q = _ws_queues[job_id]

    # Отправляем текущий статус сразу
    status = _job_status(job_id)
    if status:
        await ws.send_json({"type": "status", "data": status})

    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=30.0)
                await ws.send_json(msg)
                if msg.get("type") == "done" or msg.get("type") == "error":
                    break
            except asyncio.TimeoutError:
                # Ping чтобы держать соединение живым
                await ws.send_json({"type": "ping"})
    except WebSocketDisconnect:
        pass
    finally:
        _ws_queues.pop(job_id, None)


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    """Получить текущий статус задания (polling fallback)."""
    status = _job_status(job_id)
    if not status:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return status


@app.get("/api/download/{job_id}")
async def download_result(job_id: str):
    """Скачать готовый XLSX файл."""
    result_path = _job_dir(job_id) / "result.xlsx"
    if not result_path.exists():
        return JSONResponse({"error": "result not ready"}, status_code=404)
    status = _job_status(job_id)
    filename = f"result_{status.get('pdf_name', job_id)[:30]}.xlsx"
    return FileResponse(
        path=str(result_path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )


# ---------------------------------------------------------------------------
# Фоновый пайплайн
# ---------------------------------------------------------------------------

async def _run_pipeline(job_id: str):
    """Выполняет полный пайплайн парсинга в фоне с отправкой прогресса."""
    d = _job_dir(job_id)
    pdf_path = str(d / "input.pdf")
    xlsx_path = str(d / "template.xlsx")
    result_path = str(d / "result.xlsx")

    async def log(message: str, progress: int = None):
        status = _job_status(job_id)
        status.setdefault("log", []).append(message)
        if progress is not None:
            status["progress"] = progress
        _save_status(job_id, status)
        await _push(job_id, {"type": "log", "message": message, "progress": status.get("progress", 0)})

    try:
        status = _job_status(job_id)
        status["state"] = "running"
        status["started_at"] = time.time()
        _save_status(job_id, status)
        await _push(job_id, {"type": "status", "data": status})

        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        model = os.environ.get("OPENROUTER_MODEL", "google/gemini-3-flash-preview")

        # --- Шаг 1: Параметры из Excel ---
        await log("📋 Загружаем параметры из Excel...", progress=5)
        import sys
        sys.path.insert(0, str(_script_dir))
        from excel_handler import load_parameters
        parameters = load_parameters(xlsx_path)
        await log(f"✅ Загружено {len(parameters)} параметров", progress=10)

        # --- Шаг 2: Извлечение из PDF ---
        await log("🔍 Извлекаем данные из PDF (текст + скриншоты)...", progress=15)
        from extractor import extract_pdf
        pdf_data = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: extract_pdf(pdf_path, dpi=150, max_images=40)
        )
        await log(
            f"✅ PDF обработан: {pdf_data['page_count']} страниц, "
            f"{len(pdf_data['images_b64'])} скриншотов",
            progress=35
        )

        # --- Шаг 3: LLM анализ ---
        await log(f"🤖 Анализируем документ через AI ({model})...", progress=40)
        from llm_extractor import extract_parameters
        # Применяем кастомный промпт если задан
        custom_prompt = status.get("custom_prompt")
        if custom_prompt:
            import llm_extractor
            llm_extractor.SYSTEM_PROMPT = custom_prompt
        extracted = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: extract_parameters(
                pdf_data=pdf_data,
                parameters=parameters,
                api_key=api_key,
                model=model,
                batch_size=20,
            )
        )
        found = sum(1 for v in extracted.values() if v is not None)
        await log(
            f"✅ AI анализ завершён: найдено {found}/{len(parameters)} параметров",
            progress=85
        )

        # --- Шаг 4: Запись в Excel ---
        await log("💾 Записываем результаты в Excel...", progress=90)
        from excel_handler import write_results
        from pathlib import Path as _Path
        pdf_stem = _Path(status.get("pdf_name", "result")).stem
        output_path = write_results(
            xlsx_path=xlsx_path,
            parameters=parameters,
            extracted_values=extracted,
            column_header=pdf_stem,
            output_path=result_path,
        )
        await log("✅ Excel файл сформирован", progress=95)

        # --- Финал ---
        elapsed = time.time() - status.get("started_at", time.time())

        # Формируем таблицу результатов для UI
        results_table = []
        for p in parameters:
            results_table.append({
                "id": p.row_index,
                "number": p.number,
                "name": p.name,
                "type": p.param_type,
                "units": p.units,
                "value": extracted.get(p.row_index),
            })

        status = _job_status(job_id)
        status.update({
            "state": "done",
            "progress": 100,
            "elapsed": round(elapsed, 1),
            "found": found,
            "total": len(parameters),
            "results": results_table,
        })
        _save_status(job_id, status)

        await log(f"🎉 Готово! Найдено {found}/{len(parameters)} параметров за {elapsed:.0f} сек.", progress=100)
        await _push(job_id, {"type": "done", "data": status})

    except Exception as e:
        tb = traceback.format_exc()
        error_msg = f"❌ Ошибка: {str(e)}"
        status = _job_status(job_id)
        status.update({"state": "error", "error": str(e), "traceback": tb})
        _save_status(job_id, status)
        await _push(job_id, {"type": "error", "message": error_msg, "traceback": tb})


# ---------------------------------------------------------------------------
# Static files (фронтенд)
# ---------------------------------------------------------------------------
static_dir = _script_dir / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=False)
