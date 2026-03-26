"""
main.py — точка входа CLI-сервиса парсинга PDF-концепций в Excel.

Использование:
  python main.py <pdf_file> <excel_template> [опции]

Примеры:
  python main.py "ГК__Концепция 6 (слайды 18) (1).pdf" "параметры_концепции_общий_файл_2 (1).xlsx"
  python main.py concept.pdf template.xlsx --model openai/gpt-4o --dpi 200
  python main.py concept.pdf template.xlsx --output result.xlsx --no-images
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
from pathlib import Path

# Fix Windows console Unicode encoding (UTF-8 instead of cp1251)
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Загрузка переменных окружения из .env (если файл рядом со скриптом)
# ---------------------------------------------------------------------------
_script_dir = Path(__file__).parent
load_dotenv(_script_dir / ".env")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Парсинг PDF-концепции застройки -> Excel",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "pdf_file",
        help="Путь к PDF файлу презентации",
    )
    parser.add_argument(
        "excel_template",
        help="Путь к Excel шаблону с параметрами",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Путь для сохранения результата (по умолчанию: рядом с шаблоном)",
    )
    parser.add_argument(
        "--model", "-m",
        default=None,
        help="Модель OpenRouter (по умолчанию из .env или anthropic/claude-sonnet-4-6)",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=150,
        help="DPI скриншотов (по умолчанию: 150)",
    )
    parser.add_argument(
        "--max-images",
        type=int,
        default=40,
        help="Максимальное количество изображений для LLM (по умолчанию: 40)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="Изображений на один LLM запрос (по умолчанию: 20)",
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="Отключить скриншоты (только текстовый режим, быстрее но менее точно)",
    )
    parser.add_argument(
        "--column-name",
        default=None,
        help="Имя нового столбца в Excel (по умолчанию: имя PDF файла без .pdf)",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # -------------------------------------------------------------------
    # Проверка API ключа
    # -------------------------------------------------------------------
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print(
            "ОШИБКА: OPENROUTER_API_KEY не установлен.\n"
            "Скопируйте .env.example -> .env и добавьте ключ.",
            file=sys.stderr,
        )
        sys.exit(1)

    model = args.model or os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4-6")

    # -------------------------------------------------------------------
    # Проверка файлов
    # -------------------------------------------------------------------
    pdf_path = Path(args.pdf_file).resolve()
    xlsx_path = Path(args.excel_template).resolve()

    if not pdf_path.exists():
        print(f"ОШИБКА: PDF не найден: {pdf_path}", file=sys.stderr)
        sys.exit(1)
    if not xlsx_path.exists():
        print(f"ОШИБКА: Excel файл не найден: {xlsx_path}", file=sys.stderr)
        sys.exit(1)

    column_name = args.column_name or pdf_path.stem
    print(f"\n{'='*60}")
    print(f"  PDF:     {pdf_path.name}")
    print(f"  Excel:   {xlsx_path.name}")
    print(f"  Модель:  {model}")
    print(f"  Столбец: {column_name}")
    print(f"{'='*60}\n")

    t_start = time.time()

    # -------------------------------------------------------------------
    # Шаг 1: Чтение параметров из Excel
    # -------------------------------------------------------------------
    print(">>> Шаг 1/3: Загрузка параметров из Excel ...")
    from excel_handler import load_parameters, write_results
    parameters = load_parameters(str(xlsx_path))
    if not parameters:
        print("ОШИБКА: Список параметров пуст. Проверьте лист 'Список параметров'.", file=sys.stderr)
        sys.exit(1)
    print(f"    Загружено {len(parameters)} параметров.\n")

    # -------------------------------------------------------------------
    # Шаг 2: Извлечение данных из PDF
    # -------------------------------------------------------------------
    print(">>> Шаг 2/3: Извлечение данных из PDF через LiteParse ...")
    from extractor import extract_pdf
    pdf_data = extract_pdf(
        str(pdf_path),
        dpi=args.dpi,
        max_images=0 if args.no_images else args.max_images,
    )
    print(
        f"    Страниц: {pdf_data['page_count']}, "
        f"Символов текста: {len(pdf_data['full_text'])}, "
        f"Скриншотов: {len(pdf_data['images_b64'])}\n"
    )

    if not pdf_data["full_text"].strip() and not pdf_data["images_b64"]:
        print("ПРЕДУПРЕЖДЕНИЕ: Из PDF не извлечено ни текста, ни изображений.", file=sys.stderr)

    # -------------------------------------------------------------------
    # Шаг 3: LLM — извлечение параметров
    # -------------------------------------------------------------------
    print(f">>> Шаг 3/3: Анализ документа через LLM ({model}) ...")
    from llm_extractor import extract_parameters
    extracted = extract_parameters(
        pdf_data=pdf_data,
        parameters=parameters,
        api_key=api_key,
        model=model,
        batch_size=args.batch_size,
    )

    # -------------------------------------------------------------------
    # Шаг 4: Запись результатов в Excel
    # -------------------------------------------------------------------
    print("\n>>> Запись результатов в Excel ...")
    output_path = write_results(
        xlsx_path=str(xlsx_path),
        parameters=parameters,
        extracted_values=extracted,
        column_header=column_name,
        output_path=args.output,
    )

    # -------------------------------------------------------------------
    # Итоговая сводка
    # -------------------------------------------------------------------
    found_count = sum(1 for v in extracted.values() if v is not None)
    total = len(parameters)
    elapsed = time.time() - t_start

    print(f"\n{'='*60}")
    print(f"  ГОТОВО за {elapsed:.1f} сек.")
    print(f"  Найдено параметров: {found_count}/{total} ({100*found_count//total}%)")
    print(f"  Результат сохранён: {output_path}")
    print(f"{'='*60}\n")

    # Вывести таблицу найденных/не найденных параметров
    _print_summary(parameters, extracted)


def _print_summary(parameters, extracted: dict[int, str | None]):
    """Выводит сводную таблицу результатов в консоль."""
    print("Сводка извлечённых параметров:")
    print(f"  {'№':<4} {'Параметр':<35} {'Значение'}")
    print("  " + "-"*70)
    for p in parameters:
        value = extracted.get(p.row_index)
        status = value if value else "— не найдено"
        name = (p.name[:33] + "..") if len(p.name) > 35 else p.name
        print(f"  {p.row_index:<4} {name:<35} {status}")


if __name__ == "__main__":
    main()
