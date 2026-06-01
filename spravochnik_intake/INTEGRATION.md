# Интеграция интейка брифа в Spravochnik

## Раскладка
```
Spravochnik/
  pipeline/                      # пакет стадий 1->2 и 2->3
  sql/new_tables.sql             # недостающие таблицы (рядом с catalog_schema.sql)
  viewer/
    app.py                       # + регистрация blueprint (ниже)
    intake_routes.py             # роут /intake (этот файл)
    templates/intake.html        # страница интейка
```

## Подключение в viewer/app.py
```python
from viewer.intake_routes import intake_bp, init_intake

init_intake(str(DB_PATH), str(BASE_DIR / "sql" / "new_tables.sql"))
app.register_blueprint(intake_bp)
```
Добавь пункт навигации `{"href": "/intake", "label": "Бриф"}` в свой NAV (или используй
NAV из intake_routes как образец).

## Схема
`new_tables.sql` идемпотентен (`CREATE TABLE IF NOT EXISTS`). Можно либо вызвать его
из существующего `ensure_runtime_schema`, либо он применяется автоматически при первом
POST /intake (`storage.apply_migration`). Существующая схема не меняется; `review_queue`
переиспользуется (спорное -> status='open').

## Зависимости
`pydantic, rapidfuzz, networkx, requests` (+ `python-docx` для загрузки .docx).

## Поток
GET /intake — форма (текст + опц. файл .txt/.md/.docx).
POST /intake — сохраняет бриф в `profile_brief` -> стадия 1->2 (decompose, поиск,
evidence, синтез, резолв против каталога, жюри, триаж) -> стадия 2->3 (prereq-DAG) ->
рендерит ВСЮ информацию для проверки (декомпозиция, кандидаты с резолвом/уверенностью/
решением, граф) и пишет результаты; спорное -> /reviews.

## Режим
MOCK по умолчанию (без ключей). LIVE: `export USE_LIVE=1 OPENROUTER_API_KEY=...`.

## Важно про производительность
В LIVE поиск+синтез+жюри идут секунды-минуты. Для боевого использования вынеси прогон
в фоновую задачу: POST создаёт бриф со статусом "processing" и запускает воркер, а
страница показывает результат по готовности. Текущая реализация синхронная (ок для MOCK
и первой проверки).
