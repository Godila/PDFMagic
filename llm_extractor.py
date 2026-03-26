"""
llm_extractor.py — извлечение параметров из PDF через OpenRouter (Vision LLM).

Использует openai-совместимый API OpenRouter.
Поддерживает Vision-модели: anthropic/claude-sonnet-4-6, openai/gpt-4o, etc.
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

from openai import OpenAI

from excel_handler import Parameter, parameters_to_prompt_text

# ---------------------------------------------------------------------------
# Системный промпт
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """Ты — эксперт по анализу концепций жилой застройки и девелоперских проектов.
Тебе предоставлен документ (PDF-презентация объекта недвижимости) в двух форматах:
1. Извлечённый текст
2. Скриншоты страниц (изображения)

Твоя задача — извлечь конкретные параметры проекта из документа.

СПИСОК ПАРАМЕТРОВ ДЛЯ ИЗВЛЕЧЕНИЯ:
{params_text}

ПРАВИЛА ИЗВЛЕЧЕНИЯ (читай внимательно, это критично):

1. ИСТОЧНИКИ ДАННЫХ
   - Анализируй ОДНОВРЕМЕННО текст и изображения — данные могут быть только на слайдах
   - Таблицы, инфографика и схемы на слайдах содержат ключевые числа

2. ТОЧНОСТЬ ЗНАЧЕНИЙ
   - Переписывай значения ДОСЛОВНО как указано в документе (с оригинальными единицами)
   - НЕ округляй, НЕ пересчитывай, НЕ конвертируй единицы (если в документе "10,28 га" — пиши "10,28 га", а не "102800 кв.м")
   - Числа с пробелом как разделителем тысяч сохраняй как есть (94 981 кв.м.)

3. НЕСКОЛЬКО ЗНАЧЕНИЙ (здания, очереди, пусковые комплексы)
   - Если параметр имеет несколько значений для разных зданий/очередей — перечисли ВСЕ через "\n"
   - Пример для "Площадь здания": "6918 кв.м\n2292 кв.м\n9210 кв.м" (не суммируй!)
   - Пример для "Подземный паркинг": "960 м/м\n235 м/м"

4. ПАРКИНГ (надземный и подземный)
   - Если в документе указано несколько групп мест — перечисли все через "\n"
   - НЕ суммируй разные типы парковок (открытые + гараж = отдельные строки)
   - Если явно указан ИТОГ — используй итог, иначе перечисляй все значения

5. ПЛОЩАДЬ ЗДАНИЯ
   - Извлекай площадь каждого отдельного корпуса/секции — НЕ суммарную GFA
   - Если несколько корпусов — перечисли через "\n": "4457 кв.м\n3200 кв.м"
   - Суммарная площадь всех зданий вместе — это НЕ площадь здания

6. ТИП ЗАСТРОЙКИ
   - Используй ТОЧНУЮ формулировку из документа (не синонимы)
   - Если написано "Многоэтажная жилая застройка" — пиши именно так, а не "Многоквартирная"

7. УЧРЕЖДЕНИЯ (ДОО, СОШ, медицина, спорт)
   - Для ДОО и СОШ — указывай значение в МЕСТАХ (количество мест/учащихся), не в кв.м
   - Если есть несколько значений (мест + площадь) — перечисляй через "\n"
   - Для медицины — койко-места или посещения/смену

8. ОТСУТСТВУЮЩИЕ ДАННЫЕ
   - Если параметр явно НЕ упомянут в документе — верни null
   - НЕ придумывай и НЕ вычисляй значения самостоятельно
   - Если упомянут косвенно, но точного значения нет — тоже null

ФОРМАТ ОТВЕТА:
Верни ТОЛЬКО валидный JSON вида:
{{"1": "значение или null", "2": "значение или null", ...}}
Ключи — порядковые номера параметров (строки).
Никакого дополнительного текста — только чистый JSON.
"""

# ---------------------------------------------------------------------------
# Основная функция
# ---------------------------------------------------------------------------

def extract_parameters(
    pdf_data: dict,
    parameters: list[Parameter],
    api_key: str,
    model: str = "anthropic/claude-sonnet-4-6",
    batch_size: int = 20,
    cancel_event=None,
) -> dict[int, str | None]:
    """
    Отправляет текст и изображения PDF в LLM через OpenRouter.
    Возвращает словарь {row_index: значение_или_None}.

    Args:
        pdf_data:   Результат extractor.extract_pdf()
        parameters: Список параметров из excel_handler.load_parameters()
        api_key:    OpenRouter API ключ
        model:      ID модели на OpenRouter
        batch_size: Максимальное кол-во изображений на один запрос
    """
    client = OpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        default_headers={
            "HTTP-Referer": "https://github.com/pdf-parser",
            "X-Title": "PDF Concept Parser",
        },
    )

    params_text = parameters_to_prompt_text(parameters)
    system_prompt = SYSTEM_PROMPT.format(params_text=params_text)

    images_b64: list[str] = pdf_data.get("images_b64", [])
    full_text: str = pdf_data.get("full_text", "")

    def _check_cancel():
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("Обработка отменена пользователем")

    # Если изображений больше batch_size — делаем несколько запросов и мержим
    _check_cancel()
    if len(images_b64) <= batch_size:
        result = _single_request(
            client=client,
            model=model,
            system_prompt=system_prompt,
            full_text=full_text,
            images_b64=images_b64,
            parameters=parameters,
            cancel_event=cancel_event,
        )
    else:
        result = _batched_request(
            client=client,
            model=model,
            system_prompt=system_prompt,
            full_text=full_text,
            images_b64=images_b64,
            parameters=parameters,
            batch_size=batch_size,
            cancel_event=cancel_event,
        )

    return result


def _single_request(
    client: OpenAI,
    model: str,
    system_prompt: str,
    full_text: str,
    images_b64: list[str],
    parameters: list[Parameter],
    cancel_event=None,
) -> dict[int, str | None]:
    """Один запрос к LLM со всем текстом и изображениями."""
    print(f"[llm_extractor] Отправляем запрос: модель={model}, "
          f"изображений={len(images_b64)}, символов текста={len(full_text)}")

    messages = _build_messages(system_prompt, full_text, images_b64)

    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0,
        max_tokens=16384,
    )

    raw = response.choices[0].message.content or ""
    reasoning = getattr(response.choices[0].message, "reasoning", None)
    reasoning_len = len(reasoning) if reasoning else 0
    finish = response.choices[0].finish_reason
    print(f"[llm_extractor] Получен ответ ({len(raw)} символов, "
          f"reasoning={reasoning_len} символов, finish={finish})")
    return _parse_json_response(raw, parameters)


def _batched_request(
    client: OpenAI,
    model: str,
    system_prompt: str,
    full_text: str,
    images_b64: list[str],
    parameters: list[Parameter],
    batch_size: int,
    cancel_event=None,
) -> dict[int, str | None]:
    """
    Разбивает изображения на батчи, делает несколько запросов.
    Результаты мержатся: более поздний запрос не перезаписывает уже найденное.
    """
    merged: dict[int, str | None] = {}
    batches = [
        images_b64[i : i + batch_size]
        for i in range(0, len(images_b64), batch_size)
    ]
    print(f"[llm_extractor] Батч-режим: {len(batches)} запросов по <={batch_size} изображений")

    for batch_num, batch_images in enumerate(batches, start=1):
        # Проверяем отмену перед каждым батчем
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("Обработка отменена пользователем")
        print(f"[llm_extractor] Батч {batch_num}/{len(batches)} ...")
        batch_result = _single_request(
            client=client,
            model=model,
            system_prompt=system_prompt,
            full_text=full_text if batch_num == 1 else "",  # текст только в первом батче
            images_b64=batch_images,
            parameters=parameters,
            cancel_event=cancel_event,
        )
        # Мерж: если значение ещё не найдено — берём из текущего батча
        for idx, value in batch_result.items():
            if idx not in merged or (merged[idx] is None and value is not None):
                merged[idx] = value

    return merged


def _build_messages(
    system_prompt: str,
    full_text: str,
    images_b64: list[str],
) -> list[dict[str, Any]]:
    """Формирует список сообщений в формате OpenAI Chat Completions."""
    content: list[dict[str, Any]] = []

    # Текст документа
    if full_text.strip():
        text_block = (
            "=== ИЗВЛЕЧЁННЫЙ ТЕКСТ ДОКУМЕНТА ===\n\n"
            + full_text[:15000]  # ограничиваем чтобы не превысить контекст
        )
        if len(full_text) > 15000:
            text_block += f"\n\n[... текст сокращён, всего {len(full_text)} символов ...]"
        content.append({"type": "text", "text": text_block})

    # Скриншоты страниц
    if images_b64:
        content.append({
            "type": "text",
            "text": f"=== СКРИНШОТЫ СТРАНИЦ ({len(images_b64)} шт.) ===",
        })
        for i, img_b64 in enumerate(images_b64, start=1):
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{img_b64}",
                    "detail": "high",
                },
            })

    content.append({
        "type": "text",
        "text": "Извлеки параметры из документа и верни JSON согласно инструкции.",
    })

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]


def _parse_json_response(
    raw: str,
    parameters: list[Parameter],
) -> dict[int, str | None]:
    """
    Парсит JSON-ответ LLM в словарь {row_index: value}.
    Устойчив к markdown-обёрткам и лишнему тексту.
    """
    # Убрать markdown code block если есть
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("```").strip()

    # Найти JSON-объект в тексте
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        print(f"[llm_extractor] WARN: JSON не найден в ответе:\n{raw[:500]}", file=sys.stderr)
        return {p.row_index: None for p in parameters}

    try:
        data = json.loads(match.group())
    except json.JSONDecodeError as e:
        print(f"[llm_extractor] WARN: Ошибка парсинга JSON: {e}\n{match.group()[:300]}", file=sys.stderr)
        return {p.row_index: None for p in parameters}

    result: dict[int, str | None] = {}
    for param in parameters:
        key_str = str(param.row_index)
        raw_value = data.get(key_str)
        if raw_value is None or str(raw_value).lower() in ("null", "none", "нет данных", ""):
            result[param.row_index] = None
        else:
            result[param.row_index] = str(raw_value).strip()

    found = sum(1 for v in result.values() if v is not None)
    print(f"[llm_extractor] Извлечено параметров: {found}/{len(parameters)}")
    return result
