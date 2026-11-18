"""Fetchers for each hiring system.

Every fetcher takes a source dict from firms.yaml plus a list of search terms
and returns a list of Job objects. Any network or format problem raises
SourceError so the health checks can notice a broken source.

describe() returns a posting's full description text, used to check which
class years can apply.
"""
import html
import json
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
TIMEOUT = 25
MAX_RESULTS_PER_QUERY = 300  # safety cap so a fuzzy search can't page forever


@dataclass
class Job:
    key: str          # stable unique id, e.g. "wd:blackrock:R12345"
    firm: str
    title: str
    url: str
    location: str = ""
    posted: str = ""
    extra: str = ""   # extra text used only for matching (e.g. GS division)
    description: str = ""  # filled in when the listing already includes it
    detail: str = ""       # API URL for the full description, when the system has one


class SourceError(Exception):
    pass


def _text(markup):
    soup = BeautifulSoup(markup or "", "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/html;q=0.9, */*;q=0.8"})
    return s


def _request(s, method, url, **kw):
    for attempt in range(3):
        try:
            r = s.request(method, url, timeout=TIMEOUT, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if attempt == 2 or (e.response is not None and e.response.status_code in (401, 403, 404)):
                raise SourceError(f"{type(e).__name__}: {e}"[:200]) from None
            time.sleep(2 * (attempt + 1))


def _json(r):
    try:
        return r.json()
    except ValueError:
        raise SourceError("response was not JSON (site format may have changed)") from None


class _Throttle:
    """Workday limits how many requests it accepts from GitHub's shared servers, so all
    Workday requests go one at a time, spaced out. A 429 ("too many requests") pauses
    every Workday request for a while; if Workday keeps refusing, the rest of this run's
    Workday checks fail fast instead of waiting forever."""

    def __init__(self, interval, pauses=(30, 60, 120)):
        self.lock = threading.Lock()
        self.interval = interval
        self.pauses = pauses
        self.next_ok = 0.0
        self.blocked = False

    def request(self, s, method, url, **kw):
        for attempt in range(len(self.pauses) + 1):
            with self.lock:  # held while pausing, so a 429 pauses every Workday check at once
                if self.blocked:
                    raise SourceError("HTTP 429: Workday is refusing requests from this server right now")
                time.sleep(max(0.0, self.next_ok - time.time()))
                started = time.time()  # space requests by start time, so slow responses don't add up
                try:
                    r = s.request(method, url, timeout=TIMEOUT, **kw)
                except requests.RequestException as e:
                    self.next_ok = started + self.interval
                    if attempt == len(self.pauses):
                        raise SourceError(f"{type(e).__name__}: {e}"[:200]) from None
                    continue
                if r.status_code != 429:
                    self.next_ok = started + self.interval
                    break
                if attempt == len(self.pauses):
                    self.blocked = True
                    raise SourceError("HTTP 429: Workday is refusing requests from this server right now")
                retry_after = r.headers.get("Retry-After", "")
                pause = min(max(self.pauses[attempt], int(retry_after) if retry_after.isdigit() else 0), 120)
                print(f"Workday said too many requests; pausing all Workday checks {pause}s ({url.split('/')[2]})", flush=True)
                self.next_ok = time.time() + pause
        if r.status_code >= 400:
            raise SourceError(f"HTTP {r.status_code} from Workday")
        return r


WORKDAY = _Throttle(interval=1.5)


# --- Workday (most asset managers and many banks) ---------------------------

def workday(src, terms):
    t, wd, site = src["tenant"], src["wd"], src["site"]
    base = f"https://{t}.wd{wd}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{t}/{site}/jobs"
    s = _session()
    jobs = {}
    # Year searches ("2027") never found anything extra on Workday, so skip them to stay under its limit
    for q in [t for t in terms if not t.isdigit()] or terms:
        offset, total = 0, None
        while True:
            body = {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": q}
            data = _json(WORKDAY.request(s, "POST", api, json=body))
            if "jobPostings" not in data:
                raise SourceError("unexpected Workday response")
            if total is None:  # Workday only reports the total on the first page
                total = data.get("total") or 0
            posts = data["jobPostings"]
            for p in posts:
                path = p.get("externalPath")
                if not path:
                    continue
                rid = (p.get("bulletFields") or [path])[0]
                key = f"wd:{t}:{rid}"
                jobs[key] = Job(key, src["name"], p.get("title", "").strip(), f"{base}/{site}{path}",
                                p.get("locationsText", ""), p.get("postedOn", ""),
                                detail=f"{base}/wday/cxs/{t}/{site}{path}")
            offset += 20
            if not posts or offset >= min(total, MAX_RESULTS_PER_QUERY):
                break
    return list(jobs.values())


# --- Oracle Cloud HCM (JPMorgan, BNY, Lazard) -------------------------------

def oracle(src, terms):
    host, site = src["host"], src["site"]
    api = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    s = _session()
    jobs = {}
    for q in terms:
        offset, total = 0, None
        while True:
            finder = (f"findReqs;siteNumber={site},limit=25,offset={offset},"
                      f"keyword=%22{quote(q)}%22,sortBy=POSTING_DATES_DESC")
            url = f"{api}?onlyData=true&expand=requisitionList.secondaryLocations&finder={finder}"
            data = _json(_request(s, "GET", url))
            try:
                item = data["items"][0]
            except (KeyError, IndexError):
                raise SourceError("unexpected Oracle response") from None
            if total is None:
                total = item.get("TotalJobsCount") or 0
            reqs = item.get("requisitionList") or []
            for x in reqs:
                key = f"oracle:{host}:{x['Id']}"
                locs = [x.get("PrimaryLocation") or ""] + [l.get("Name", "") for l in x.get("secondaryLocations") or []]
                jobs[key] = Job(key, src["name"], x.get("Title", "").strip(),
                                f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{x['Id']}",
                                "; ".join(l for l in locs if l), x.get("PostedDate") or "",
                                detail=(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
                                        f"?expand=all&onlyData=true&finder=ById;Id=%22{x['Id']}%22,siteNumber={site}"))
            offset += 25
            if not reqs or offset >= min(total, MAX_RESULTS_PER_QUERY):
                break
            time.sleep(0.3)
    return list(jobs.values())


# --- Greenhouse / Lever / Ashby (hedge funds, boutiques) --------------------

def greenhouse(src, terms):
    board = src["board"]
    data = _json(_request(_session(), "GET", f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"))
    return [Job(f"gh:{board}:{j['id']}", src["name"], j["title"].strip(), j["absolute_url"],
                (j.get("location") or {}).get("name", ""), (j.get("first_published") or j.get("updated_at") or "")[:10],
                description=_text(html.unescape(j.get("content") or "")))
            for j in data.get("jobs", [])]


def lever(src, terms):
    co = src["company"]
    data = _json(_request(_session(), "GET", f"https://api.lever.co/v0/postings/{co}?mode=json"))
    return [Job(f"lever:{co}:{j['id']}", src["name"], j["text"].strip(), j["hostedUrl"],
                (j.get("categories") or {}).get("location", ""),
                time.strftime("%Y-%m-%d", time.gmtime(j["createdAt"] / 1000)) if j.get("createdAt") else "",
                description=" ".join([j.get("descriptionPlain") or "", j.get("additionalPlain") or ""]
                                     + [_text(l.get("content")) for l in j.get("lists") or []]))
            for j in data]


def ashby(src, terms):
    board = src["board"]
    data = _json(_request(_session(), "GET", f"https://api.ashbyhq.com/posting-api/job-board/{board}"))
    return [Job(f"ashby:{board}:{j['id']}", src["name"], j["title"].strip(), j["jobUrl"],
                j.get("location", ""), (j.get("publishedAt") or "")[:10], description=j.get("descriptionPlain") or "")
            for j in data.get("jobs", [])]


# --- Eightfold (Millennium) -------------------------------------------------

def eightfold(src, terms):
    host, domain = src["host"], src["domain"]
    s = _session()
    jobs = {}
    for q in terms:
        start = 0
        while True:
            url = f"https://{host}/api/apply/v2/jobs?domain={domain}&query={quote(q)}&start={start}&num=10"
            data = _json(_request(s, "GET", url))
            if "positions" not in data:
                raise SourceError("unexpected Eightfold response")
            for p in data["positions"]:
                key = f"ef:{domain}:{p['id']}"
                jobs[key] = Job(key, src["name"], p.get("name", "").strip(),
                                p.get("canonicalPositionUrl") or f"https://{host}/careers?pid={p['id']}&domain={domain}",
                                p.get("location", ""),
                                time.strftime("%Y-%m-%d", time.gmtime(p["t_create"])) if p.get("t_create") else "",
                                description=_text(p.get("job_description")))
            start += 10
            if not data["positions"] or start >= min(data.get("count", 0), MAX_RESULTS_PER_QUERY):
                break
            time.sleep(0.3)
    return list(jobs.values())


# --- Oleeo / tal.net campus boards (Jefferies, Evercore, Nomura, BofA) ------

def oleeo(src, terms):
    s = _session()
    jobs = {}
    for board in src["boards"]:
        url, pages = board, 0
        while url and pages < 10:
            r = _request(s, "GET", url)
            if "tal.net" not in r.url:
                raise SourceError(f"board redirected away to {r.url[:80]}")
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                m = re.search(r"/opp/(\d+)-", a["href"])
                if m:
                    key = f"tal:{src['name']}:{m.group(1)}"
                    jobs[key] = Job(key, src["name"], a.get_text(" ", strip=True), urljoin(url, a["href"]))
            nxt = soup.find("a", string=re.compile(r"next page", re.I), href=True)
            url = urljoin(url, nxt["href"]) if nxt else None
            pages += 1
    return list(jobs.values())


# --- iCIMS career sites (TCW, Marathon, Stifel) -----------------------------

def icims(src, terms):
    base = src["base"].rstrip("/")
    s = _session()
    jobs = {}
    for page in range(20):
        r = _request(s, "GET", f"{base}/jobs/search?ss=1&in_iframe=1&pr={page}")
        soup = BeautifulSoup(r.text, "html.parser")
        found = 0
        for a in soup.find_all("a", href=True):
            m = re.search(r"/jobs/(\d+)/", a["href"])
            if not m:
                continue
            title = re.sub(r"^(Job\s+)?Title\s+", "", a.get_text(" ", strip=True))
            key = f"icims:{base}:{m.group(1)}"
            if key not in jobs:
                found += 1
            jobs[key] = Job(key, src["name"], title, urljoin(base, a["href"].split("?")[0]))
        pages = re.search(r"Page\s+\d+\s+of\s+(\d+)", r.text)
        if not found or not pages or page + 1 >= int(pages.group(1)):
            break
        time.sleep(0.3)
    if not jobs and "iCIMS" not in r.text and "icims" not in r.text:
        raise SourceError("page doesn't look like an iCIMS job board anymore")
    return list(jobs.values())


# --- Phenom career sites (Franklin Templeton) -------------------------------

def phenom(src, terms):
    # Some Phenom sites ignore the search keyword; for those (the default) walk the full list once.
    # Big sites that do honor it get keyword_search: true in firms.yaml.
    base = src["base"].rstrip("/")
    s = _session()
    jobs = {}
    for q in (terms if src.get("keyword_search") else [None]):
        frm = 0
        while True:
            kw = f"keywords={quote(q)}&" if q else ""
            r = _request(s, "GET", f"{base}/search-results?{kw}from={frm}&s=1")
            m = re.search(r"phApp\.ddo\s*=\s*(\{.*?\});\s*phApp\.", r.text, re.S)
            if not m:
                raise SourceError("could not find job data on Phenom page")
            res = json.loads(m.group(1)).get("eagerLoadRefineSearch") or {}
            found = (res.get("data") or {}).get("jobs") or []
            for j in found:
                key = f"phenom:{base}:{j['jobId']}"
                jobs[key] = Job(key, src["name"], j.get("title", "").strip(), f"{base}/job/{j['jobId']}",
                                j.get("location") or j.get("cityStateCountry", ""), (j.get("postedDate") or "")[:10])
            frm += len(found)
            if not found or frm >= min(res.get("totalHits") or 0, MAX_RESULTS_PER_QUERY if q else 1000):
                break
            time.sleep(0.2)
    return list(jobs.values())


# --- SmartRecruiters (NBCUniversal, ServiceNow) -----------------------------

def smartrecruiters(src, terms):
    co = src["company"]
    s = _session()
    jobs = {}
    for q in terms:
        offset = 0
        while True:
            data = _json(_request(s, "GET", f"https://api.smartrecruiters.com/v1/companies/{co}/postings"
                                            f"?q={quote(q)}&limit=100&offset={offset}"))
            if "content" not in data:
                raise SourceError("unexpected SmartRecruiters response")
            for p in data["content"]:
                loc = p.get("location") or {}
                where = loc.get("fullLocation") or ", ".join(v for v in (loc.get("city"), loc.get("region"), loc.get("country")) if v)
                key = f"sr:{co}:{p['id']}"
                jobs[key] = Job(key, src["name"], p.get("name", "").strip(), f"https://jobs.smartrecruiters.com/{co}/{p['id']}",
                                where, (p.get("releasedDate") or "")[:10], detail=p.get("ref") or "")
            offset += 100
            if not data["content"] or offset >= min(data.get("totalFound") or 0, MAX_RESULTS_PER_QUERY):
                break
            time.sleep(0.3)
    return list(jobs.values())


# --- Amazon (its own job search) ---------------------------------------------

def amazon(src, terms):
    s = _session()
    jobs = {}
    for loc in src["locations"]:
        for q in terms:
            offset = 0
            while True:
                data = _json(_request(s, "GET", f"https://www.amazon.jobs/en/search.json?base_query={quote(q)}"
                                                f"&loc_query={quote(loc)}&result_limit=100&offset={offset}&sort=recent"))
                if "jobs" not in data:
                    raise SourceError("unexpected Amazon response")
                for j in data["jobs"]:
                    key = f"amzn:{j.get('id_icims') or j.get('job_path')}"
                    jobs[key] = Job(key, src["name"], j.get("title", "").strip(), "https://www.amazon.jobs" + j.get("job_path", ""),
                                    j.get("normalized_location") or j.get("location", ""), j.get("posted_date", ""),
                                    description=_text(" ".join(j.get(k) or "" for k in
                                                               ("description", "basic_qualifications", "preferred_qualifications"))))
                offset += 100
                if not data["jobs"] or offset >= min(data.get("hits") or 0, MAX_RESULTS_PER_QUERY):
                    break
                time.sleep(0.3)
    return list(jobs.values())


# --- Goldman Sachs (custom GraphQL API behind higher.gs.com) ----------------

GS_QUERY = """query GetCampusRoles($searchQueryInput: RoleSearchQueryInput!) {
  roleSearch(searchQueryInput: $searchQueryInput) {
    totalCount
    items { roleId corporateTitle jobTitle division status startDate
            locations { primary state country city } externalSource { sourceId } }
  }
}"""


def goldman(src, terms):
    s = _session()
    s.headers.update({"Origin": "https://higher.gs.com", "Referer": "https://higher.gs.com/"})
    jobs, page, total = {}, 0, None
    while True:
        body = {"operationName": "GetCampusRoles", "query": GS_QUERY, "variables": {"searchQueryInput": {
            "page": {"pageSize": 20, "pageNumber": page},
            "sort": {"sortStrategy": "POSTED_DATE", "sortOrder": "DESC"},
            "filters": [], "experiences": ["CAMPUS"], "searchTerm": ""}}}
        data = _json(_request(s, "POST", "https://api-higher.gs.com/gateway/api/v1/graphql", json=body))
        try:
            rs = data["data"]["roleSearch"]
        except (KeyError, TypeError):
            raise SourceError(f"unexpected GS response: {str(data)[:120]}") from None
        if total is None:
            total = rs.get("totalCount") or 0
        for x in rs["items"]:
            sid = (x.get("externalSource") or {}).get("sourceId") or x["roleId"].split("_")[0]
            loc = next((l for l in x.get("locations") or [] if l.get("primary")), (x.get("locations") or [{}])[0])
            where = ", ".join(v for v in (loc.get("city"), loc.get("state"), loc.get("country")) if v)
            key = f"gs:{x['roleId']}"
            jobs[key] = Job(key, src["name"], x.get("jobTitle", "").strip(), f"https://higher.gs.com/roles/{sid}",
                            where, (x.get("startDate") or "")[:10],
                            extra=f"{x.get('division') or ''} {x.get('corporateTitle') or ''}")
        page += 1
        if not rs["items"] or page * 20 >= min(total, 1000):
            break
        time.sleep(0.3)
    return list(jobs.values())


# --- Generic page watcher (any page without a feed) -------------------------

def page(src, terms):
    r = _request(_session(), "GET", src["url"])
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer"]):
        tag.decompose()
    if len(soup.get_text(" ", strip=True)) < 200:
        raise SourceError("page came back nearly empty (probably needs JavaScript)")
    jobs = {}
    for el in soup.find_all(["a", "h1", "h2", "h3", "h4", "li"]):
        text = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        if not 8 <= len(text) <= 200:
            continue
        href = el.get("href") if el.name == "a" else None
        key = f"page:{src['name']}:{text.lower()}"
        jobs[key] = Job(key, src["name"], text, urljoin(src["url"], href) if href else src["url"])
    return list(jobs.values())


# --- Full description for one posting --------------------------------------

def describe(src, job):
    """Full description text of a posting, or "" if this system doesn't expose one.

    Oleeo (tal.net) detail pages sit behind a "are you a real person" check, so
    they're never fetched; those postings are judged on their title alone.
    """
    if job.description:
        return job.description
    t = src["type"]
    s = _session()
    if t == "workday" and job.detail:
        info = _json(WORKDAY.request(s, "GET", job.detail)).get("jobPostingInfo") or {}
        return _text(info.get("jobDescription"))
    if t == "oracle" and job.detail:
        items = _json(_request(s, "GET", job.detail)).get("items") or [{}]
        return _text(" ".join(str(items[0].get(k) or "") for k in
                              ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")))
    if t == "goldman":
        page_html = _request(s, "GET", job.url).text
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page_html, re.S)
        return _text(json.dumps(json.loads(m.group(1)), ensure_ascii=False)) if m else ""
    if t == "icims":
        return _text(_request(s, "GET", job.url + "?in_iframe=1").text)
    if t == "phenom":
        m = re.search(r'"description"\s*:\s*"((?:[^"\\]|\\.)*)"', _request(s, "GET", job.url).text)
        return _text(html.unescape(json.loads(f'"{m.group(1)}"'))) if m else ""
    if t == "smartrecruiters" and job.detail:
        sections = (_json(_request(s, "GET", job.detail)).get("jobAd") or {}).get("sections") or {}
        return _text(" ".join((v or {}).get("text") or "" for v in sections.values()))
    return ""


FETCHERS = {
    "workday": workday, "oracle": oracle, "greenhouse": greenhouse, "lever": lever, "ashby": ashby,
    "eightfold": eightfold, "oleeo": oleeo, "icims": icims, "phenom": phenom, "goldman": goldman, "page": page,
    "smartrecruiters": smartrecruiters, "amazon": amazon,
}
