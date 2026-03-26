"""
extractor.py -- PDF -> текст + скриншоты страниц через LiteParse.

Требует установки:
  npm i -g @llamaindex/liteparse
  pip install liteparse
"""
import base64
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _kill_tree(pid: int):
    """Убивает процесс и всех его потомков (Windows-safe)."""
    try:
        import psutil
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        parent.kill()
    except Exception:
        pass


def _run_with_kill_tree(cmd: list, timeout: int, cancel_event=None,
                        **popen_kwargs) -> subprocess.CompletedProcess:
    """
    Запускает subprocess с жёстким таймаутом и убийством всего дерева процессов.

    На Windows shell=True создаёт cmd.exe → node.exe цепочку.
    Стандартный subprocess.run(timeout=) убивает только cmd.exe, node.exe
    продолжает висеть. psutil.kill() убивает всё дерево.

    Использует communicate() в отдельном потоке чтобы избежать deadlock при
    большом stdout (PIPE буфер переполняется при poll-подходе).
    """
    import time as _time
    import threading as _threading

    proc = subprocess.Popen(cmd, **popen_kwargs)
    result_holder: list = [None, None]  # [stdout, stderr]

    def _communicate():
        try:
            result_holder[0], result_holder[1] = proc.communicate()
        except Exception:
            pass

    comm_thread = _threading.Thread(target=_communicate, daemon=True)
    comm_thread.start()

    deadline = _time.monotonic() + timeout
    poll_interval = 0.3

    while comm_thread.is_alive():
        if cancel_event and cancel_event.is_set():
            _kill_tree(proc.pid)
            comm_thread.join(timeout=2)
            raise InterruptedError("Обработка отменена пользователем")

        if _time.monotonic() > deadline:
            _kill_tree(proc.pid)
            comm_thread.join(timeout=2)
            raise subprocess.TimeoutExpired(cmd, timeout)

        _time.sleep(poll_interval)

    return subprocess.CompletedProcess(
        cmd, proc.returncode, result_holder[0], result_holder[1]
    )


def _screenshots_via_cli(pdf_path: str, dpi: int = 150, timeout: int = 120,
                         cancel_event=None) -> list[str]:
    """
    Генерирует скриншоты каждой страницы PDF через `lit screenshot` CLI.
    Возвращает список base64-строк (PNG), отсортированных по номеру страницы.
    """
    lit_cmd = _find_lit_command()
    out_dir = tempfile.mkdtemp(prefix="liteparse_screenshots_")
    try:
        try:
            result = _run_with_kill_tree(
                [lit_cmd, "screenshot", pdf_path, "-o", out_dir, "--dpi", str(dpi)],
                timeout=timeout,
                cancel_event=cancel_event,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=True,
            )
        except (subprocess.TimeoutExpired, InterruptedError):
            print(
                f"[extractor] WARN: lit screenshot завис (>{timeout}с), возвращаем пустой список.",
                file=sys.stderr,
            )
            return []

        if result.returncode != 0:
            print(
                f"[extractor] WARN: lit screenshot завершился с кодом {result.returncode}",
                file=sys.stderr,
            )
            if result.stderr:
                print(f"[extractor] stderr: {result.stderr[:500]}", file=sys.stderr)

        images_b64 = []
        png_files = sorted(
            [f for f in os.listdir(out_dir) if f.lower().endswith(".png")],
            key=_page_sort_key,
        )
        for fname in png_files:
            fpath = os.path.join(out_dir, fname)
            images_b64.append(_load_and_resize(fpath))

        return images_b64
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def _text_via_cli(pdf_path: str, timeout: int = 60, cancel_event=None) -> tuple[str, list[str]]:
    """
    Извлекает текст через `lit parse` CLI (JSON-режим).
    Возвращает (полный_текст, [текст_страницы_1, ...]).
    При зависании (timeout) возвращает пустой текст — pipeline продолжит
    работу только на скриншотах.
    """
    lit_cmd = _find_lit_command()

    try:
        result = _run_with_kill_tree(
            [lit_cmd, "parse", pdf_path, "--format", "json"],
            timeout=timeout,
            cancel_event=cancel_event,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=True,
        )
        # Декодируем вручную (encoding нельзя передать в Popen напрямую с shell=True)
        result = subprocess.CompletedProcess(
            result.args, result.returncode,
            result.stdout.decode("utf-8", errors="replace") if result.stdout else "",
            result.stderr.decode("utf-8", errors="replace") if result.stderr else "",
        )
    except InterruptedError:
        raise  # пробрасываем отмену выше
    except subprocess.TimeoutExpired:
        print(
            f"[extractor] WARN: lit parse завис (>{timeout}с), пропускаем текст — "
            f"анализ продолжится только по скриншотам.",
            file=sys.stderr,
        )
        return "", []

    if result.returncode != 0:
        print(
            f"[extractor] WARN: lit parse завершился с кодом {result.returncode}",
            file=sys.stderr,
        )

    import json

    try:
        data = json.loads(result.stdout)
        full_text: str = data.get("text", "")
        pages_text: list[str] = [p.get("text", "") for p in data.get("pages", [])]
    except (json.JSONDecodeError, AttributeError):
        # Если JSON не распарсился — берём stdout как plain text
        full_text = result.stdout
        pages_text = [full_text]

    return full_text, pages_text


def _find_lit_command() -> str:
    """Ищет исполняемый файл `lit` (liteparse CLI) в PATH.

    Поддерживает Windows (lit.cmd) и Linux/Docker (/usr/local/bin/lit).
    Также учитывает переменную окружения LIT_CMD для явного указания пути.
    """
    # Явное переопределение через env (удобно для Docker/CI)
    env_override = os.environ.get("LIT_CMD")
    if env_override and os.path.exists(env_override):
        return env_override

    # Windows: npm global bin
    if os.name == "nt":
        npm_lit = os.path.expandvars(r"%APPDATA%\npm\lit.cmd")
        if os.path.exists(npm_lit):
            return npm_lit
        for candidate in ("lit.cmd", "lit.ps1", "lit"):
            if shutil.which(candidate):
                return candidate
    else:
        # Linux/macOS: стандартные места npm global bin
        for candidate in ("lit", "/usr/local/bin/lit", "/usr/bin/lit"):
            if shutil.which(candidate) or os.path.exists(candidate):
                return candidate

    raise RuntimeError(
        "Команда `lit` не найдена. Установите liteparse:\n"
        "  npm install -g @llamaindex/liteparse\n"
        "Или задайте путь явно через переменную окружения LIT_CMD."
    )


def _load_and_resize(fpath: str, max_width: int = 1024) -> str:
    """
    Загружает PNG, сжимает до max_width px (сохраняя пропорции),
    возвращает base64-строку JPEG (качество 85).
    Уменьшает размер файла примерно в 5-10x по сравнению с исходным PNG.
    """
    from PIL import Image

    with Image.open(fpath) as img:
        w, h = img.size
        if w > max_width:
            ratio = max_width / w
            img = img.resize((max_width, int(h * ratio)), Image.LANCZOS)
        # Конвертируем в RGB (убираем прозрачность если есть)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        return base64.b64encode(buf.getvalue()).decode("utf-8")


def _page_sort_key(filename: str) -> int:
    """Сортирует файлы по числу в имени (page-1.png, page-2.png, ...)."""
    import re

    nums = re.findall(r"\d+", filename)
    return int(nums[-1]) if nums else 0


def extract_pdf(pdf_path: str, dpi: int = 150, max_images: int = 40,
                cancel_event=None) -> dict:
    """
    Основная функция извлечения данных из PDF.

    Args:
        pdf_path:   Путь к PDF файлу.
        dpi:        DPI для скриншотов (150 — баланс качества и размера).
        max_images: Максимальное количество изображений для LLM
                    (чтобы не превысить лимиты контекста).

    Returns:
        {
          "full_text":   str,          # весь текст документа
          "pages_text":  list[str],    # текст каждой страницы
          "images_b64":  list[str],    # PNG каждой страницы в base64
          "page_count":  int,
        }
    """
    pdf_path = str(Path(pdf_path).resolve())
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF не найден: {pdf_path}")

    def _check_cancel():
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("Обработка отменена пользователем")

    print(f"[extractor] Извлекаем текст из {Path(pdf_path).name} ...")
    full_text, pages_text = _text_via_cli(pdf_path)
    page_count = len(pages_text)
    print(f"[extractor] Получено {page_count} страниц текста.")

    _check_cancel()  # точка отмены между text и screenshots

    print(f"[extractor] Генерируем скриншоты (DPI={dpi}) ...")
    images_b64 = _screenshots_via_cli(pdf_path, dpi=dpi)
    print(f"[extractor] Получено {len(images_b64)} скриншотов.")

    # Ограничиваем количество изображений для LLM
    if len(images_b64) > max_images:
        print(
            f"[extractor] Ограничиваем до {max_images} изображений "
            f"(из {len(images_b64)})."
        )
        # Равномерно выбираем изображения по всему документу
        step = len(images_b64) / max_images
        images_b64 = [images_b64[int(i * step)] for i in range(max_images)]

    return {
        "full_text": full_text,
        "pages_text": pages_text,
        "images_b64": images_b64,
        "page_count": page_count,
    }
