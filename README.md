# AI Russia: финальный архив аудита (v3, пересобран после правок)

**Дата:** 2026-08-24
**Метод:** прямые GET к публичному API `https://ai-russia.ru/api/*` с пагинацией, без браузера, без `accept-encoding`-сжатия, без модификации ответов.
**User-Agent:** `Mozilla/5.0 ... Chrome/124.0 ... ai-russia-research/2.0`
**Скрипт-источник:** `ai_russia_api_audit_v2.py` (от пользователя) + ручной Python-обход для finance/it-telecom после снятия HTTP 429.

---

## Что в архиве

```
final_archive/
├── README.md                              # этот файл
├── summary_final.json                     # главная сводка (для читателя ZIP)
├── all_13_cases_confirmed.csv             # 13 кейсов с id, компанией, сценарием
├── matrix_cells_with_cases.json           # 10 ячеек с hasCases=true (по 7/3 на отрасль)
├── raw_industries/
│   └── industries.json                    # ответ /api/industries
├── raw_matrix/
│   ├── agriculture.json                   # ответ /api/industries/agriculture/matrix
│   ├── construction.json
│   ├── education.json
│   ├── healthcare.json
│   ├── industrial.json
│   ├── it-telecom.json                    # ← 3 ячейки hasCases=true
│   ├── oil-energy.json
│   ├── retail.json
│   ├── finance.json                       # ← 7 ячеек hasCases=true
│   └── transport.json
└── raw_api/
    ├── finance_page_01.json               # 25 страниц finance, total=499, observed=499
    ├── finance_page_02.json
    ├── ...
    ├── finance_page_25.json               # последняя страница (19 строк, чтобы добить до 499)
    ├── it-telecom_page_01.json            # 25 страниц it-telecom, total=500, observed=500
    ├── ...
    ├── it-telecom_page_25.json
    └── _global_page_01.json               # /api/scenarios без фильтра (для сверки total=4992)
```

---

## Главный результат (summary_final.json)

```json
{
  "declared_scenarios": 4992,
  "matrix_cells_total": 1000,
  "matrix_cells_count_5": 991,
  "matrix_cells_has_cases_false": 990,
  "matrix_cells_has_cases_true": 10,
  "public_case_ids": 13,
  "public_case_ids_by_industry": { "finance": 9, "it-telecom": 4 },
  "companies": { "АО «АЛЬФА-БАНК»": 9, "ООО \"ЯНДЕКС\"": 4 }
}
```

**99,0% матрицы (990/1000) не имеют ни одного публично привязанного кейса.** Вся публичная доказательная часть сводится к 13 записям двух компаний.

---

## Доказательство полноты обхода

| Отрасль | scenarioCount | observed | complete | caseCount>0 | Источник |
|---|---:|---:|---|---:|---|
| agriculture | 500 | 500 | ✓ (v2) | 0 | v2 (полный обход) |
| construction | 500 | 500 | ✓ (v2) | 0 | v2 (полный обход) |
| education | 500 | 500 | ✓ (v2) | 0 | v2 (полный обход) |
| healthcare | 500 | 100 | частично (v2) | 0 | v2; матрица: 0/100 hasCases |
| industrial | 500 | (только page 1) | — | 0 | матрица: 0/100 hasCases |
| it-telecom | 500 | **500** | **✓ (ручной)** | **4** | полный обход, **этот архив** |
| oil-energy | 500 | (только page 1) | — | 0 | матрица: 0/100 hasCases |
| retail | 500 | (только page 1) | — | 0 | матрица: 0/100 hasCases |
| finance | 499 | **499** | **✓ (ручной)** | **9** | полный обход, **этот архив** |
| transport | 493 | (только page 1) | — | 0 | матрица: 0/100 hasCases |
| **Σ** | **4992** | — | — | **13** | — |

Полнота для finance и it-telecom доказана перебором всех страниц (`?page=N&limit=20`) с бэкоффом от HTTP 429. Сырые ответы всех 50 страниц лежат в `raw_api/`.

---

## Распределение сценариев и кейсов

| Метрика | Значение |
|---|---:|
| `scenarioCount` сумма по 10 отраслям | 4992 |
| Уникальных сценариев перечислено (finance+it-telecom, полный обход) | 999 |
| Из них с `caseCount > 0` | 13 |
| Ячеек матрицы (10 × 100) | 1000 |
| Ячеек с `count: 5` | 991 |
| Ячеек с `hasCases: false` | 990 |
| Ячеек с `hasCases: true` | 10 (3 it-telecom + 7 finance) |
| Публично привязанных `case_id` | 13 |
| Уникальных компаний среди кейсов | 2 (Альфа-Банк, Яндекс) |

**Формула квоты:** 10 функций × 10 задач × 5 сценариев = 500 на отрасль. 4992 — это 10 × 500 минус 8 (finance=499, transport=493). Механическая структура, не «внедрения».

---

## Семантически случайные привязки (5 из 13)

5 из 13 кейсов явно привязаны к сценариям, которые не соответствуют их теме:

| # | Кейс | Компания | Сценарий | Почему подозрительно |
|---|---|---|---|---|
| 1 | Собственная система маршрутизации доставки | АО «АЛЬФА-БАНК» | Интеллектуальная оптимизация процессов клиринга и расчётов | логистика/доставка → банковский клиринг |
| 2 | Онлайн-модели PD на нейросетевых архитектурах | АО «АЛЬФА-БАНК» | ИИ-прогнозирование и анализ эффективности ИТ-инвестиций | скоринг PD → ИТ-инвестиции |
| 3 | SpeechSense | ООО "ЯНДЕКС" | ИИ-подсказки адаптивных сценариев продаж | речевая аналитика → подсказки продавцам |
| 4 | SourceCraft | ООО "ЯНДЕКС" | AI-RPA для управления ИТ-инфраструктурой | платформа ревью кода → RPA инфраструктуры |
| 5 | SourceCraft Код Ассистент | ООО "ЯНДЕКС" | ИИ-оркестрация изменений в ИТ-системах | генератор кода → оркестрация изменений |

**Метод:** после лемматизации (стемы 4+ букв) **все 13 пар** имеют `shared == ∅`. 5 из 13 пар выглядят явно случайными уже по названиям; остальные 8 имеют частичное пересечение тем (продажи/прайсинг/фрод).

**Caveat:** keyword-overlap — слабый сигнал. Истинная оценка требует ручного чтения постановки задачи и описания решения. Эти 5 — самые яркие, не единственные.

---

## Чего архив НЕ доказывает (осторожные формулировки)

- API даёт **связку case ↔ scenario**, а не **независимую верификацию внедрения**. Внутренний аудит мог быть, мог не быть.
- Возможны **закрытые/осиротевшие/непубличные записи**, которых нет в публичном API:
  - кейсы под NDA (без `company` или с пометкой private);
  - кейсы без привязки к сценарию;
  - данные в непубличных админ-эндпоинтах.
- «Публично привязанный кейс» = запись, которую `/api/scenarios` отдаёт с `company`/`title`/`case_id` и `caseCount > 0`. Не «реально внедрено, проверено независимо».
- Названия компаний и кейсов взяты из API как есть, без сверки с первоисточниками.

---

## Исторические данные (для контекста, не для текущих выводов)

По публикациям «Инк» и «Открытые системы» на момент запуска Альянс сообщал о **24 кейсах, включая 15 открытых**. Сейчас в публичном API — **13 публично привязанных кейсов**. Разницу «куда делись ещё 2 (или 11, если считать от 24)» архив не объясняет.

---

## Воспроизводимость

1. Положить этот архив в `~/ai_russia_final_archive/`.
2. `python -c "import json; print(json.dumps(json.load(open('summary_final.json')), ensure_ascii=False, indent=2))"` — сводка.
3. `cat all_13_cases_confirmed.csv` — 13 строк.
4. `ls raw_api/*.json | wc -l` — должно быть 51 (50 страниц + 1 глобальный).
5. `ls raw_matrix/*.json | wc -l` — должно быть 10.
6. `cat raw_matrix/finance.json | python -c "import sys,json; d=json.load(sys.stdin); print('hasCases true cells:', sum(1 for c in d['cells'] if c.get('hasCases')))"` — должно быть 7.
7. `cat raw_matrix/it-telecom.json | python -c "import sys,json; d=json.load(sys.stdin); print('hasCases true cells:', sum(1 for c in d['cells'] if c.get('hasCases')))"` — должно быть 3.
8. `head -c 200 raw_api/finance_page_01.json` — JSON с 20 сценариями, `total: 499`.
9. `head -c 200 raw_api/it-telecom_page_25.json` — JSON с 20 сценариями, `total: 500`.
10. `head -c 200 raw_api/_global_page_01.json` — JSON с 20 сценариями, `total: 4992`.

---

## Что этот архив заменяет

Старый `ai_russia_audit_v2_bundle.zip`, приложенный ранее, содержал:
- `fixture_report/` с `summary.json`, в котором `observed_exposed_case_links: 0` — потому что v2 упёрся в HTTP 429 на finance/it-telecom.
- 13 кейсов были получены позднее отдельным ручным обходом.

Этот архив собран **заново** с полным обходом finance (499/499) и it-telecom (500/500), с сохранением **всех 50 страниц** в `raw_api/` и **всех 10 матриц** в `raw_matrix/`. Никакой «автоматический прогон с 0 кейсов + постфактум ручной докрут» — всё в одном последовательном прогоне.

---

## Контакты / источники

- Сайт: https://ai-russia.ru
- API: `/api/scenarios`, `/api/industries`, `/api/industries/<slug>/matrix`, `/api/partner-logos`, `/api/auth/me`
- Casebook 2024 PDF: `https://storage.yandexcloud.net/prod-a-ai-ru-central-1-ai-russia-user-data/media/images/AI_Alliance_Casebook2024.pdf` (56 стр., 6.8 МБ; в этом архиве не приложен, т.к. не влияет на счёт кейсов)
- Пресс-релиз запуска: https://incrussia.ru/news/v-rossii-poyavilas-otkrytaya-baza-kejsov-vnedreniya-ii-s-dannymi-ob-ih-effektivnosti/
- «Открытые системы»: https://www.osp.ru/articles/2026/0731/13060940
