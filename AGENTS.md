---
kind: daemon
when: collect a full list of IT vacancies with descriptions from one platform (dou, djinni or linkedin) into a career/searches/ session — scripts only: run them directly, no subagent
works-for: domains/career/
---

# it-job-searcher — collects IT vacancies from one job platform

Collectors that gather, as completely as possible, every vacancy on **one**
platform that is plausibly useful for a given stack and experience, with full
descriptions, into a search session. They do not rank, judge fit or merge
duplicates across platforms — that is `it-job-analyzer`.

**No subagent needed.** All three collectors are plain scripts with no LLM
work inside (LinkedIn included — the script talks to the MCP server itself).
The caller runs them directly and reads only the one-line JSON summary each
prints; the `.md` / `.json` reports are for the analyzer and for people.

## Contract

- **One platform per run:** `dou.py`, `djinni.py` or `linkedin.py`. Several
  platforms = several runs into the same session (in parallel is fine —
  different platforms only; the scripts refuse a second run on the same one).
- **Input (all from the caller, nothing personal is built in):** experience N
  (`--years`, searched **N−2 … N+1**; `--max-exp` caps the upper end), the platform's stack filters, other
  hard filters (remote, region, English…), session folder (`--out`).
- **Output** in the session folder, named by search start time and platform:
  `<YYYY-MM-DD_HH-MM-SS>_<platform>.json` (records), `.md` (readable list with
  full descriptions, `--lang ru|uk|en`), `.log` (every request). Written even
  when stopped early.
- **Summary** (stdout, one JSON line): `platform, count, requests, stopped_early,
  warnings, files` — or `refused` when the run was not allowed to start.
  Exit `0` = done; `2` = refused / stopped early → report it, **don't retry**.
- **Runs on** Windows / macOS / Linux: Python 3, standard library only, any cwd.

## Mapping a stack to platform filters

Breadth over precision: when unsure whether a category fits, include it — the
analyzer filters.

| Platform | Stack → | Other filters |
|---|---|---|
| `dou.py` | `--category` (repeat), e.g. JS/TS backend & fullstack → `Node.js`, `Front End`; Python → `Python`; optional `--search <keyword>` | `--remote`, `--city <name>` |
| `djinni.py` | `--keyword` (repeat): `JavaScript`, `Node.js`, `Fullstack`, `Python`, `Java`, `.NET`, `PHP`, `Golang`… | `--remote`, `--english <level>`, `--region UKR/eu/worldwide/other`, `--search <text>` |
| `linkedin.py` | `--keyword` (repeat) — search phrases, e.g. "Node.js developer", "Full stack TypeScript"; narrower phrases find more within the budget | `--remote`, `--location` (**always set it** — otherwise LinkedIn uses the account's country), `--date-posted` |

```
python daemons/it-job-searcher/scripts/dou.py      --years N --category "Node.js" --category "Front End" --remote --out <session>
python daemons/it-job-searcher/scripts/djinni.py   --years N --keyword JavaScript --keyword Node.js --remote --out <session>
python daemons/it-job-searcher/scripts/linkedin.py --years N --keyword "Node.js developer" --location "European Union" --remote --out <session>
```

## Completeness — what the scripts already do

- DOU: every experience bucket touching the window (inclusive), plus one pass
  without the experience filter for vacancies that don't state it; collected
  count is checked against the site's own counter.
- Djinni: follows pages until an empty one; one page carries ~15 full vacancies.
- LinkedIn: the budget covers only part of a big result set — the summary
  warns "got X of Y"; use several narrow phrases and `--date-posted past_week`
  for regular runs.
- A vacancy without its full text is marked `description_complete: false`;
  markup changes stop the run with an error instead of returning a silent zero.

## Anti-ban rules (enforced by the scripts — never work around them)

- One run per platform at a time (lock file); jittered pauses between requests
  (DOU / Djinni 4–9 s, LinkedIn 8–20 s), a longer pause every 10–15 requests,
  strictly sequential.
- Daily budgets: DOU 300, Djinni 200 requests, LinkedIn 60 page loads; at least
  30 min between DOU / Djinni runs, 2 h between LinkedIn runs.
- 403 / 429 / captcha / anti-bot page → the run stops; that is final for this run.
- Fetched vacancy pages are cached (`--cache-days`, default 7).
- `--ignore-gap` only on the user's explicit request.
- DOU / Djinni: public pages only, no login, only `/vacancies/` and `/jobs/`
  (allowed by their `robots.txt`). LinkedIn: only `search_jobs` and
  `get_job_details` — nothing is sent, saved or applied for.
- State (budgets, locks, cache): `$JOB_SEARCH_STATE_DIR`, default
  `domains/career/searches/.state/`; tests set it to a temp folder.

## Limits

Read-only everywhere. Never invents data — unknown salary / experience stays
empty. Writes only into the session folder and the state folder. Glassdoor:
not supported (no API, strong anti-bot).

## Scripts — `scripts/`

| File | What |
|---|---|
| `common.py` | polite HTTP client, budgets + lock, cache, MCP stdio client, record schema (`RECORD_FIELDS`), report writer |
| `dou.py` | jobs.dou.ua: listings + "load more" + vacancy pages |
| `djinni.py` | djinni.co: listing pages with full descriptions (structured data) |
| `linkedin.py` | LinkedIn through the `linkedin` MCP server (command from `.mcp.json`, `LINKEDIN_MCP_CMD` or `--server-cmd`) |
