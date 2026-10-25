"""Shared helpers for the it-job-searcher collectors.

Polite HTTP client (pacing, backoff, block detection), per-platform budgets
and run lock, detail cache, a minimal MCP stdio client, the record schema and
the report writer. Standard library only, so the collectors run the same on
Windows, macOS and Linux with plain `python`.
"""

from __future__ import annotations

import html
import http.cookiejar
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

# The Windows console is not UTF-8 by default; vacancy texts are Cyrillic.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[3]
# Where sessions go when `--out` is not given, and where budgets / locks / cache
# live. Both overridable, so the scripts work outside this repo layout too.
SEARCHES = Path(os.environ.get("JOB_SEARCH_DIR") or REPO / "domains" / "career" / "searches")
STATE = Path(os.environ.get("JOB_SEARCH_STATE_DIR") or SEARCHES / ".state")

# Anti-ban policy per platform. Delays are seconds between two requests
# (random within the range); a "breather" is a longer pause every N requests.
POLICY = {
    "dou": dict(delay=(4.0, 9.0), breather_every=15, breather=(25.0, 45.0),
                daily_requests=300, min_gap_minutes=30),
    "djinni": dict(delay=(4.0, 9.0), breather_every=15, breather=(25.0, 45.0),
                   daily_requests=200, min_gap_minutes=30),
    # LinkedIn runs on the user's logged-in account: the strictest limits,
    # the account is what gets restricted. Counted in page loads.
    "linkedin": dict(delay=(8.0, 20.0), breather_every=10, breather=(45.0, 90.0),
                     daily_requests=60, min_gap_minutes=120),
}
LOCK_STALE_HOURS = 3

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
BLOCK_MARKERS = (
    "cf-chl-", "challenge-platform", "Just a moment...", "Attention Required!",
    "g-recaptcha", "hcaptcha", "captcha-delivery",
)

RECORD_FIELDS = (
    "platform", "source_id", "url", "title", "company", "company_url",
    "location", "format", "salary_min", "salary_max", "currency", "salary_text",
    "experience_years", "experience_text", "english", "employment", "category",
    "posted_at", "collected_at", "description", "description_complete", "notes",
)


class Stop(Exception):
    """Collection must stop now (blocked, budget used up, markup changed).
    Partial results are kept."""


# ------------------------------------------------------- budget and lock ---

class Budget:
    """Per-platform ledger in `.state/<platform>-ledger.json` (requests per day,
    end of the last run) plus a lock file, so two collectors never hit the
    same site at once and runs can't follow each other too closely."""

    def __init__(self, platform: str):
        self.platform = platform
        self.policy = POLICY[platform]
        self.path = STATE / f"{platform}-ledger.json"
        self.lock = STATE / f"{platform}.lock"
        self.data = json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}
        self.today = date.today().isoformat()
        self.locked = False

    @property
    def used_today(self) -> int:
        return self.data.get("days", {}).get(self.today, 0)

    @property
    def left_today(self) -> int:
        return max(0, self.policy["daily_requests"] - self.used_today)

    def minutes_since_last_run(self) -> float | None:
        last = self.data.get("last_run_end")
        if not last:
            return None
        return (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 60

    def begin(self, ignore_gap: bool = False) -> None:
        """Refuse (Stop) or take the platform lock. Nothing is spent on refusal."""
        if self.lock.exists():
            info = json.loads(self.lock.read_text("utf-8") or "{}")
            age = (datetime.now() - datetime.fromisoformat(info.get("since", "2000-01-01"))).total_seconds() / 3600
            if age < LOCK_STALE_HOURS:
                raise Stop(f"another {self.platform} collector is running since {info.get('since')} "
                           f"(pid {info.get('pid')}); one run per platform at a time")
        gap = self.minutes_since_last_run()
        need = self.policy["min_gap_minutes"]
        if gap is not None and gap < need and not ignore_gap:
            raise Stop(f"last {self.platform} run ended {gap:.0f} min ago; "
                       f"wait at least {need} min between runs")
        if self.left_today <= 0:
            raise Stop(f"daily {self.platform} budget ({self.policy['daily_requests']}) is used up")
        STATE.mkdir(parents=True, exist_ok=True)
        self.lock.write_text(json.dumps({"pid": os.getpid(), "since": datetime.now().isoformat(timespec="seconds")}), "utf-8")
        self.locked = True

    def spend(self, n: int = 1) -> None:
        if self.left_today < n:
            raise Stop(f"daily {self.platform} budget ({self.policy['daily_requests']}) is used up")
        days = self.data.setdefault("days", {})
        days[self.today] = days.get(self.today, 0) + n
        for d in sorted(days)[:-14]:  # two weeks of history
            del days[d]
        self._save()

    def finish(self) -> None:
        self.data["last_run_end"] = datetime.now().isoformat(timespec="seconds")
        self._save()
        if self.locked:
            self.lock.unlink(missing_ok=True)
            self.locked = False

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), "utf-8")
        tmp.replace(self.path)


class Pacer:
    """Jittered pause before every request / page load after the first one."""

    def __init__(self, platform: str, log):
        self.p = POLICY[platform]
        self.log = log
        self.count = 0

    def before(self, cost: int = 1) -> None:
        if self.count:
            if self.count // self.p["breather_every"] != (self.count + cost) // self.p["breather_every"]:
                pause = random.uniform(*self.p["breather"])
                self.log(f"breather {pause:.0f}s after {self.count} requests")
            else:
                pause = random.uniform(*self.p["delay"])
            time.sleep(pause)
        self.count += cost


# ------------------------------------------------------------------ http ---

class PoliteClient:
    """One cookie session per run (like a single browser tab), strictly
    sequential requests with jittered pauses, honours Retry-After, stops at
    the first sign of a block instead of retrying through it."""

    def __init__(self, platform: str, budget: Budget, log):
        self.platform = platform
        self.budget = budget
        self.log = log
        self.pacer = Pacer(platform, log)
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
        }

    @property
    def count(self) -> int:
        return self.pacer.count

    def request(self, url: str, data: dict | None = None, headers: dict | None = None) -> str:
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        for attempt in range(3):
            self.budget.spend()
            self.pacer.before()
            req = urllib.request.Request(url, data=body, headers={**self.headers, **(headers or {})})
            try:
                with self.opener.open(req, timeout=30) as resp:
                    text = resp.read().decode("utf-8", "replace")
                    self.log(f"{'POST' if body else 'GET'} {resp.status} {url}")
                    if any(m in text for m in BLOCK_MARKERS):
                        raise Stop(f"anti-bot challenge on {url} — stopped, try again later")
                    return text
            except urllib.error.HTTPError as e:
                self.log(f"HTTP {e.code} {url}")
                if e.code in (429, 503) and attempt < 2:
                    wait = _retry_after(e.headers.get("Retry-After")) or (60 * (attempt + 1))
                    self.log(f"rate limited, waiting {wait:.0f}s")
                    time.sleep(wait)
                    continue
                if e.code in (403, 429, 503):
                    raise Stop(f"HTTP {e.code} on {url} — looks like rate limiting / block, stopped")
                raise
            except (urllib.error.URLError, TimeoutError) as e:
                self.log(f"network error {e} on {url}")
                if attempt < 1:
                    time.sleep(20)
                    continue
                raise Stop(f"network error on {url}: {e}")
        raise Stop(f"gave up on {url}")


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return min(float(value), 300.0)
    except ValueError:
        return None


# ------------------------------------------------------------ MCP client ---

class McpClient:
    """Minimal MCP stdio client: start a server process, call its tools.
    Used for LinkedIn so the data never passes through an LLM."""

    def __init__(self, command: list[str], log):
        exe = shutil.which(command[0]) or command[0]
        self.log = log
        self.proc = subprocess.Popen([exe, *command[1:]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        self.stderr: list[str] = []
        threading.Thread(target=self._drain, daemon=True).start()
        self.next_id = 0
        info = self._call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "it-job-searcher", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        log(f"MCP server {(info.get('serverInfo') or {}).get('name')} {(info.get('serverInfo') or {}).get('version')}")

    def _drain(self):
        for line in self.proc.stderr:
            self.stderr.append(line.decode("utf-8", "replace"))
            del self.stderr[:-50]

    def _send(self, msg: dict) -> None:
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def _call(self, method: str, params: dict, timeout: float = 300) -> dict:
        self.next_id += 1
        my_id = self.next_id
        self._send({"jsonrpc": "2.0", "id": my_id, "method": method, "params": params})
        end = time.time() + timeout
        while time.time() < end:
            line = self.proc.stdout.readline()
            if not line:
                if self.proc.poll() is not None:
                    raise Stop("MCP server exited: " + "".join(self.stderr[-5:])[-500:])
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == my_id:
                if "error" in msg:
                    raise Stop(f"MCP {method} error: {msg['error']}")
                return msg.get("result") or {}
        raise Stop(f"MCP {method} timed out after {timeout:.0f}s")

    def tool(self, name: str, arguments: dict) -> dict:
        res = self._call("tools/call", {"name": name, "arguments": arguments})
        texts = [c.get("text", "") for c in res.get("content", []) if c.get("type", "text") == "text"]
        if res.get("isError"):
            raise Stop(f"{name} failed: {' '.join(texts)[:300]}")
        joined = "\n".join(texts).strip()
        start = joined.find("{")
        if start >= 0:
            try:
                return json.JSONDecoder().raw_decode(joined[start:])[0]
            except ValueError:
                pass
        return {"text": joined}

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


# ----------------------------------------------------------------- cache ---

class Cache:
    """Detail pages already fetched (per platform, by source id), so a vacancy
    is not downloaded again within `ttl_days`."""

    def __init__(self, platform: str, ttl_days: float = 7):
        self.dir = STATE / "cache" / platform
        self.ttl = timedelta(days=ttl_days)

    def get(self, key: str) -> dict | None:
        path = self.dir / f"{key}.json"
        if not path.exists():
            return None
        item = json.loads(path.read_text("utf-8"))
        if datetime.now() - datetime.fromisoformat(item["_cached_at"]) > self.ttl:
            return None
        return item

    def put(self, key: str, item: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        item = {**item, "_cached_at": datetime.now().isoformat(timespec="seconds")}
        (self.dir / f"{key}.json").write_text(json.dumps(item, ensure_ascii=False), "utf-8")


# -------------------------------------------------------------- parsing ---

class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.parts.append("\n- ")
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(fragment: str) -> str:
    p = _TextExtractor()
    p.feed(fragment or "")
    text = "".join(p.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def clean(fragment: str | None) -> str:
    """Inline HTML fragment → one line of plain text."""
    if not fragment:
        return ""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def inner_html(page: str, tag: str, class_token: str) -> str | None:
    """innerHTML of the first <tag> whose class contains `class_token`
    (depth-aware, so nested tags of the same name are handled)."""
    m = re.search(rf'<{tag}\b[^>]*class="[^"]*\b{re.escape(class_token)}\b[^"]*"[^>]*>', page)
    if not m:
        return None
    depth, pos = 1, m.end()
    for t in re.finditer(rf"<(/?){tag}\b[^>]*>", page[pos:], re.I):
        depth += -1 if t.group(1) else 1
        if depth == 0:
            return page[pos:pos + t.start()]
    return page[pos:]


UA_MONTHS = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}


def parse_ua_date(text: str) -> str | None:
    """'13 жовтня' / '25 вересня 2026' → ISO date (year guessed if missing)."""
    m = re.search(r"(\d{1,2})\s+([а-яіїєґ']+)(?:\s+(\d{4}))?", (text or "").lower())
    if not m or m.group(2) not in UA_MONTHS:
        return None
    today = date.today()
    year = int(m.group(3)) if m.group(3) else today.year
    d = date(year, UA_MONTHS[m.group(2)], int(m.group(1)))
    if not m.group(3) and d > today:
        d = d.replace(year=year - 1)
    return d.isoformat()


def parse_salary(text: str | None) -> tuple[int | None, int | None, str | None]:
    """'$1600–2000' / 'до $3000' / 'від $2500' / '$60K/yr' → (min, max, currency).
    A lone 'from' number gives max = None (upper bound unknown)."""
    if not text:
        return None, None, None
    cur = "USD" if "$" in text else "EUR" if "€" in text else "UAH" if ("грн" in text or "₴" in text) else None
    low = text.lower()
    raw = re.findall(r"(\d[\d  ,.]*)\s*([kк])?", low)
    nums = []
    for num, k in raw:
        digits = re.sub(r"[  ,]", "", num).rstrip(".")
        if not digits or not re.fullmatch(r"\d+(\.\d+)?", digits):
            continue
        value = float(digits) * (1000 if k else 1)
        nums.append(int(value))
    if any(s in low for s in ("/yr", "per year", "/year", "на рік", "в год")):
        nums = [n // 12 for n in nums]  # yearly → monthly
    if not nums:
        return None, None, cur
    if len(nums) == 1:
        if "до" in low or "up to" in low:
            return None, nums[0], cur
        if "від" in low or "от " in low or "from" in low or "+" in low:
            return nums[0], None, cur
        return nums[0], nums[0], cur
    return min(nums[:2]), max(nums[:2]), cur


# -------------------------------------------------------------- helpers ---

def exp_range(years: float, cap: float | None = None) -> tuple[float, float]:
    """Experience window searched around N years: N-2 … N+1 (floor 0),
    the upper end limited by `cap` (`--max-exp`) when given."""
    hi = years + 1 if cap is None else min(years + 1, cap)
    return max(0.0, years - 2), hi


def record(**fields) -> dict:
    rec = {k: None for k in RECORD_FIELDS}
    unknown = set(fields) - set(RECORD_FIELDS)
    if unknown:
        raise ValueError(f"unknown record fields: {unknown}")
    rec.update(fields)
    rec["collected_at"] = rec["collected_at"] or datetime.now().isoformat(timespec="seconds")
    return rec


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "search"


def session_dir(out: str | None, label: str) -> Path:
    """`--out` as given (absolute or relative to the current folder), else
    `<JOB_SEARCH_DIR>/<YYYY-MM-DD>-<label>/`."""
    path = Path(out).resolve() if out else SEARCHES / f"{date.today().isoformat()}-{slug(label)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


class RunLog:
    def __init__(self):
        self.lines: list[str] = []
        self.warnings: list[str] = []

    def __call__(self, msg: str) -> None:
        line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
        self.lines.append(line)
        print(line, flush=True)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        self(f"WARNING: {msg}")


def run_collector(platform: str, args, query: dict, collect) -> int:
    """Common skeleton: lock + budget, collect, always write the report,
    print a one-line JSON summary (all the caller needs to read)."""
    started, log = datetime.now(), RunLog()
    budget = Budget(platform)
    try:
        budget.begin(getattr(args, "ignore_gap", False))
    except Stop as e:  # refused before any request: no report, nothing spent
        print(json.dumps({"platform": platform, "refused": str(e)}, ensure_ascii=False))
        return 2
    session = session_dir(args.out, platform)
    records: dict[str, dict] = {}
    stopped, requests = None, 0
    try:
        log(f"{platform} search {query}; budget left today: {budget.left_today}")
        requests = collect(budget, log, records)
    except Stop as e:
        stopped = str(e)
        log(f"STOP: {stopped}")
    except Exception as e:  # parser broke (site changed?) — keep what we have
        stopped = f"script error: {type(e).__name__}: {e}"
        log(f"ERROR: {stopped}")
    finally:
        budget.finish()
    if isinstance(requests, PoliteClient):
        requests = requests.count
    meta = write_report(session, platform, started, query, list(records.values()), log, stopped,
                        int(requests or 0), getattr(args, "lang", "ru"))
    print(json.dumps({k: meta[k] for k in ("platform", "count", "requests", "stopped_early", "warnings", "files")},
                     ensure_ascii=False))
    return 2 if stopped else 0


# --------------------------------------------------------------- report ---

LABELS = {
    "ru": dict(title="Вакансии", platform="Платформа", started="Поиск начат", finished="закончен",
               params="Параметры", found="Найдено", requests="запросов к платформе",
               stopped="Остановлено досрочно", warnings="Предупреждения", position="Должность",
               company="Компания", place="Формат / локация", salary="Зарплата", exp="Опыт", date="Дата",
               link="Ссылка", fmt="Формат", location="Локация", english="Английский", employment="Занятость",
               category="Категория", posted="Опубликовано", notes="Заметки", no_desc="_описание не получено_",
               partial="только краткое описание"),
    "uk": dict(title="Вакансії", platform="Платформа", started="Пошук почато", finished="завершено",
               params="Параметри", found="Знайдено", requests="запитів до платформи",
               stopped="Зупинено достроково", warnings="Попередження", position="Посада",
               company="Компанія", place="Формат / локація", salary="Зарплата", exp="Досвід", date="Дата",
               link="Посилання", fmt="Формат", location="Локація", english="Англійська", employment="Зайнятість",
               category="Категорія", posted="Опубліковано", notes="Нотатки", no_desc="_опис не отримано_",
               partial="лише короткий опис"),
    "en": dict(title="Vacancies", platform="Platform", started="Search started", finished="finished",
               params="Parameters", found="Found", requests="requests to the platform",
               stopped="Stopped early", warnings="Warnings", position="Position",
               company="Company", place="Format / location", salary="Salary", exp="Experience", date="Date",
               link="Link", fmt="Format", location="Location", english="English", employment="Employment",
               category="Category", posted="Posted", notes="Notes", no_desc="_no description_",
               partial="summary only"),
}


def write_report(session: Path, platform: str, started: datetime, query: dict,
                 records: list[dict], log: RunLog, stopped: str | None,
                 requests: int, lang: str = "ru") -> dict:
    """Write `<YYYY-MM-DD_HH-MM-SS>_<platform>.{json,md,log}` into the session
    folder: timestamp = search start, platform = where it was searched."""
    stem = f"{started.strftime('%Y-%m-%d_%H-%M-%S')}_{platform}"
    incomplete = sum(1 for r in records if not r.get("description_complete"))
    if incomplete:
        log.warn(f"{incomplete} of {len(records)} vacancies have only a short description")
    meta = {
        "platform": platform,
        "searched_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "query": query,
        "count": len(records),
        "requests": requests,
        "stopped_early": stopped,
        "warnings": log.warnings,
    }
    (session / f"{stem}.json").write_text(
        json.dumps({**meta, "records": records}, ensure_ascii=False, indent=2), "utf-8")
    (session / f"{stem}.md").write_text(_markdown(meta, records, LABELS.get(lang, LABELS["en"])), "utf-8")
    (session / f"{stem}.log").write_text("\n".join(log.lines) + "\n", "utf-8")
    return {**meta, "files": [str(session / f"{stem}.{ext}") for ext in ("json", "md", "log")]}


def _markdown(meta: dict, records: list[dict], L: dict) -> str:
    q = ", ".join(f"{k}: {v}" for k, v in meta["query"].items() if v not in (None, [], "", False))
    out = [
        f"# {L['title']} — {meta['platform']} — {meta['searched_at'].replace('T', ' ')}",
        "",
        f"- {L['platform']}: **{meta['platform']}**",
        f"- {L['started']}: {meta['searched_at'].replace('T', ' ')}, {L['finished']}: {meta['finished_at'].replace('T', ' ')}",
        f"- {L['params']}: {q}",
        f"- {L['found']}: **{meta['count']}**, {L['requests']}: {meta['requests']}",
    ]
    if meta["stopped_early"]:
        out.append(f"- ⚠ {L['stopped']}: {meta['stopped_early']}")
    for w in meta["warnings"]:
        out.append(f"- ⚠ {L['warnings']}: {w}")
    out += ["", f"| # | {L['position']} | {L['company']} | {L['place']} | {L['salary']} | {L['exp']} | {L['date']} |",
            "|---|---|---|---|---|---|---|"]
    for i, r in enumerate(records, 1):
        place = " / ".join(x for x in (r.get("format"), r.get("location")) if x)
        out.append(f"| {i} | [{_cell(r['title'])}]({r['url']}) | {_cell(r.get('company'))} | {_cell(place)} | "
                   f"{_cell(r.get('salary_text'))} | {_cell(r.get('experience_text'))} | {r.get('posted_at') or ''} |")
    for i, r in enumerate(records, 1):
        out += ["", f"## {i}. {r['title']} — {r.get('company') or '?'}", "", f"- {L['link']}: {r['url']}"]
        for key, field in (("fmt", "format"), ("location", "location"), ("salary", "salary_text"),
                           ("exp", "experience_text"), ("english", "english"), ("employment", "employment"),
                           ("category", "category"), ("posted", "posted_at"), ("notes", "notes")):
            if r.get(field):
                out.append(f"- {L[key]}: {r[field]}")
        if not r.get("description_complete"):
            out.append(f"- ⚠ {L['partial']}")
        out += ["", r.get("description") or L["no_desc"]]
    return "\n".join(out) + "\n"


def _cell(v) -> str:
    return str(v or "").replace("|", "/").replace("\n", " ")
