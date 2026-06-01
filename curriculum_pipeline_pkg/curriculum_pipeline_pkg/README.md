# curriculum_pipeline — стадии 1→2 и 2→3

Реализация в стиле `content_gen`: pydantic-модели, слоистый LLM-клиент (OpenRouter),
конфиг-как-данные, стадии как модули. Подключается к реальному `skills_catalog.sqlite`.

## Модули
- `config.py` — флаги/пороги/slug'и моделей
- `models.py` — контракт: `IndicatorSpec, Evidence, SkillCandidate, PrereqEdge`
- `llm.py` — вызовы OpenRouter (mock через `USE_LIVE`)
- `catalog_repo.py` — чтение канона + резолв кандидата (matched/alias/fuzzy/new)
- `stage_brief_to_catalog.py` — стадия 1→2 (decompose→поиск→evidence→синтез→резолв→жюри→триаж)
- `stage_catalog_to_dag.py` — стадия 2→3 (рёбра→Блум-проверка→цикл-разрыв→транзитивная редукция)
- `storage.py` — миграция недостающих таблиц + запись результатов
- `run_pipeline.py` — сквозной прогон

## Запуск (без ключей, mock)
```
python -m curriculum_pipeline.run_pipeline src.sqlite work.sqlite sql/new_tables.sql
```
LIVE: `export USE_LIVE=1 OPENROUTER_API_KEY=...`

## Проверка каталога
```
python verify_catalog.py        # integrity_check, FK, наполнение, представления
```

## Что пишется (в рабочую копию, оригинал не трогается)
- новые таблицы: `profile_brief`, `evidence_source`, `skill_suggestion`, `skill_prerequisite`
- переиспользуется существующая `review_queue` (спорное -> status='open')
