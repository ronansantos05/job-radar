"""Coletores de vagas remotas. Só fontes com API/RSS público (nada de scraping de LinkedIn)."""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

import httpx

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "job-radar/1.0"}
TIMEOUT = 30
MAX_DESCRIPTION = 12000

_BLOCK_TAGS = {"p", "br", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "tr"}


class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    parser = _Stripper()
    parser.feed(str(text))
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    out, blank = [], False
    for line in lines:
        if line:
            out.append(line)
            blank = False
        elif not blank and out:
            out.append("")
            blank = True
    return "\n".join(out).strip()[:MAX_DESCRIPTION]


def parse_date(value) -> str | None:
    """Aceita epoch, ISO 8601 ou RFC 822 e devolve ISO em UTC."""
    if value in (None, ""):
        return None
    try:
        text = str(value).strip()
        if text.isdigit():
            dt = datetime.fromtimestamp(int(text), timezone.utc)
        else:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return None


def _as_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _salary(lo, hi, currency="", period="") -> str:
    try:
        lo, hi = int(float(lo or 0)), int(float(hi or 0))
    except (TypeError, ValueError):
        return ""
    if not lo and not hi:
        return ""
    rng = f"{lo:,}–{hi:,}" if lo and hi and lo != hi else f"{max(lo, hi):,}"
    return " ".join(x for x in (currency, rng, f"/{period}" if period else "") if x)


def _job(source, ext_id, title, company, url, location="", tags=None, description="", salary="", posted=None):
    return {
        "source": source,
        "ext_id": str(ext_id),
        "title": " ".join(str(title or "").split()),
        "company": " ".join(str(company or "").split()),
        "url": str(url or ""),
        "location": str(location or ""),
        "tags": _as_list(tags)[:15],
        "description": strip_html(description),
        "salary": str(salary or ""),
        "posted_at": parse_date(posted),
    }


def _get(url: str, **params) -> httpx.Response:
    r = httpx.get(url, params=params or None, headers=HEADERS, timeout=TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------- parsers (testáveis sem rede)

def parse_remotive(data: dict) -> list[dict]:
    return [
        _job("remotive", j["id"], j.get("title"), j.get("company_name"), j.get("url"),
             j.get("candidate_required_location"), j.get("tags"), j.get("description"),
             j.get("salary"), j.get("publication_date"))
        for j in data.get("jobs", [])
    ]


def parse_remoteok(data: list) -> list[dict]:
    out = []
    for j in data:
        if not isinstance(j, dict) or "position" not in j:
            continue  # o primeiro item é um aviso legal, não uma vaga
        out.append(_job("remoteok", j["id"], j.get("position"), j.get("company"), j.get("url"),
                        j.get("location") or "Remote", j.get("tags"), j.get("description"),
                        _salary(j.get("salary_min"), j.get("salary_max"), "USD", "ano"), j.get("date")))
    return out


def parse_himalayas(data: dict) -> list[dict]:
    out = []
    for j in data.get("jobs", []):
        where = ", ".join(_as_list(j.get("locationRestrictions"))) or "Worldwide"
        tags = _as_list(j.get("categories")) + _as_list(j.get("seniority")) + _as_list(j.get("employmentType"))
        out.append(_job("himalayas", j.get("guid") or j.get("applicationLink"), j.get("title"),
                        j.get("companyName"), j.get("applicationLink") or j.get("guid"), where, tags,
                        j.get("description"),
                        _salary(j.get("minSalary"), j.get("maxSalary"), j.get("currency") or "",
                                j.get("salaryPeriod") or ""),
                        j.get("pubDate")))
    return out


def parse_jobicy(data: dict) -> list[dict]:
    return [
        _job("jobicy", j["id"], j.get("jobTitle"), j.get("companyName"), j.get("url"), j.get("jobGeo"),
             _as_list(j.get("jobIndustry")) + _as_list(j.get("jobType")) + _as_list(j.get("jobLevel")),
             j.get("jobDescription") or j.get("jobExcerpt"),
             _salary(j.get("salaryMin"), j.get("salaryMax"), j.get("salaryCurrency") or "",
                     j.get("salaryPeriod") or ""),
             j.get("pubDate"))
        for j in data.get("jobs", [])
    ]


def parse_wwr(xml_text: str) -> list[dict]:
    out = []
    for item in ET.fromstring(xml_text).iter("item"):
        get = lambda tag: (item.findtext(tag) or "").strip()  # noqa: E731
        company, _, title = get("title").partition(": ")
        if not title:
            company, title = "", get("title")
        out.append(_job("weworkremotely", get("guid") or get("link"), title, company, get("link"),
                        get("region"), [c.text for c in item.findall("category") if c.text],
                        get("description"), "", get("pubDate")))
    return out


# ---------------------------------------------------------------- coletores

def fetch_remotive() -> list[dict]:
    return parse_remotive(_get("https://remotive.com/api/remote-jobs").json())


def fetch_remoteok() -> list[dict]:
    return parse_remoteok(_get("https://remoteok.com/api").json())


def fetch_himalayas(pages: int = 5) -> list[dict]:
    out = []
    for page in range(pages):
        batch = parse_himalayas(_get("https://himalayas.app/jobs/api", limit=20, offset=page * 20).json())
        out += batch
        if len(batch) < 20:
            break
    return out


def fetch_jobicy() -> list[dict]:
    seen: dict[str, dict] = {}
    for tag in ("python", "machine learning", "data science"):
        try:
            for job in parse_jobicy(_get("https://jobicy.com/api/v2/remote-jobs", count=50, tag=tag).json()):
                seen[job["ext_id"]] = job
        except httpx.HTTPError as e:
            log.warning("jobicy (%s): %s", tag, e)
    return list(seen.values())


def fetch_wwr() -> list[dict]:
    seen: dict[str, dict] = {}
    for cat in ("remote-programming-jobs", "remote-back-end-programming-jobs",
                "remote-full-stack-programming-jobs"):
        for job in parse_wwr(_get(f"https://weworkremotely.com/categories/{cat}.rss").text):
            seen[job["ext_id"]] = job
    return list(seen.values())


SOURCES = {
    "remotive": fetch_remotive,
    "remoteok": fetch_remoteok,
    "himalayas": fetch_himalayas,
    "jobicy": fetch_jobicy,
    "weworkremotely": fetch_wwr,
}
