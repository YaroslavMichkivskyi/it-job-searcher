"""Collect vacancies from djinni.co — one platform per run.

    python djinni.py --years 4 --keyword JavaScript --keyword Node.js [--remote]
                     [--english upper] [--region eu] [--out <session folder>] [--lang ru|uk|en]

Djinni listing pages carry every vacancy as structured data (schema.org
JobPosting) with the full description, so no per-vacancy requests are needed:
~15 vacancies per request. Public pages only, no login. Exit 2 = refused or
stopped early (partial results still written).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # find common.py from any cwd
from common import PoliteClient, Stop, clean, exp_range, record, run_collector

BASE = "https://djinni.co/jobs/"
PAGE_SIZE = 15
ENGLISH = ("no_english", "basic", "pre", "intermediate", "upper", "fluent", "proficient", "native")
REGIONS = ("UKR", "eu", "worldwide", "other")


def exp_levels(years: float, cap: float | None = None) -> list[str]:
    lo, hi = exp_range(years, cap)
    levels = []
    for y in range(int(lo), int(hi) + 1):
        lvl = "no_exp" if y == 0 else f"{min(y, 10)}y"
        if lvl not in levels:
            levels.append(lvl)
    return levels


def _obj(value, key: str = "name") -> dict:
    """schema.org fields may be an object, a bare string or missing."""
    if isinstance(value, dict):
        return value
    return {key: value} if isinstance(value, (str, int, float)) else {}


def parse_page(page: str, log=print) -> list[dict]:
    postings = []
    for block in re.findall(r'<script type="application/ld\+json">\s*(.*?)</script>', page, re.S):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        postings += [d for d in (data if isinstance(data, list) else [data])
                     if isinstance(d, dict) and d.get("@type") == "JobPosting"]
    cards = len(set(re.findall(r'id="job-item-(\d+)"', page)))
    if cards and not postings:
        raise Stop(f"Djinni page has {cards} job cards but no structured data — markup changed?")
    out = []
    for p in postings:
        try:
            out.append(_posting(page, p))
        except Exception as e:  # one odd posting must not sink the page
            log(f"skipped posting {p.get('identifier')}: {type(e).__name__}: {e}")
    if cards and len(out) < cards and hasattr(log, "warn"):
        log.warn(f"page has {cards} job cards, parsed {len(out)}")
    return out


def _posting(page: str, p: dict) -> dict:
    jid = str(p.get("identifier"))
    block = page[page.find(f'id="job-item-{jid}"'):]
    block = block[:block.find('id="job-item-', 20)] if 'id="job-item-' in block[20:] else block[:20000]
    meta = [clean(s) for s in re.findall(r'<span class="(?:text-nowrap|location-text)">(.*?)</span>', block, re.S)]
    base = _obj(p.get("baseSalary"), "value")
    salary = _obj(base.get("value"), "value")
    months = _obj(p.get("experienceRequirements"), "monthsOfExperience").get("monthsOfExperience")
    org = _obj(p.get("hiringOrganization"))
    reqs = p.get("applicantLocationRequirements") or []
    addrs = [_obj(_obj(r).get("address")) for r in (reqs if isinstance(reqs, list) else [reqs])]
    region = ", ".join(str(a.get("addressRegion") or a.get("addressCountry") or a.get("name") or "")
                       for a in addrs).strip(", ") or None
    return {
        "source_id": jid, "url": p.get("url"), "title": p.get("title"),
        "company": org.get("name"), "company_url": org.get("sameAs"),
        "salary_min": salary.get("minValue"), "salary_max": salary.get("maxValue"),
        "currency": base.get("currency"),
        "experience_years": round(float(months) / 12, 1) if months is not None else None,
        "employment": p.get("employmentType"), "category": p.get("category"),
        "format": "remote" if p.get("jobLocationType") == "TELECOMMUTE" else None,
        "posted_at": (p.get("datePosted") or "")[:10] or None,
        "description": (p.get("description") or "").strip() or None,
        "meta": meta, "region": region, "industry": p.get("industry"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--years", type=float, required=True, help="commercial experience N; searched N-2 … N+1")
    ap.add_argument("--max-exp", type=float, help="upper end of the experience window in years (default N+1)")
    ap.add_argument("--keyword", action="append", required=True,
                    help="Djinni primary keyword (category), repeatable: JavaScript, Node.js, Python, Java, .NET, PHP, Golang, Fullstack…")
    ap.add_argument("--search", help="free-text filter (Djinni 'all_keywords')")
    ap.add_argument("--remote", action="store_true", help="remote vacancies only")
    ap.add_argument("--english", choices=ENGLISH, help="candidate's English level (Djinni filter)")
    ap.add_argument("--region", action="append", choices=REGIONS, help="where the job accepts candidates, repeatable")
    ap.add_argument("--out", help="session folder (default <JOB_SEARCH_DIR>/<date>-djinni)")
    ap.add_argument("--lang", default="ru", choices=("ru", "uk", "en"), help="language of the .md report")
    ap.add_argument("--max-pages", type=int, default=20, help="listing pages, ~15 vacancies each (default 20)")
    ap.add_argument("--ignore-gap", action="store_true", help="skip the pause-between-runs check (only on the user's request)")
    args = ap.parse_args()

    query = {"years": args.years, "experience_range": exp_range(args.years, args.max_exp),
             "djinni_exp": exp_levels(args.years, args.max_exp),
             "keywords": args.keyword, "search": args.search, "remote": args.remote, "english": args.english,
             "region": args.region}
    params = [("primary_keyword", k) for k in args.keyword] + [("exp_level", e) for e in query["djinni_exp"]]
    if args.search:
        params.append(("all_keywords", args.search))
    if args.remote:
        params.append(("employment", "remote"))
    if args.english:
        params.append(("english_level", args.english))
    params += [("region", r) for r in args.region or []]

    def collect(budget, log, records):
        client = PoliteClient("djinni", budget, log)
        for n in range(1, args.max_pages + 1):
            page = client.request(BASE + "?" + urllib.parse.urlencode(params + ([("page", n)] if n > 1 else [])))
            items = parse_page(page, log)
            log(f"page {n}: {len(items)} vacancies")
            for it in items:
                if it["source_id"] not in records:
                    records[it["source_id"]] = to_record(it)
            # Next page exists if the site links it — or, should the pager markup
            # change, if this page was full. An empty page ends the loop either way.
            if not items or (f"page={n + 1}" not in page and len(items) < PAGE_SIZE):
                break
        else:
            log.warn(f"max-pages {args.max_pages} reached — there may be more vacancies")
        return client

    return run_collector("djinni", args, query, collect)


def to_record(it: dict) -> dict:
    meta = it.pop("meta")
    exp_text = next((m for m in meta if "досвід" in m or "experience" in m.lower()), None)
    english = next((m for m in meta if m.startswith(("Англійська", "English"))), None)
    stats = [m for m in meta if "перегляд" in m or "відгук" in m]  # competition: views / applications
    place = [m for m in meta if m not in (exp_text, english, "Більше") and m not in stats
             and (it["industry"] or "").lower() not in m.lower()][:2]
    cur = it["currency"]
    salary_text = (f"{it['salary_min'] or ''}–{it['salary_max'] or ''} {cur or ''}".strip()
                   if it["salary_min"] or it["salary_max"] else None)
    return record(
        platform="djinni", source_id=it["source_id"], url=it["url"], title=it["title"],
        company=it["company"], company_url=it["company_url"],
        location=" · ".join(place) or it["region"], format=it["format"],
        salary_min=it["salary_min"], salary_max=it["salary_max"], currency=cur, salary_text=salary_text,
        experience_years=it["experience_years"], experience_text=exp_text, english=english,
        employment=it["employment"], category=it["category"], posted_at=it["posted_at"],
        description=it["description"], description_complete=bool(it["description"]),
        notes="; ".join(x for x in (f"industry: {it['industry']}" if it["industry"] else "", ", ".join(stats)) if x) or None,
    )


if __name__ == "__main__":
    sys.exit(main())
