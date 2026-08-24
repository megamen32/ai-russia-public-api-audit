#!/usr/bin/env python3
"""
AI Russia API auditor, v2.

This script audits the *current* SPA/API architecture of https://ai-russia.ru.
It deliberately separates:

- scenario records: potential/use-case descriptions from /api/scenarios;
- implementation cases: nested records from scenarios[].cases[] and /api/cases/<cuid>;
- taxonomy placements: industry x business function x business task;
- headline/matrix counts versus records actually enumerated.

The main safety rule is simple: the length of the returned `scenarios` array is a
page size, while the response field `total` is the endpoint's total count. Never
turn the first 20 returned records into "20 scenarios in the database".

Live example:
    python ai_russia_api_audit_v2.py --out ai_russia_api_dump --verbose

Offline check against a saved API-samples JSON:
    python ai_russia_api_audit_v2.py \
      --fixture a9b987a4-093c-4987-b915-53aa4e902c4a.json \
      --out fixture_report

The crawler first tries to discover a working pagination scheme. If pagination
is not exposed in the obvious form, it discovers the function/task filter names
and enumerates all 10 x 10 cells per industry. Every response is saved verbatim.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import json
import logging
import re
import sys
import time
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

import httpx


BASE_URL = "https://ai-russia.ru"
SCENARIOS_URL = f"{BASE_URL}/api/scenarios"
INDUSTRIES_URL = f"{BASE_URL}/api/industries"
MATRIX_URL = f"{BASE_URL}/api/industries/{{slug}}/matrix"
CASE_URL = f"{BASE_URL}/api/cases/{{case_id}}"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
    "ai-russia-api-audit/2.0"
)

ACTUAL_RESULT_RE = re.compile(
    r"\b(?:по итогам|в результате|достиг(?:нут|нута|нуто|нуты)|"
    r"составил(?:а|о|и)?|сократил(?:ся|ась|ось|ись|и)?|"
    r"увеличил(?:ся|ась|ось|ись|и)?|повысил(?:ся|ась|ось|ись|и)?|"
    r"получен(?:а|о|ы)?|экономия составила|эффект составил|"
    r"точность составила|обработано|сэкономлено)\b",
    re.IGNORECASE,
)
POTENTIAL_RE = re.compile(
    r"\b(?:может|могут|позволит|потенциальн\w*|ожидаем\w*|"
    r"способен|способна|способны|предполагается|возможно|до\s+\d)\b",
    re.IGNORECASE,
)
METRIC_RE = re.compile(
    r"(?<!\w)(?:\d{1,3}(?:[\s\u00a0]\d{3})*|\d+)(?:[.,]\d+)?\s*"
    r"(?:%|млн|млрд|тыс\.?|руб(?:лей|ля|\.)?|₽|сек(?:унд[ыау]?|\.)?|"
    r"мин(?:ут[ыау]?|\.)?|час(?:а|ов)?|дн(?:я|ей)?|раз(?:а)?|x|х|×)(?!\w)",
    re.IGNORECASE,
)


@dataclasses.dataclass(slots=True)
class Industry:
    id: str
    slug: str
    name: str
    order: int | None
    declared_scenario_count: int | None


@dataclasses.dataclass(slots=True)
class TaxonomyItem:
    id: str
    slug: str
    name: str
    order: int | None


@dataclasses.dataclass(slots=True)
class MatrixCell:
    industry_slug: str
    function_id: str
    function_slug: str
    function_name: str
    task_id: str
    task_slug: str
    task_name: str
    declared_count: int | None
    has_cases: bool | None


@dataclasses.dataclass(slots=True)
class CaseStub:
    id: str
    title: str
    company: str
    scenario_ids: set[str] = dataclasses.field(default_factory=set)
    industries: set[str] = dataclasses.field(default_factory=set)
    functions: set[str] = dataclasses.field(default_factory=set)
    tasks: set[str] = dataclasses.field(default_factory=set)


@dataclasses.dataclass(slots=True)
class Scenario:
    id: str
    title: str
    title_en: str
    business_impact: str
    implementation_speed: str
    technologies: tuple[str, ...]
    case_count: int
    exposed_case_ids: tuple[str, ...]
    industries: set[str] = dataclasses.field(default_factory=set)
    functions: set[str] = dataclasses.field(default_factory=set)
    tasks: set[str] = dataclasses.field(default_factory=set)
    source_urls: set[str] = dataclasses.field(default_factory=set)


@dataclasses.dataclass(slots=True)
class PaginationStrategy:
    name: str
    page_size: int
    first_index: int

    def params(self, index: int) -> dict[str, int]:
        if self.name == "limit_offset":
            return {"limit": self.page_size, "offset": index * self.page_size}
        if self.name == "limit_skip":
            return {"limit": self.page_size, "skip": index * self.page_size}
        if self.name == "take_skip":
            return {"take": self.page_size, "skip": index * self.page_size}
        if self.name == "page_limit":
            return {"page": index + self.first_index, "limit": self.page_size}
        if self.name == "page_pageSize":
            return {"page": index + self.first_index, "pageSize": self.page_size}
        if self.name == "page_perPage":
            return {"page": index + self.first_index, "perPage": self.page_size}
        if self.name == "page_only":
            return {"page": index + self.first_index}
        raise ValueError(f"Unknown pagination strategy: {self.name}")


@dataclasses.dataclass(slots=True)
class CellFilterStrategy:
    function_param: str
    function_value_kind: str  # slug | id
    task_param: str
    task_value_kind: str  # slug | id


class ApiClient:
    def __init__(
        self,
        *,
        out_dir: Path,
        timeout: float,
        delay: float,
        user_agent: str,
        verify: bool,
    ) -> None:
        self.out_dir = out_dir
        self.raw_dir = out_dir / "raw_api"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.delay = max(0.0, delay)
        self._last_request = 0.0
        self._counter = 0
        self.client = httpx.Client(
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            verify=verify,
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json,text/plain;q=0.9,*/*;q=0.5",
                "Accept-Language": "ru,en;q=0.7",
            },
        )
        self.endpoint_rows: list[dict[str, Any]] = []

    def close(self) -> None:
        self.client.close()

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        label: str = "api",
        allow_status: Iterable[int] = (200,),
    ) -> tuple[int, Any]:
        self._wait()
        response = self.client.get(url, params=dict(params or {}))
        self._last_request = time.monotonic()
        self._counter += 1
        final_url = str(response.url)
        content_type = response.headers.get("content-type", "")
        row = {
            "sequence": self._counter,
            "label": label,
            "url": final_url,
            "status": response.status_code,
            "content_type": content_type,
            "bytes": len(response.content),
        }
        self.endpoint_rows.append(row)
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("_")[:80] or "api"
        raw_path = self.raw_dir / f"{self._counter:05d}_{safe_label}.json"
        try:
            data = response.json()
            raw_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            data = {"_raw_text": response.text}
            raw_path.write_text(response.text, encoding="utf-8", errors="replace")
        row["raw_path"] = str(raw_path.relative_to(self.out_dir))
        if response.status_code not in set(allow_status):
            logging.debug("Unexpected status %s for %s", response.status_code, final_url)
        return response.status_code, data


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        keys: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
        fieldnames = keys
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_fixture(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Fixture must be a JSON object whose keys identify saved endpoints")
    return data


def parse_industries(data: Any) -> list[Industry]:
    rows = data.get("industries", []) if isinstance(data, Mapping) else []
    out: list[Industry] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        slug = normalize_space(row.get("slug"))
        if not slug:
            continue
        out.append(
            Industry(
                id=normalize_space(row.get("id")),
                slug=slug,
                name=normalize_space(row.get("nameRu") or row.get("name")),
                order=as_int(row.get("order")),
                declared_scenario_count=as_int(row.get("scenarioCount")),
            )
        )
    return sorted(out, key=lambda item: (item.order is None, item.order or 0, item.slug))


def parse_matrix(data: Any, industry_slug: str) -> tuple[list[TaxonomyItem], list[TaxonomyItem], list[MatrixCell]]:
    if not isinstance(data, Mapping):
        return [], [], []
    functions: list[TaxonomyItem] = []
    tasks: list[TaxonomyItem] = []
    for row in data.get("functions", []) or []:
        if not isinstance(row, Mapping):
            continue
        functions.append(
            TaxonomyItem(
                id=normalize_space(row.get("id")),
                slug=normalize_space(row.get("slug")),
                name=normalize_space(row.get("nameRu") or row.get("name")),
                order=as_int(row.get("order")),
            )
        )
    for row in data.get("tasks", []) or []:
        if not isinstance(row, Mapping):
            continue
        tasks.append(
            TaxonomyItem(
                id=normalize_space(row.get("id")),
                slug=normalize_space(row.get("slug")),
                name=normalize_space(row.get("nameRu") or row.get("name")),
                order=as_int(row.get("order")),
            )
        )
    by_function = {item.id: item for item in functions}
    by_task = {item.id: item for item in tasks}
    cells: list[MatrixCell] = []
    for row in data.get("cells", []) or []:
        if not isinstance(row, Mapping):
            continue
        fid = normalize_space(row.get("functionId"))
        tid = normalize_space(row.get("taskId"))
        function = by_function.get(fid, TaxonomyItem(fid, "", "", None))
        task = by_task.get(tid, TaxonomyItem(tid, "", "", None))
        cells.append(
            MatrixCell(
                industry_slug=industry_slug,
                function_id=fid,
                function_slug=function.slug,
                function_name=function.name,
                task_id=tid,
                task_slug=task.slug,
                task_name=task.name,
                declared_count=as_int(row.get("count")),
                has_cases=as_bool(row.get("hasCases")),
            )
        )
    return functions, tasks, cells


def scenario_page(data: Any) -> tuple[list[Mapping[str, Any]], int | None]:
    if not isinstance(data, Mapping):
        return [], None
    rows = data.get("scenarios", [])
    if not isinstance(rows, list):
        rows = []
    return [row for row in rows if isinstance(row, Mapping)], as_int(data.get("total"))


def scenario_ids(data: Any) -> list[str]:
    rows, _ = scenario_page(data)
    return [normalize_space(row.get("id")) for row in rows if normalize_space(row.get("id"))]


def merge_scenario_page(
    data: Any,
    *,
    source_url: str,
    scenarios: MutableMapping[str, Scenario],
    cases: MutableMapping[str, CaseStub],
    industry: str = "",
    function: str = "",
    task: str = "",
) -> tuple[int, int | None]:
    rows, total = scenario_page(data)
    for row in rows:
        sid = normalize_space(row.get("id"))
        if not sid:
            continue
        case_rows = row.get("cases", []) if isinstance(row.get("cases"), list) else []
        exposed_ids: list[str] = []
        for case_row in case_rows:
            if not isinstance(case_row, Mapping):
                continue
            cid = normalize_space(case_row.get("id"))
            if not cid:
                continue
            exposed_ids.append(cid)
            stub = cases.get(cid)
            if stub is None:
                stub = CaseStub(
                    id=cid,
                    title=normalize_space(case_row.get("titleRu") or case_row.get("title")),
                    company=normalize_space(case_row.get("company")),
                )
                cases[cid] = stub
            else:
                if not stub.title:
                    stub.title = normalize_space(case_row.get("titleRu") or case_row.get("title"))
                if not stub.company:
                    stub.company = normalize_space(case_row.get("company"))
            stub.scenario_ids.add(sid)
            if industry:
                stub.industries.add(industry)
            if function:
                stub.functions.add(function)
            if task:
                stub.tasks.add(task)

        technologies = row.get("technologies", [])
        if not isinstance(technologies, list):
            technologies = []
        parsed = scenarios.get(sid)
        if parsed is None:
            parsed = Scenario(
                id=sid,
                title=normalize_space(row.get("titleRu") or row.get("title")),
                title_en=normalize_space(row.get("titleEn")),
                business_impact=normalize_space(row.get("businessImpact")),
                implementation_speed=normalize_space(row.get("implementationSpeed")),
                technologies=tuple(normalize_space(x) for x in technologies if normalize_space(x)),
                case_count=as_int(row.get("caseCount")) or 0,
                exposed_case_ids=tuple(exposed_ids),
            )
            scenarios[sid] = parsed
        else:
            parsed.case_count = max(parsed.case_count, as_int(row.get("caseCount")) or 0)
            parsed.exposed_case_ids = tuple(sorted(set(parsed.exposed_case_ids) | set(exposed_ids)))
        parsed.source_urls.add(source_url)
        if industry:
            parsed.industries.add(industry)
        if function:
            parsed.functions.add(function)
        if task:
            parsed.tasks.add(task)
    return len(rows), total


def params_url(base: str, params: Mapping[str, Any]) -> str:
    return f"{base}?{urllib.parse.urlencode(params, doseq=True)}" if params else base


def detect_pagination(
    api: ApiClient,
    *,
    base_params: Mapping[str, Any],
    first_data: Any,
    preferred_size: int,
) -> PaginationStrategy | None:
    first_ids = scenario_ids(first_data)
    if not first_ids:
        return None
    first_len = len(first_ids)
    _, total = scenario_page(first_data)
    if total is not None and first_len >= total:
        return PaginationStrategy("page_only", max(first_len, 1), 1)

    # First see whether the endpoint honours a larger page size.
    page_size = max(preferred_size, first_len)
    size_variants = [
        ("limit", {"limit": page_size}),
        ("pageSize", {"pageSize": page_size}),
        ("perPage", {"perPage": page_size}),
        ("take", {"take": page_size}),
    ]
    honoured: dict[str, int] = {}
    for name, extra in size_variants:
        params = dict(base_params)
        params.update(extra)
        status, data = api.get_json(SCENARIOS_URL, params=params, label=f"probe_size_{name}")
        ids = scenario_ids(data) if status == 200 else []
        if len(ids) > first_len:
            honoured[name] = len(ids)
            page_size = len(ids)
            logging.info("Page-size parameter %s honoured with %d records", name, len(ids))
            break

    candidates: list[PaginationStrategy] = []
    if "limit" in honoured:
        candidates.extend(
            [
                PaginationStrategy("limit_offset", page_size, 0),
                PaginationStrategy("limit_skip", page_size, 0),
                PaginationStrategy("page_limit", page_size, 1),
                PaginationStrategy("page_limit", page_size, 0),
            ]
        )
    elif "take" in honoured:
        candidates.append(PaginationStrategy("take_skip", page_size, 0))
    elif "pageSize" in honoured:
        candidates.extend(
            [
                PaginationStrategy("page_pageSize", page_size, 1),
                PaginationStrategy("page_pageSize", page_size, 0),
            ]
        )
    elif "perPage" in honoured:
        candidates.extend(
            [
                PaginationStrategy("page_perPage", page_size, 1),
                PaginationStrategy("page_perPage", page_size, 0),
            ]
        )
    else:
        page_size = first_len
        candidates.extend(
            [
                PaginationStrategy("limit_offset", page_size, 0),
                PaginationStrategy("limit_skip", page_size, 0),
                PaginationStrategy("take_skip", page_size, 0),
                PaginationStrategy("page_limit", page_size, 1),
                PaginationStrategy("page_limit", page_size, 0),
                PaginationStrategy("page_pageSize", page_size, 1),
                PaginationStrategy("page_perPage", page_size, 1),
                PaginationStrategy("page_only", page_size, 1),
                PaginationStrategy("page_only", page_size, 0),
            ]
        )

    first_set = set(first_ids)
    for strategy in candidates:
        params = dict(base_params)
        params.update(strategy.params(1))
        status, data = api.get_json(
            SCENARIOS_URL,
            params=params,
            label=f"probe_pagination_{strategy.name}_{strategy.first_index}",
        )
        ids = scenario_ids(data) if status == 200 else []
        if ids and set(ids) != first_set and len(first_set - set(ids)) >= max(1, len(first_set) // 3):
            logging.info("Detected pagination: %s, page_size=%d, first_index=%d", strategy.name, strategy.page_size, strategy.first_index)
            return strategy
    return None


def paginate_scenarios(
    api: ApiClient,
    *,
    base_params: Mapping[str, Any],
    first_data: Any,
    strategy: PaginationStrategy,
    label_prefix: str,
    max_pages: int,
) -> list[tuple[Any, str]]:
    pages: list[tuple[Any, str]] = [(first_data, params_url(SCENARIOS_URL, base_params))]
    first_ids = scenario_ids(first_data)
    seen = set(first_ids)
    _, total = scenario_page(first_data)
    if total is not None and len(seen) >= total:
        return pages

    page_index = 1
    while page_index < max_pages:
        params = dict(base_params)
        params.update(strategy.params(page_index))
        status, data = api.get_json(
            SCENARIOS_URL,
            params=params,
            label=f"{label_prefix}_page_{page_index + 1:04d}",
        )
        url = params_url(SCENARIOS_URL, params)
        if status != 200:
            break
        ids = scenario_ids(data)
        if not ids:
            break
        new_ids = [sid for sid in ids if sid not in seen]
        pages.append((data, url))
        if not new_ids:
            logging.warning("Pagination stopped: page %d produced no new scenario IDs", page_index + 1)
            break
        seen.update(new_ids)
        if total is not None and len(seen) >= total:
            break
        page_index += 1
    return pages


def detect_cell_filter(
    api: ApiClient,
    *,
    industry: Industry,
    functions: Sequence[TaxonomyItem],
    tasks: Sequence[TaxonomyItem],
    cells: Sequence[MatrixCell],
) -> CellFilterStrategy | None:
    if not functions or not tasks or not cells:
        return None
    function = functions[0]
    task = tasks[0]
    target = next(
        (
            cell.declared_count
            for cell in cells
            if cell.function_id == function.id and cell.task_id == task.id and cell.declared_count is not None
        ),
        5,
    )
    function_params = [
        "function",
        "functionSlug",
        "businessFunction",
        "businessFunctionSlug",
        "business_function",
        "functionId",
        "businessFunctionId",
    ]
    task_params = [
        "task",
        "taskSlug",
        "businessTask",
        "businessTaskSlug",
        "business_task",
        "taskId",
        "businessTaskId",
    ]
    function_values = [("slug", function.slug), ("id", function.id)]
    task_values = [("slug", task.slug), ("id", task.id)]

    for fparam in function_params:
        for fkind, fvalue in function_values:
            if not fvalue:
                continue
            for tparam in task_params:
                for tkind, tvalue in task_values:
                    if not tvalue:
                        continue
                    params = {"industry": industry.slug, fparam: fvalue, tparam: tvalue, "limit": 50}
                    status, data = api.get_json(
                        SCENARIOS_URL,
                        params=params,
                        label=f"probe_cell_{fparam}_{fkind}_{tparam}_{tkind}",
                    )
                    if status != 200:
                        continue
                    rows, total = scenario_page(data)
                    if total is not None and 0 < total <= max(10, (target or 5) * 2) and len(rows) <= max(20, total):
                        if target is None or total == target:
                            logging.info(
                                "Detected cell filters: %s(%s), %s(%s), target=%s",
                                fparam,
                                fkind,
                                tparam,
                                tkind,
                                target,
                            )
                            return CellFilterStrategy(fparam, fkind, tparam, tkind)
    return None


def crawl_live(args: argparse.Namespace, out_dir: Path) -> dict[str, Any]:
    api = ApiClient(
        out_dir=out_dir,
        timeout=args.timeout,
        delay=args.delay,
        user_agent=args.user_agent,
        verify=not args.insecure,
    )
    scenarios: dict[str, Scenario] = {}
    cases: dict[str, CaseStub] = {}
    matrices: dict[str, tuple[list[TaxonomyItem], list[TaxonomyItem], list[MatrixCell]]] = {}
    industry_coverage: list[dict[str, Any]] = []
    warnings: list[str] = []
    pagination: PaginationStrategy | None = None
    cell_filter: CellFilterStrategy | None = None

    try:
        status, industries_data = api.get_json(INDUSTRIES_URL, label="industries")
        if status != 200:
            raise RuntimeError(f"/api/industries returned HTTP {status}")
        industries = parse_industries(industries_data)
        if not industries:
            raise RuntimeError("No industries parsed from /api/industries")

        all_cells: list[MatrixCell] = []
        for industry in industries:
            status, data = api.get_json(
                MATRIX_URL.format(slug=urllib.parse.quote(industry.slug)),
                label=f"matrix_{industry.slug}",
            )
            functions, tasks, cells = parse_matrix(data if status == 200 else {}, industry.slug)
            matrices[industry.slug] = (functions, tasks, cells)
            all_cells.extend(cells)

        first_industry = industries[0]
        status, first_data = api.get_json(
            SCENARIOS_URL,
            params={"industry": first_industry.slug},
            label=f"scenarios_{first_industry.slug}_first",
        )
        if status != 200:
            raise RuntimeError(f"/api/scenarios?industry={first_industry.slug} returned HTTP {status}")

        pagination = detect_pagination(
            api,
            base_params={"industry": first_industry.slug},
            first_data=first_data,
            preferred_size=args.page_size,
        )

        if pagination is not None:
            for industry in industries:
                base_params = {"industry": industry.slug}
                # Re-fetch page 1 using the detected strategy. This is essential when the
                # endpoint honoured a larger page size during probing; otherwise a default
                # 20-row first page followed by offset=200 would silently skip 180 records.
                first_params = dict(base_params)
                first_params.update(pagination.params(0))
                status, data = api.get_json(
                    SCENARIOS_URL,
                    params=first_params,
                    label=f"scenarios_{industry.slug}_first_paginated",
                )
                if status != 200:
                    warnings.append(f"Industry {industry.slug}: first scenario page HTTP {status}")
                    continue
                pages = paginate_scenarios(
                    api,
                    base_params=base_params,
                    first_data=data,
                    strategy=pagination,
                    label_prefix=f"scenarios_{industry.slug}",
                    max_pages=args.max_pages_per_industry,
                )
                before = len(scenarios)
                page_total: int | None = None
                returned = 0
                for page_data, source_url in pages:
                    count, total = merge_scenario_page(
                        page_data,
                        source_url=source_url,
                        scenarios=scenarios,
                        cases=cases,
                        industry=industry.slug,
                    )
                    returned += count
                    if total is not None:
                        page_total = total
                unique_for_industry = sum(1 for scenario in scenarios.values() if industry.slug in scenario.industries)
                complete = page_total is not None and unique_for_industry >= page_total
                industry_coverage.append(
                    {
                        "industry": industry.slug,
                        "declared_scenario_count": industry.declared_scenario_count,
                        "api_total": page_total,
                        "returned_rows_across_pages": returned,
                        "unique_scenarios": unique_for_industry,
                        "complete": complete,
                        "new_global_unique": len(scenarios) - before,
                    }
                )
                if not complete:
                    warnings.append(
                        f"Industry {industry.slug}: incomplete enumeration {unique_for_industry}/{page_total}"
                    )
        else:
            functions, tasks, cells = matrices[first_industry.slug]
            cell_filter = detect_cell_filter(
                api,
                industry=first_industry,
                functions=functions,
                tasks=tasks,
                cells=cells,
            )
            if cell_filter is None:
                raise RuntimeError(
                    "Could not discover pagination or function/task filter parameters. "
                    "Inspect raw_api/probe_*.json and rerun with a patched parameter map."
                )

            for industry in industries:
                functions, tasks, cells = matrices[industry.slug]
                f_by_id = {item.id: item for item in functions}
                t_by_id = {item.id: item for item in tasks}
                unique_before = len(scenarios)
                returned_total = 0
                cell_mismatches = 0
                for index, cell in enumerate(cells, 1):
                    function = f_by_id.get(cell.function_id)
                    task = t_by_id.get(cell.task_id)
                    if function is None or task is None:
                        cell_mismatches += 1
                        continue
                    fvalue = function.slug if cell_filter.function_value_kind == "slug" else function.id
                    tvalue = task.slug if cell_filter.task_value_kind == "slug" else task.id
                    params = {
                        "industry": industry.slug,
                        cell_filter.function_param: fvalue,
                        cell_filter.task_param: tvalue,
                        "limit": 50,
                    }
                    status, data = api.get_json(
                        SCENARIOS_URL,
                        params=params,
                        label=f"cell_{industry.slug}_{index:03d}",
                    )
                    if status != 200:
                        cell_mismatches += 1
                        continue
                    count, total = merge_scenario_page(
                        data,
                        source_url=params_url(SCENARIOS_URL, params),
                        scenarios=scenarios,
                        cases=cases,
                        industry=industry.slug,
                        function=function.slug,
                        task=task.slug,
                    )
                    returned_total += count
                    expected = cell.declared_count
                    if expected is not None and total is not None and total != expected:
                        cell_mismatches += 1
                unique_for_industry = sum(1 for scenario in scenarios.values() if industry.slug in scenario.industries)
                complete = (
                    industry.declared_scenario_count is not None
                    and unique_for_industry >= industry.declared_scenario_count
                    and cell_mismatches == 0
                )
                industry_coverage.append(
                    {
                        "industry": industry.slug,
                        "declared_scenario_count": industry.declared_scenario_count,
                        "api_total": industry.declared_scenario_count,
                        "returned_rows_across_cells": returned_total,
                        "unique_scenarios": unique_for_industry,
                        "complete": complete,
                        "cell_mismatches": cell_mismatches,
                        "new_global_unique": len(scenarios) - unique_before,
                    }
                )
                if not complete:
                    warnings.append(
                        f"Industry {industry.slug}: cell crawl incomplete or mismatched; "
                        f"unique={unique_for_industry}, declared={industry.declared_scenario_count}, mismatches={cell_mismatches}"
                    )

        case_details: dict[str, dict[str, Any]] = {}
        if not args.no_case_details:
            for index, case in enumerate(sorted(cases.values(), key=lambda item: item.id), 1):
                status, data = api.get_json(
                    CASE_URL.format(case_id=urllib.parse.quote(case.id)),
                    label=f"case_{index:04d}_{case.id}",
                    allow_status=(200, 401, 403, 404),
                )
                case_details[case.id] = {
                    "status": status,
                    "data": data,
                }

        write_csv(out_dir / "api_endpoints.csv", api.endpoint_rows)
        return build_outputs(
            out_dir=out_dir,
            industries=industries,
            matrices=matrices,
            scenarios=scenarios,
            cases=cases,
            case_details=case_details,
            industry_coverage=industry_coverage,
            warnings=warnings,
            pagination=pagination,
            cell_filter=cell_filter,
            mode="live",
        )
    finally:
        api.close()


def crawl_fixture(args: argparse.Namespace, out_dir: Path, fixture: Mapping[str, Any]) -> dict[str, Any]:
    industries_data = fixture.get("/api/industries", {})
    industries = parse_industries(industries_data)
    matrices: dict[str, tuple[list[TaxonomyItem], list[TaxonomyItem], list[MatrixCell]]] = {}
    for key, value in fixture.items():
        match = re.fullmatch(r"/api/industries/([^/]+)/matrix", key)
        if match:
            slug = match.group(1)
            matrices[slug] = parse_matrix(value, slug)

    scenarios: dict[str, Scenario] = {}
    cases: dict[str, CaseStub] = {}
    coverage: list[dict[str, Any]] = []
    warnings = [
        "Fixture mode only analyses the saved pages. It does not imply that observed case counts are site-wide totals."
    ]

    for key, value in fixture.items():
        if key.startswith("/api/scenarios (all"):
            count, total = merge_scenario_page(
                value,
                source_url=f"fixture:{key}",
                scenarios=scenarios,
                cases=cases,
            )
            coverage.append(
                {
                    "industry": "ALL",
                    "api_total": total,
                    "returned_rows": count,
                    "unique_scenarios_observed": len(scenarios),
                    "complete": total is not None and len(scenarios) >= total,
                }
            )
        elif key.startswith("/api/scenarios?industry="):
            industry = key.split("=", 1)[1]
            before_ids = {sid for sid, scenario in scenarios.items() if industry in scenario.industries}
            count, total = merge_scenario_page(
                value,
                source_url=f"fixture:{key}",
                scenarios=scenarios,
                cases=cases,
                industry=industry,
            )
            observed = sum(1 for scenario in scenarios.values() if industry in scenario.industries)
            coverage.append(
                {
                    "industry": industry,
                    "api_total": total,
                    "returned_rows": count,
                    "unique_scenarios_observed": observed,
                    "complete": total is not None and observed >= total,
                    "new_unique_for_industry": observed - len(before_ids),
                }
            )

    return build_outputs(
        out_dir=out_dir,
        industries=industries,
        matrices=matrices,
        scenarios=scenarios,
        cases=cases,
        case_details={},
        industry_coverage=coverage,
        warnings=warnings,
        pagination=None,
        cell_filter=None,
        mode="fixture",
    )


def flatten_json(obj: Any, prefix: str = "", depth: int = 0, max_depth: int = 5) -> dict[str, str]:
    out: dict[str, str] = {}
    if depth > max_depth:
        return out
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            out.update(flatten_json(value, child, depth + 1, max_depth))
    elif isinstance(obj, list):
        if all(not isinstance(item, (Mapping, list)) for item in obj):
            out[prefix] = " | ".join(normalize_space(item) for item in obj)
        else:
            for index, value in enumerate(obj[:50]):
                child = f"{prefix}[{index}]"
                out.update(flatten_json(value, child, depth + 1, max_depth))
    else:
        out[prefix] = normalize_space(obj)
    return out


def detail_text(data: Any) -> str:
    if not isinstance(data, Mapping):
        return normalize_space(data)
    return "\n".join(value for value in flatten_json(data).values() if value)


def build_outputs(
    *,
    out_dir: Path,
    industries: Sequence[Industry],
    matrices: Mapping[str, tuple[list[TaxonomyItem], list[TaxonomyItem], list[MatrixCell]]],
    scenarios: Mapping[str, Scenario],
    cases: Mapping[str, CaseStub],
    case_details: Mapping[str, Mapping[str, Any]],
    industry_coverage: Sequence[Mapping[str, Any]],
    warnings: Sequence[str],
    pagination: PaginationStrategy | None,
    cell_filter: CellFilterStrategy | None,
    mode: str,
) -> dict[str, Any]:
    industry_rows = [
        {
            "id": item.id,
            "slug": item.slug,
            "name": item.name,
            "order": item.order,
            "declared_scenario_count": item.declared_scenario_count,
        }
        for item in industries
    ]
    write_csv(out_dir / "industries.csv", industry_rows)

    matrix_rows: list[dict[str, Any]] = []
    for slug, (_, _, cells) in matrices.items():
        for cell in cells:
            matrix_rows.append(dataclasses.asdict(cell))
    write_csv(out_dir / "matrix_cells.csv", matrix_rows)

    scenario_rows: list[dict[str, Any]] = []
    for scenario in sorted(scenarios.values(), key=lambda item: (sorted(item.industries), item.title, item.id)):
        scenario_rows.append(
            {
                "scenario_id": scenario.id,
                "title": scenario.title,
                "title_en": scenario.title_en,
                "business_impact": scenario.business_impact,
                "implementation_speed": scenario.implementation_speed,
                "technologies": "; ".join(scenario.technologies),
                "case_count": scenario.case_count,
                "exposed_case_count": len(scenario.exposed_case_ids),
                "hidden_or_unexposed_case_count": max(0, scenario.case_count - len(scenario.exposed_case_ids)),
                "case_ids": "; ".join(scenario.exposed_case_ids),
                "industries": "; ".join(sorted(scenario.industries)),
                "functions": "; ".join(sorted(scenario.functions)),
                "tasks": "; ".join(sorted(scenario.tasks)),
                "source_urls": " | ".join(sorted(scenario.source_urls)),
            }
        )
    write_csv(out_dir / "all_scenarios.csv", scenario_rows)

    case_rows: list[dict[str, Any]] = []
    detail_status_counter: Counter[int] = Counter()
    for case in sorted(cases.values(), key=lambda item: (item.company, item.title, item.id)):
        detail = case_details.get(case.id, {})
        status = as_int(detail.get("status")) if isinstance(detail, Mapping) else None
        if status is not None:
            detail_status_counter[status] += 1
        data = detail.get("data", {}) if isinstance(detail, Mapping) else {}
        text = detail_text(data)
        case_rows.append(
            {
                "case_id": case.id,
                "title": case.title,
                "company": case.company,
                "scenario_ids": "; ".join(sorted(case.scenario_ids)),
                "industries": "; ".join(sorted(case.industries)),
                "functions": "; ".join(sorted(case.functions)),
                "tasks": "; ".join(sorted(case.tasks)),
                "detail_http_status": status or "",
                "detail_has_metric": bool(METRIC_RE.search(text)),
                "detail_has_actual_result_language": bool(ACTUAL_RESULT_RE.search(text)),
                "detail_potential_marker_count": len(POTENTIAL_RE.findall(text)),
                "detail_text_excerpt": normalize_space(text)[:800],
            }
        )
    write_csv(out_dir / "all_cases.csv", case_rows)

    with (out_dir / "case_details.jsonl").open("w", encoding="utf-8") as handle:
        for case_id, detail in case_details.items():
            handle.write(json.dumps({"case_id": case_id, **dict(detail)}, ensure_ascii=False) + "\n")

    write_csv(out_dir / "industry_coverage.csv", list(industry_coverage))

    matrix_count_distribution = Counter(cell.declared_count for row in matrices.values() for cell in row[2])
    matrix_has_cases_distribution = Counter(cell.has_cases for row in matrices.values() for cell in row[2])
    declared_total = sum(item.declared_scenario_count or 0 for item in industries)
    observed_case_links = sum(scenario.case_count for scenario in scenarios.values())
    exposed_case_links = sum(len(scenario.exposed_case_ids) for scenario in scenarios.values())
    unique_companies = sorted({case.company for case in cases.values() if case.company})
    complete = bool(industry_coverage) and all(bool(row.get("complete")) for row in industry_coverage if row.get("industry") != "ALL")

    summary = {
        "mode": mode,
        "generated_at_unix": int(time.time()),
        "industries": len(industries),
        "declared_scenario_total_from_industries": declared_total,
        "unique_scenarios_observed": len(scenarios),
        "enumeration_complete": complete,
        "observed_case_links_sum_caseCount": observed_case_links,
        "observed_exposed_case_links": exposed_case_links,
        "unique_exposed_case_ids": len(cases),
        "unique_exposed_companies": len(unique_companies),
        "companies": unique_companies,
        "matrix_cells_observed": sum(len(row[2]) for row in matrices.values()),
        "matrix_count_distribution": {str(k): v for k, v in sorted(matrix_count_distribution.items(), key=lambda x: str(x[0]))},
        "matrix_hasCases_distribution": {str(k): v for k, v in sorted(matrix_has_cases_distribution.items(), key=lambda x: str(x[0]))},
        "pagination_strategy": dataclasses.asdict(pagination) if pagination else None,
        "cell_filter_strategy": dataclasses.asdict(cell_filter) if cell_filter else None,
        "case_detail_statuses": {str(k): v for k, v in sorted(detail_status_counter.items())},
        "warnings": list(warnings),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    report_lines = [
        "# AI Russia API audit v2",
        "",
        f"- Режим: **{mode}**",
        f"- Отраслей: **{len(industries)}**",
        f"- Заявленная сумма scenarioCount: **{declared_total}**",
        f"- Уникальных сценариев фактически перечислено: **{len(scenarios)}**",
        f"- Полнота перечисления: **{'да' if complete else 'нет / не доказана'}**",
        f"- Уникальных открыто видимых case ID в собранных ответах: **{len(cases)}**",
        f"- Компаний в открыто видимых case stubs: **{len(unique_companies)}**",
        "",
        "## Критическое правило интерпретации",
        "",
        "`len(response.scenarios)` - число строк на возвращённой странице. "
        "`response.total` - полный счётчик запроса. Первую страницу из 20 строк нельзя объявлять всей базой.",
        "",
        "## Матрица",
        "",
        f"- Ячеек получено: **{summary['matrix_cells_observed']}**",
        f"- Распределение `count`: `{summary['matrix_count_distribution']}`",
        f"- Распределение `hasCases`: `{summary['matrix_hasCases_distribution']}`",
        "",
        "## Связанные кейсы",
        "",
        f"- Сумма наблюдаемых `caseCount` по перечисленным сценариям: **{observed_case_links}**",
        f"- Сумма открыто показанных элементов `cases[]`: **{exposed_case_links}**",
        f"- Уникальных открыто показанных case ID: **{len(cases)}**",
        "",
    ]
    if unique_companies:
        report_lines.extend(["Компании: " + ", ".join(unique_companies), ""])
    report_lines.extend(["## Покрытие по отраслям", ""])
    for row in industry_coverage:
        report_lines.append("- " + ", ".join(f"{key}={value}" for key, value in row.items()))
    if warnings:
        report_lines.extend(["", "## Предупреждения", ""])
        report_lines.extend(f"- {warning}" for warning in warnings)
    report_lines.extend(
        [
            "",
            "## Файлы",
            "",
            "- `all_scenarios.csv` - сценарии, их размещения и `caseCount`.",
            "- `all_cases.csv` - уникальные открыто видимые кейсы и результаты запроса `/api/cases/<id>`.",
            "- `matrix_cells.csv` - матрица отрасль × функция × задача.",
            "- `industry_coverage.csv` - доказательство полноты или неполноты обхода.",
            "- `raw_api/` - неизменённые ответы сервера.",
            "- `summary.json` - машинно-читаемая сводка.",
        ]
    )
    report = "\n".join(report_lines) + "\n"
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit current ai-russia.ru API without confusing a 20-row page with the total dataset.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--out", default="ai_russia_api_dump", help="Output directory")
    parser.add_argument("--fixture", help="Offline API-samples JSON instead of live requests")
    parser.add_argument("--page-size", type=int, default=200, help="Preferred page size during pagination probing")
    parser.add_argument("--max-pages-per-industry", type=int, default=200, help="Safety cap")
    parser.add_argument("--delay", type=float, default=0.15, help="Delay between live HTTP requests")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--no-case-details", action="store_true")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS verification")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        if args.fixture:
            summary = crawl_fixture(args, out_dir, load_fixture(Path(args.fixture)))
        else:
            summary = crawl_live(args, out_dir)
    except Exception as exc:
        logging.exception("Audit failed: %s", exc)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nReport: {out_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
