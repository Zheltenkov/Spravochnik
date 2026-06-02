# Интеграция intake в текущий viewer

## Живые точки входа
```text
Spravochnik/
  spravochnik_intake/
    pipeline/                 # decompose/search/synthesize/atomize/resolve/council/triage
    sql/new_tables.sql        # единственный источник миграций intake/DAG
  viewer/
    app.py                    # WSGI viewer, роуты /intake, /reviews, /intake/jobs/<id>/build-dag
    templates/intake.html     # живая страница intake
    templates/reviews.html    # живая страница review + кнопка сборки DAG
```

## Как это работает сейчас
1. `POST /intake` создаёт `intake_job` и запускает intake в фоне.
2. Intake сохраняет `profile_brief`, `evidence_source`, `skill_suggestion` и записи в `review_queue`.
3. В intake DAG больше не строится. В `result_payload.dag` сохраняется deferred-state.
4. Методолог подтверждает/отклоняет `review_queue`.
5. Отдельный шаг `POST /reviews/build-dag` или `POST /intake/jobs/<id>/build-dag` строит DAG только по:
   - `entity_type = skill`
   - `atomicity = atomic`
   - `decision = accepted`

## Ключевой инвариант
`viewer/app.py` читает миграции только из:
`spravochnik_intake/sql/new_tables.sql`

Корневой `new_tables.sql` удалён специально, чтобы не было расхождения между кодом и SQL.

## Зависимости
`pydantic`, `rapidfuzz`, `networkx`, `requests`, `python-docx`
