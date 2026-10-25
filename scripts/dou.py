"""Collect vacancies from jobs.dou.ua — one platform per run.

    python dou.py --years 4 --category "Node.js" --category "Front End" [--remote]
                  [--search TypeScript] [--city Київ] [--out <session folder>] [--lang ru|uk|en]

Per category: listings for every experience bucket overlapping N-2 … N+1
(+ the site's own "load more" endpoint), then one listing without the
experience filter to catch vacancies that don't state experience. Every kept
vacancy's own page gives the full description (cached). Public pages only, no
login. Exit 2 = refused or stopped early (partial results still written).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # find common.py from any cwd
from common import (Cache, PoliteClient, Stop, clean, exp_range, html_to_text, inner_html,
                    parse_salary, parse_ua_date, record, run_collector)

BASE = "https://jobs.dou.ua/vacancies/"
BUCKETS = {"0-1": (0, 1), "1-3": (1, 3), "3-5": (3, 5), "5plus": (5, 99)}
CATEGORIES = (
    ".NET", "AI/ML", "Analyst", "Android", "Architect", "Big Data", "Blockchain", "C++",
    "Data Engineer", "Data Science", "DBA", "DevOps", "Embedded", "Engineering Manager",
    "Erlang", "ERP/CRM", "Flutter", "Front End", "Golang", "iOS/macOS", "Java", "No-code",
    "Node.js", "Other", "PHP", "Python", "QA", "React Native", "Ruby", "Rust", "Salesforce",
    "SAP", "Scala", "Security", "SysAdmin", "Unity", "Unreal Engine",
)


def buckets_for(years: float, cap: float | None = None) -> list[str]:
    """Buckets touching the window (inclusive: 5 years → '3-5' and '5plus')."""
    lo, hi = exp_range(years, cap)
    return [b for b, (b_lo, b_hi) in BUCKETS.items() if b_lo <= hi and b_hi >= lo]


def query_string(params: dict, remote: bool) -> str:
    # DOU's remote filter is a bare `remote` key (`?remote&category=…`).
    return ("remote&" if remote else "") + urllib.parse.urlencode(params)


def parse_items(fragment: str) -> list[dict]:
    items = []
    for block in fragment.split('<li class="l-vacancy')[1:]:
        link = re.search(r'<a class="vt" href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        url = link.group(1).split("?")[0]
        vid = re.search(r"/vacancies/(\d+)/", url)
        company = re.search(r'class="company"[^>]*>(.*?)</a>', block, re.S)
        items.append({
            "source_id": vid.group(1) if vid else url,
            "url": url,
            "title": clean(link.group(2)),
            "company": clean(company.group(1)) if company else None,
            "company_url": (re.search(r'class="company" href="([^"]+)"', block) or [None, None])[1],
            "salary_text": clean((re.search(r'<span class="salary">(.*?)</span>', block, re.S) or [None, None])[1]) or None,
            "location": clean((re.search(r'<span class="cities[^"]*">(.*?)</span>', block, re.S) or [None, None])[1]) or None,
            "posted_at": parse_ua_date(clean((re.search(r'<div class="date">(.*?)</div>', block, re.S) or [None, ""])[1])),
            "summary": clean((re.search(r'<div class="sh-info">(.*?)</div>', block, re.S) or [None, None])[1]),
        })
    return items


def list_vacancies(client: PoliteClient, params: dict, remote: bool, max_pages: int, log) -> list[dict]:
    qs = query_string(params, remote)
    url = f"{BASE}?{qs}"
    page = client.request(url)
    found = parse_items(page)
    m = re.search(r"<h1>\s*(\d+)\s", page)
    total = int(m.group(1)) if m else 0
    if not m and "Немає вакансій" not in page and "No vacancies" not in page:
        raise Stop(f"DOU listing markup changed (no vacancy counter) on {url}")
    if total and not found:
        raise Stop(f"DOU shows {total} vacancies but none were parsed — markup changed? {url}")
    log(f"listing {params}{' remote' if remote else ''}: {total} on site, page 1 → {len(found)}")
    csrf = re.search(r"CSRF_TOKEN\s*=\s*['\"]([^'\"]+)", page)
    pages = 1
    while len(found) < total and pages < max_pages and csrf:
        chunk = json.loads(client.request(
            f"{BASE}xhr-load/?{qs}",
            data={"csrfmiddlewaretoken": csrf.group(1), "count": len(found)},
            headers={"Referer": url, "X-Requested-With": "XMLHttpRequest"},
        ))
        more = parse_items(chunk.get("html", ""))
        if not more:
            break
        found += more
        pages += 1
        if chunk.get("last"):
            break
    if len(found) < total:
        log.warn(f"{params}: collected {len(found)} of {total} on site"
                 + (f" (max-pages {max_pages} reached)" if pages >= max_pages else ""))
    return found


def fetch_details(client: PoliteClient, cache: Cache, rec: dict, log) -> dict:
    cached = cache.get(rec["source_id"])
    if cached:
        return cached
    try:
        page = client.request(rec["url"])
    except urllib.error.HTTPError as e:
        log(f"detail {rec['url']} → HTTP {e.code}, skipped")
        return {}
    body = inner_html(page, "div", "vacancy-section")
    if body is None:
        log.warn(f"no description block on {rec['url']} (markup changed?)")
        return {}
    info = inner_html(page, "div", "sh-info") or ""
    detail = {
        "description": html_to_text(body),
        "location": clean((re.search(r'<span class="place[^"]*">(.*?)</span>', info, re.S) or [None, None])[1]) or None,
        "salary_text": clean((re.search(r'<span class="salary">(.*?)</span>', info, re.S) or [None, None])[1]) or None,
        "posted_at": parse_ua_date(clean((re.search(r'<div class="date">(.*?)</div>', page, re.S) or [None, ""])[1])),
    }
    cache.put(rec["source_id"], detail)
    return detail


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--years", type=float, required=True, help="commercial experience N; searched N-2 … N+1")
    ap.add_argument("--max-exp", type=float, help="upper end of the experience window in years (default N+1)")
    ap.add_argument("--category", action="append", required=True, choices=CATEGORIES, help="DOU category, repeatable")
    ap.add_argument("--remote", action="store_true", help="remote vacancies only (DOU 'віддалено' filter)")
    ap.add_argument("--search", help="extra keyword filter (DOU 'search' field)")
    ap.add_argument("--city", help="DOU city filter, e.g. Київ")
    ap.add_argument("--no-unspecified", action="store_true",
                    help="skip the extra pass for vacancies that don't state experience")
    ap.add_argument("--out", help="session folder (default <JOB_SEARCH_DIR>/<date>-dou)")
    ap.add_argument("--lang", default="ru", choices=("ru", "uk", "en"), help="language of the .md report")
    ap.add_argument("--max-pages", type=int, default=10, help="pages of 20 per listing (default 10)")
    ap.add_argument("--max-details", type=int, default=200, help="max vacancy pages to open (default 200)")
    ap.add_argument("--cache-days", type=float, default=7, help="reuse fetched vacancy pages this long (default 7)")
    ap.add_argument("--ignore-gap", action="store_true", help="skip the pause-between-runs check (only on the user's request)")
    args = ap.parse_args()

    buckets = buckets_for(args.years, args.max_exp)
    query = {"years": args.years, "experience_range": exp_range(args.years, args.max_exp), "dou_exp": buckets,
             "categories": args.category, "remote": args.remote, "search": args.search, "city": args.city,
             "unspecified_pass": not args.no_unspecified}

    def collect(budget, log, records):
        client, cache = PoliteClient("dou", budget, log), Cache("dou", args.cache_days)
        base = {k: v for k, v in (("search", args.search), ("city", args.city)) if v}

        def add(it, category, label):
            rec = records.get(it["source_id"])
            if rec:
                rec["notes"] = f"{rec['notes']}; also {category} {label}"
                return
            lo, hi, cur = parse_salary(it["salary_text"])
            loc = it["location"] or ""
            records[it["source_id"]] = record(
                platform="dou", source_id=it["source_id"], url=it["url"], title=it["title"],
                company=it["company"], company_url=it["company_url"], location=it["location"],
                format="remote" if (args.remote or "віддалено" in loc) else None,
                salary_min=lo, salary_max=hi, currency=cur, salary_text=it["salary_text"],
                experience_text=f"DOU: {label}", category=category, posted_at=it["posted_at"],
                description=it["summary"], description_complete=False, notes=f"listed under {category} {label}",
            )

        for category in args.category:
            seen_in_buckets: set[str] = set()
            for bucket in BUCKETS:  # every bucket, so the no-filter pass can tell "unspecified" apart
                items = list_vacancies(client, {"category": category, "exp": bucket, **base},
                                       args.remote, args.max_pages, log) if (bucket in buckets or not args.no_unspecified) else []
                for it in items:
                    seen_in_buckets.add(it["source_id"])
                    if bucket in buckets:
                        add(it, category, f"{bucket} р.")
            if not args.no_unspecified:
                for it in list_vacancies(client, {"category": category, **base}, args.remote, args.max_pages, log):
                    if it["source_id"] not in seen_in_buckets:
                        add(it, category, "опыт не указан")
        log(f"listed {len(records)} unique vacancies")
        for n, rec in enumerate(records.values()):
            if n >= args.max_details:
                log.warn(f"max-details {args.max_details} reached; {len(records) - n} keep the listing summary")
                break
            d = fetch_details(client, cache, rec, log)
            if d.get("description"):
                rec["description"], rec["description_complete"] = d["description"], True
            for key in ("location", "salary_text", "posted_at"):
                if d.get(key):
                    rec[key] = d[key]
            if d.get("salary_text"):
                rec["salary_min"], rec["salary_max"], rec["currency"] = parse_salary(d["salary_text"])
            if rec["location"] and "віддалено" in rec["location"]:
                rec["format"] = "remote"
        return client

    return run_collector("dou", args, query, collect)


if __name__ == "__main__":
    sys.exit(main())
