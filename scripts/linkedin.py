"""Collect vacancies from LinkedIn — one platform per run.

    python linkedin.py --years 4 --keyword "Node.js developer" --keyword "Full stack TypeScript"
                       --location "European Union" [--remote] [--date-posted past_month]
                       [--out <session folder>] [--lang ru|uk|en]

Talks to the `linkedin` MCP server directly (stdio JSON-RPC), so the data
never passes through an LLM. The server runs on the user's logged-in LinkedIn
session — the account is what gets restricted, so this collector has the
strictest pacing and budget (page loads per day, 2 h between runs). Exit 2 =
refused or stopped early (partial results still written).

Server command: `--server-cmd`, else env `LINKEDIN_MCP_CMD`, else the
`linkedin` entry of the repo's `.mcp.json`, else `uvx mcp-server-linkedin`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # find common.py from any cwd
from common import (REPO, STATE, Budget, Cache, McpClient, Pacer, Stop, add_title_filters, exp_range, parse_salary,
                    pending_ids, record, run_collector, seen_ids, title_rank)

# `experience_level` values of the server's `search_jobs`, by years of experience.
LEVELS = [((0, 1), ["internship", "entry"]), ((1, 3), ["entry", "associate"]),
          ((2, 5), ["associate", "mid_senior"]), ((5, 99), ["mid_senior"])]
DESCRIPTION_END = ("About the company", "Set alert for similar jobs", "Similar jobs", "People also viewed",
                   "Looking for talent?", "More jobs")
WORK_TYPES = ("Remote", "Hybrid", "On-site")


def levels_for(years: float, cap: float | None = None) -> list[str]:
    lo, hi = exp_range(years, cap)
    out: list[str] = []
    for (a, b), names in LEVELS:
        if a <= hi and b >= lo:
            out += [n for n in names if n not in out]
    return out


def server_command(arg: str | None) -> list[str]:
    if arg:
        return shlex.split(arg)
    if os.environ.get("LINKEDIN_MCP_CMD"):
        return shlex.split(os.environ["LINKEDIN_MCP_CMD"])
    cfg = REPO / ".mcp.json"
    if cfg.exists():
        entry = (json.loads(cfg.read_text("utf-8")).get("mcpServers") or {}).get("linkedin")
        if entry and entry.get("command"):
            return [entry["command"], *entry.get("args", [])]
    return ["uvx", "mcp-server-linkedin"]


def card_title(text: str | None) -> str | None:
    """Title from a search-result link, or None for the card's widget text
    ("Is this information helpful? …") that shares the job's link."""
    if not text or text.lower().startswith("is this information helpful"):
        return None
    return text


_CARD_LOCATION = re.compile(r"\((Remote|Hybrid|On-site)\)$")


def text_titles(text: str | None) -> list[str]:
    """Card titles, in page order, from the search page's visible text.

    LinkedIn's result links no longer carry the title, but every card reads
    "<title> / <company> / <location (Remote|Hybrid|On-site)>". A title that
    itself ends in "(Remote)" also matches the pattern, so a line repeated
    next to itself or following the "Be an early applicant" badge is not a card."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    out = []
    for i, ln in enumerate(lines):
        if i < 2 or not _CARD_LOCATION.search(ln):
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if ln in (nxt, lines[i - 1]) or lines[i - 1].lower() == "be an early applicant":
            continue
        out.append(re.sub(r"\s*\(Verified job\)$", "", lines[i - 2]).removeprefix("Selected, ").strip())
    return out


def ago_to_date(text: str) -> str | None:
    m = re.search(r"(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago", text)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    days = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}[unit] * n
    return (date.today() - timedelta(days=days)).isoformat()


def parse_job(job_id: str, data: dict) -> dict:
    text = ((data.get("sections") or {}).get("job_posting") or data.get("text") or "").strip()
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    company = lines[0] if lines else None
    title = lines[1] if len(lines) > 1 else None
    meta = lines[2] if len(lines) > 2 else ""
    header = lines[:12]
    desc = ""
    if "About the job" in text:
        desc = text.split("About the job", 1)[1]
        cut = min([desc.find(m) for m in DESCRIPTION_END if m in desc] or [len(desc)])
        desc = desc[:cut].strip()
    salary = next((l for l in header if "$" in l or "€" in l or "/yr" in l or "/hr" in l), None)
    lo, hi, cur = parse_salary(salary) if salary else (None, None, None)
    work = next((w for w in WORK_TYPES if w in header), None)
    return {
        "source_id": job_id, "url": data.get("url") or f"https://www.linkedin.com/jobs/view/{job_id}/",
        "title": title, "company": company, "location": meta.split("·")[0].strip() or None,
        "format": {"Remote": "remote", "Hybrid": "hybrid", "On-site": "office"}.get(work),
        "salary_text": salary, "salary_min": lo, "salary_max": hi, "currency": cur,
        "employment": next((l for l in header if l in ("Full-time", "Part-time", "Contract", "Temporary")), None),
        "posted_at": ago_to_date(meta), "description": desc or None,
        "notes": " · ".join(p.strip() for p in meta.split("·")[1:]) or None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--years", type=float, required=True, help="commercial experience N; searched N-2 … N+1")
    ap.add_argument("--max-exp", type=float, help="upper end of the experience window in years (default N+1)")
    ap.add_argument("--keyword", action="append", required=True, help="search phrase, repeatable (one search each)")
    ap.add_argument("--location", action="append", help="LinkedIn location, repeatable, e.g. 'European Union', "
                                                         "'Ukraine' (without it LinkedIn uses the account's country)")
    ap.add_argument("--remote", action="store_true", help="remote vacancies only")
    ap.add_argument("--date-posted", choices=("auto", "past_24_hours", "past_week", "past_month"), default="past_month",
                    help="auto: past_24_hours if the last LinkedIn run ended < 23.5 h ago, else past_week")
    ap.add_argument("--pages-per-keyword", type=int, default=2, help="search result pages per phrase (default 2)")
    ap.add_argument("--keyword-pages", action="append", default=[], metavar="PHRASE=N",
                    help="pages for one phrase, overrides --pages-per-keyword, repeatable")
    ap.add_argument("--max-details", type=int, default=60, help="max job pages to open (default 60)")
    ap.add_argument("--cache-days", type=float, default=7)
    ap.add_argument("--server-cmd", help="command that starts the LinkedIn MCP server")
    ap.add_argument("--out", help="session folder (default <JOB_SEARCH_DIR>/<date>-linkedin)")
    ap.add_argument("--lang", default="ru", choices=("ru", "uk", "en"), help="language of the .md report")
    ap.add_argument("--ignore-gap", action="store_true", help="skip the pause-between-runs check (only on the user's request)")
    ap.add_argument("--only-new", action="store_true",
                    help="skip vacancies already collected in earlier sessions (no detail call, not in the report)")
    add_title_filters(ap)
    args = ap.parse_args()

    if args.date_posted == "auto":
        mins = Budget("linkedin").minutes_since_last_run()
        args.date_posted = "past_24_hours" if mins is not None and mins < 23.5 * 60 else "past_week"
    kw_pages = {}
    for item in args.keyword_pages:
        phrase, _, n = item.rpartition("=")
        if not phrase or not n.isdigit():
            ap.error(f"--keyword-pages expects PHRASE=N, got {item!r}")
        kw_pages[phrase] = int(n)
    levels = levels_for(args.years, args.max_exp)
    query = {"years": args.years, "experience_range": exp_range(args.years, args.max_exp), "experience_levels": levels,
             "keywords": args.keyword, "locations": args.location, "remote": args.remote,
             "date_posted": args.date_posted}

    def collect(budget, log, records):
        pacer, cache = Pacer("linkedin", log), Cache("linkedin", args.cache_days)
        client = McpClient(server_command(args.server_cmd), log)
        titles: dict[str, str] = {}
        skipped: dict[str, str | None] = {}
        seen = seen_ids("linkedin") if args.only_new else None
        if seen is not None:
            log(f"only-new: {len(seen)} LinkedIn vacancies known from earlier sessions")
        try:
            searches = [(kw, loc) for kw in args.keyword for loc in (args.location or [None])]
            for kw, loc in searches:
                pages = max(1, min(kw_pages.get(kw, args.pages_per_keyword), 5))
                budget.spend(pages)
                pacer.before(pages)
                params = {"keywords": kw, "experience_level": ",".join(levels), "date_posted": args.date_posted,
                          "max_pages": pages}
                if loc:
                    params["location"] = loc
                if args.remote:
                    params["work_type"] = "remote"
                res = client.tool("search_jobs", params)
                debug = STATE / "debug"
                debug.mkdir(parents=True, exist_ok=True)
                (debug / "linkedin-last-search.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), "utf-8")
                ids = [str(i) for i in res.get("job_ids") or []]
                for ref in (res.get("references") or {}).get("search_results", []):
                    m = re.search(r"/jobs/view/(\d+)", ref.get("url", ""))
                    if m and card_title(ref.get("text")):
                        titles.setdefault(m.group(1), card_title(ref.get("text")))
                # Links without titles → read them from the page text, matched by order; only when the
                # card count equals the id count, otherwise a shifted list would mislabel vacancies.
                if any(i not in titles for i in ids):
                    from_text = text_titles((res.get("sections") or {}).get("search_results"))
                    if len(from_text) == len(ids):
                        for i, t in zip(ids, from_text):
                            titles.setdefault(i, t)
                    else:
                        log(f"'{kw}': {len(from_text)} card titles in page text vs {len(ids)} ids — not matched")
                total = (res.get("total") or {}).get("count")
                log(f"search '{kw}' @ {loc or 'account country'}: {total} on site, {len(ids)} ids from {pages} page(s)")
                if total and len(ids) < total:
                    log.warn(f"'{kw}': got {len(ids)} of {total} — more pages / narrower phrases would find more")
                if total and not ids:
                    raise Stop(f"LinkedIn shows {total} results for '{kw}' but returned no ids — format changed?")
                untitled = [i for i in ids if i not in titles]
                if ids and len(untitled) > len(ids) // 2:
                    log.warn(f"'{kw}': {len(untitled)} of {len(ids)} ids came without a title — search format "
                             f"changed? raw response in .state/debug/linkedin-last-search.json")
                if seen is not None:
                    fresh = [i for i in ids if i not in seen]
                    log(f"'{kw}': {len(ids) - len(fresh)} known ones skipped, {len(fresh)} new")
                    ids = fresh
                for i in ids:
                    if i not in records and title_rank(titles.get(i), args) is None:
                        skipped[i] = titles.get(i)
                        continue
                    records.setdefault(i, record(platform="linkedin", source_id=i,
                                                 url=f"https://www.linkedin.com/jobs/view/{i}/",
                                                 title=titles.get(i), description_complete=False,
                                                 notes=f"search: {kw} @ {loc or '-'}"))
            if seen is not None:
                pending = pending_ids("linkedin")
                backlog = sorted((i for i in pending if i not in records and i not in skipped), reverse=True)
                if backlog:
                    log(f"only-new: {len(backlog)} listed earlier but never opened — retried")
                for i in backlog:  # newest ids first
                    pending[i] = card_title(pending[i])  # older reports may hold widget text
                    if title_rank(pending[i], args) is None:
                        skipped[i] = pending[i]
                        continue
                    records.setdefault(i, record(platform="linkedin", source_id=i,
                                                 url=f"https://www.linkedin.com/jobs/view/{i}/", title=pending[i],
                                                 description_complete=False, notes="backlog from an earlier run"))
            for i, t in skipped.items():
                log(f"skip-title: {i} {t}")
            if skipped:
                log(f"skip-title: {len(skipped)} vacancies not opened by title")
            # Detail order: normal titles first, then --later-title ones and repeats of a title
            # already queued (one company reposting the same job under several ids).
            queued, order = set(), {}
            for n, (i, rec) in enumerate(records.items()):
                norm = re.sub(r"\W+", " ", (rec.get("title") or "").lower()).strip()
                order[i] = (title_rank(rec.get("title"), args) or (1 if norm and norm in queued else 0), n)
                queued.add(norm)
            opened = 0
            for i, rec in sorted(records.items(), key=lambda kv: order[kv[0]]):
                data = cache.get(i)
                if data and not (data.get("sections") or {}).get("job_posting"):
                    data = None  # a failed page load (timeout, network drop) cached by an older run
                if not data:
                    if opened >= args.max_details:
                        continue
                    budget.spend(1)
                    pacer.before(1)
                    data = client.tool("get_job_details", {"job_id": i})
                    opened += 1
                    if (data.get("sections") or {}).get("job_posting"):
                        cache.put(i, data)
                    else:  # page didn't load (timeout, network drop): keep it out of the cache, retry next run
                        errors = data.get("section_errors") or {}
                        reason = next((e.get("error_type") for e in errors.values() if isinstance(e, dict)), None)
                        log.warn(f"{i}: page not loaded ({reason or 'empty response'}) — not cached, retried next run")
                job = parse_job(i, data)
                notes = "; ".join(x for x in (rec["notes"], job.pop("notes")) if x)
                rec.update({k: v for k, v in job.items() if v is not None})
                rec["notes"] = notes
                rec["description_complete"] = bool(job["description"])
            if any(not r["description_complete"] for r in records.values()):
                log(f"max-details {args.max_details}: the rest keep title only")
        finally:
            client.close()
        return pacer.count

    return run_collector("linkedin", args, query, collect)


if __name__ == "__main__":
    sys.exit(main())
