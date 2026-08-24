# AI Russia API audit v2

- Режим: **live**
- Отраслей: **10**
- Заявленная сумма scenarioCount: **4992**
- Уникальных сценариев фактически перечислено: **4992**
- Полнота перечисления: **да**
- Уникальных открыто видимых case ID в собранных ответах: **13**
- Компаний в открыто видимых case stubs: **2**

## Критическое правило интерпретации

`len(response.scenarios)` - число строк на возвращённой странице. `response.total` - полный счётчик запроса. Первую страницу из 20 строк нельзя объявлять всей базой.

## Матрица

- Ячеек получено: **1000**
- Распределение `count`: `{'3': 1, '4': 7, '5': 991, '6': 1}`
- Распределение `hasCases`: `{'False': 990, 'True': 10}`

## Связанные кейсы

- Сумма наблюдаемых `caseCount` по перечисленным сценариям: **13**
- Сумма открыто показанных элементов `cases[]`: **13**
- Уникальных открыто показанных case ID: **13**

Компании: АО «АЛЬФА-БАНК», ООО "ЯНДЕКС"

## Покрытие по отраслям

- industry=agriculture, declared_scenario_count=500, api_total=500, returned_rows_across_pages=500, unique_scenarios=500, complete=True, new_global_unique=500
- industry=construction, declared_scenario_count=500, api_total=500, returned_rows_across_pages=500, unique_scenarios=500, complete=True, new_global_unique=500
- industry=education, declared_scenario_count=500, api_total=500, returned_rows_across_pages=500, unique_scenarios=500, complete=True, new_global_unique=500
- industry=healthcare, declared_scenario_count=500, api_total=500, returned_rows_across_pages=500, unique_scenarios=500, complete=True, new_global_unique=500
- industry=industrial, declared_scenario_count=500, api_total=500, returned_rows_across_pages=500, unique_scenarios=500, complete=True, new_global_unique=500
- industry=it-telecom, declared_scenario_count=500, api_total=500, returned_rows_across_pages=500, unique_scenarios=500, complete=True, new_global_unique=500
- industry=oil-energy, declared_scenario_count=500, api_total=500, returned_rows_across_pages=500, unique_scenarios=500, complete=True, new_global_unique=500
- industry=retail, declared_scenario_count=500, api_total=500, returned_rows_across_pages=500, unique_scenarios=500, complete=True, new_global_unique=500
- industry=finance, declared_scenario_count=499, api_total=499, returned_rows_across_pages=499, unique_scenarios=499, complete=True, new_global_unique=499
- industry=transport, declared_scenario_count=493, api_total=493, returned_rows_across_pages=493, unique_scenarios=493, complete=True, new_global_unique=493

## Файлы

- `all_scenarios.csv` - сценарии, их размещения и `caseCount`.
- `all_cases.csv` - уникальные открыто видимые кейсы и результаты запроса `/api/cases/<id>`.
- `matrix_cells.csv` - матрица отрасль × функция × задача.
- `industry_coverage.csv` - доказательство полноты или неполноты обхода.
- `raw_api/` - неизменённые ответы сервера.
- `summary.json` - машинно-читаемая сводка.
