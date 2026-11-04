"""Fetchers for each hiring system.

Every fetcher takes a source dict from firms.yaml plus a list of search terms
and returns a list of Job objects. Any network or format problem raises
SourceError so the health checks can notice a broken source.
"""
import json
import re
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


class SourceError(Exception):
    pass


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


# --- Workday (most asset managers and many banks) ---------------------------

def workday(src, terms):
    t, wd, site = src["tenant"], src["wd"], src["site"]
    base = f"https://{t}.wd{wd}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{t}/{site}/jobs"
    s = _session()
    jobs = {}
    for q in terms:
        offset, total = 0, None
        while True:
            body = {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": q}
            data = _json(_request(s, "POST", api, json=body))
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
                                p.get("locationsText", ""), p.get("postedOn", ""))
            offset += 20
            if not posts or offset >= min(total, MAX_RESULTS_PER_QUERY):
                break
            time.sleep(0.3)
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
                                "; ".join(l for l in locs if l), x.get("PostedDate") or "")
            offset += 25
            if not reqs or offset >= min(total, MAX_RESULTS_PER_QUERY):
                break
            time.sleep(0.3)
    return list(jobs.values())


# --- Greenhouse / Lever / Ashby (hedge funds, boutiques) --------------------

def greenhouse(src, terms):
    board = src["board"]
    data = _json(_request(_session(), "GET", f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs"))
    return [Job(f"gh:{board}:{j['id']}", src["name"], j["title"].strip(), j["absolute_url"],
                (j.get("location") or {}).get("name", ""), (j.get("first_published") or j.get("updated_at") or "")[:10])
            for j in data.get("jobs", [])]


def lever(src, terms):
    co = src["company"]
    data = _json(_request(_session(), "GET", f"https://api.lever.co/v0/postings/{co}?mode=json"))
    return [Job(f"lever:{co}:{j['id']}", src["name"], j["text"].strip(), j["hostedUrl"],
                (j.get("categories") or {}).get("location", ""),
                time.strftime("%Y-%m-%d", time.gmtime(j["createdAt"] / 1000)) if j.get("createdAt") else "")
            for j in data]


def ashby(src, terms):
    board = src["board"]
    data = _json(_request(_session(), "GET", f"https://api.ashbyhq.com/posting-api/job-board/{board}"))
    return [Job(f"ashby:{board}:{j['id']}", src["name"], j["title"].strip(), j["jobUrl"],
                j.get("location", ""), (j.get("publishedAt") or "")[:10])
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
                                time.strftime("%Y-%m-%d", time.gmtime(p["t_create"])) if p.get("t_create") else "")
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
    # Phenom's server-rendered page ignores the keyword, so walk the full list once.
    base = src["base"].rstrip("/")
    s = _session()
    jobs, frm = {}, 0
    while True:
        r = _request(s, "GET", f"{base}/search-results?from={frm}&s=1")
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
        if not found or frm >= min(res.get("totalHits") or 0, 1000):
            break
        time.sleep(0.2)
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


FETCHERS = {
    "workday": workday, "oracle": oracle, "greenhouse": greenhouse, "lever": lever, "ashby": ashby,
    "eightfold": eightfold, "oleeo": oleeo, "icims": icims, "phenom": phenom, "goldman": goldman, "page": page,
}
