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


def _screenshots_via_cli(pdf_path: str, dpi: int = 150) -> list[str]:
    """
    Генерирует скриншоты каждой страницы PDF через `lit screenshot` CLI.
    Возвращает список base64-строк (PNG), отсортированных по номеру страницы.
    """
    lit_cmd = _find_lit_command()
    out_dir = tempfile.mkdtemp(prefix="liteparse_screenshots_")
    try:
        result = subprocess.run(
            [lit_cmd, "screenshot", pdf_path, "-o", out_dir, "--dpi", str(dpi)],
            capture_output=True,
            text=True,
            shell=True,
        )
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


def _text_via_cli(pdf_path: str) -> tuple[str, list[str]]:
    """
    Извлекает текст через `lit parse` CLI (JSON-режим).
    Возвращает (полный_текст, [текст_страницы_1, ...]).
    """
    lit_cmd = _find_lit_command()
    result = subprocess.run(
        [lit_cmd, "parse", pdf_path, "--format", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        shell=True,
    )
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
    """Ищет исполняемый файл `lit` (liteparse CLI) в PATH."""
    # Явный путь npm global bin (Windows)
    npm_lit = os.path.expandvars(r"%APPDATA%\npm\lit.cmd")
    if os.path.exists(npm_lit):
        return npm_lit
    for candidate in ("lit", "lit.cmd", "lit.ps1"):
        if shutil.which(candidate):
            return candidate
    raise RuntimeError(
        "Команда `lit` не найдена. Установите liteparse:\n"
        "  npm install -g @llamaindex/liteparse"
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


def extract_pdf(pdf_path: str, dpi: int = 150, max_images: int = 40) -> dict:
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

    print(f"[extractor] Извлекаем текст из {Path(pdf_path).name} ...")
    full_text, pages_text = _text_via_cli(pdf_path)
    page_count = len(pages_text)
    print(f"[extractor] Получено {page_count} страниц текста.")

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
