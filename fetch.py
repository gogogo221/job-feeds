#!/usr/bin/env python3
"""Pull public job-board APIs, filter to target roles, and write data/jobs.json.

Runs nightly in GitHub Actions (stdlib only). The daily digest reads data/jobs.json
instead of searching boards by hand.

Board types: greenhouse, lever, ashby, workday, smartrecruiters, eightfold, uber.
Companies with no known board are auto-discovered by probing slug guesses on
Greenhouse/Lever/Ashby (re-probed weekly).
"""
import collections
import concurrent.futures as cf
import datetime as dt
import html
import json
import pathlib
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data"
NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date().isoformat()
UA = "Mozilla/5.0 (compatible; job-feeds/1.0; personal job digest)"
DISCOVERY_RECHECK_DAYS = 7
SEEN_TTL_DAYS = 45

# ---------------------------------------------------------------- HTTP

_host_locks: dict[str, threading.Lock] = {}
_host_last: dict[str, float] = {}
_locks_guard = threading.Lock()


def _polite(host: str, gap: float = 0.5):
    """At most one request per host every `gap` seconds."""
    with _locks_guard:
        lock = _host_locks.setdefault(host, threading.Lock())
    with lock:
        wait = _host_last.get(host, 0) + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        _host_last[host] = time.time()


def http(url, body=None, headers=None, timeout=40):
    """Return (status, parsed_json_or_None, error_str)."""
    h = {"User-Agent": UA, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    host = urllib.parse.urlsplit(url).hostname or ""
    for attempt in range(3):
        _polite(host)
        try:
            req = urllib.request.Request(url, data=data, headers=h, method="POST" if body is not None else "GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
                try:
                    return r.status, json.loads(raw), ""
                except json.JSONDecodeError:
                    return r.status, None, "non-JSON response"
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(4 * (attempt + 1))
                continue
            return e.code, None, f"HTTP {e.code}"
        except Exception as e:  # timeouts, DNS, TLS
            if attempt < 2:
                time.sleep(3)
                continue
            return 0, None, type(e).__name__ + ": " + str(e)[:120]
    return 0, None, "retries exhausted"


# ---------------------------------------------------------------- text helpers


def html_to_text(s: str) -> str:
    if not s:
        return ""
    s = html.unescape(s) if "&lt;" in s else s
    s = re.sub(r"(?i)<br\s*/?>|</(p|li|h\d|div|tr|ul|ol)>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "\n- ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln)


# ---------------------------------------------------------------- filters

ROLE = re.compile(r"\b(engineer|engineering|developer|swe|sde|programmer|technologist)\b", re.I)
SENIOR = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|leader|manager|management|director|head|vp|vice president|chief|"
    r"distinguished|fellow|architect|iv|v)\b",
    re.I,
)
JUNIOR = re.compile(
    r"\b(intern|internship|new grad|new graduate|graduate|university|campus|early[- ]career|entry[- ]level|"
    r"apprentice|co-?op|residency|phd|associate|summer 20\d\d|20(2[6-9]) start)\b",
    re.I,
)
OFFTRACK = re.compile(
    r"\b(front[- ]?end|ios|android|mobile|ui|ux|design|designer|sales|solutions?|customer|support|success|"
    r"forward[- ]deployed|field|deployment|recruit\w*|marketing|account|partner\w*|advocate|devrel|"
    r"developer relations|technical writer|writer|qa|quality assurance|test engineer|sdet|help ?desk|"
    r"hardware|mechanical|electrical|asic|fpga|rtl|analog|silicon|chip|verification|physical design|"
    r"manufacturing|firmware|embedded|it engineer|desktop|network technician|facilities|legal|finance|"
    r"accounting|payroll|tax|audit|compliance|gtm|go-to-market|growth|technical services|services|"
    r"techno-functional|functional|erp|oracle fusion|netsuite|salesforce developer|implementation|"
    r"professional services|consultant|onboarding)\b",
    re.I,
)

US_STATES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC "
    "ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()
US_RE = re.compile(
    r"(,\s*(" + "|".join(US_STATES) + r")\b)|\b(united states|usa|u\.s\.a?\.?|us|america|north america|"
    r"new york|nyc|brooklyn|san francisco|sf bay|bay area|south san francisco|oakland|berkeley|palo alto|"
    r"mountain view|sunnyvale|menlo park|san jose|santa clara|redwood city|san mateo|cupertino|seattle|"
    r"bellevue|redmond|kirkland|austin|dallas|houston|chicago|boston|cambridge, ma|los angeles|santa monica|"
    r"culver city|irvine|san diego|denver|boulder|atlanta|miami|washington|arlington|philadelphia|pittsburgh|"
    r"salt lake|portland|phoenix|minneapolis|detroit|raleigh|durham|nashville|columbus|jersey city|"
    r"hoboken|stamford|greenwich|princeton|alabama|alaska|arizona|california|colorado|connecticut|florida|"
    r"georgia|illinois|massachusetts|michigan|minnesota|new jersey|north carolina|ohio|oregon|"
    r"pennsylvania|texas|utah|virginia|wisconsin)\b",
    re.I,
)
NYC_RE = re.compile(r"\b(new york|nyc|brooklyn|manhattan|queens)\b|,\s*NY\b", re.I)
REMOTE_RE = re.compile(r"\b(remote|anywhere|distributed|work from home|wfh)\b", re.I)
NONUS_RE = re.compile(
    r"\b(canada|toronto|vancouver|montreal|ottawa|waterloo|london|uk|united kingdom|england|scotland|"
    r"ireland|dublin|germany|berlin|munich|hamburg|france|paris|netherlands|amsterdam|spain|madrid|"
    r"barcelona|poland|warsaw|krakow|india|bangalore|bengaluru|hyderabad|pune|delhi|gurgaon|mumbai|"
    r"chennai|singapore|japan|tokyo|australia|sydney|melbourne|israel|tel aviv|brazil|s[aã]o paulo|"
    r"mexico|switzerland|zurich|zug|geneva|sweden|stockholm|denmark|copenhagen|hong kong|china|shanghai|"
    r"beijing|shenzhen|korea|seoul|emea|apac|europe|latam|portugal|lisbon|italy|milan|czech|prague|"
    r"romania|serbia|belgrade|argentina|buenos aires|colombia|bogota|chile|philippines|manila|taiwan|"
    r"taipei|vietnam|uae|dubai|abu dhabi|south africa|new zealand|austria|vienna|belgium|brussels|"
    r"finland|helsinki|norway|oslo|estonia|tallinn|ukraine|kyiv|turkey|istanbul|greece|athens|hungary|"
    r"budapest|luxembourg|malaysia|indonesia|jakarta|thailand|bangkok|nigeria|lagos|kenya|nairobi|egypt|"
    r"cairo|pakistan|bulgaria|sofia|croatia|lithuania|latvia|slovakia|slovenia|cyprus|malta|costa rica|"
    r"peru|lima|uruguay|montevideo)\b",
    re.I,
)


def loc_class(s: str) -> str:
    """nyc | remote_us | us | nonus | unknown for one location string."""
    if not s:
        return "unknown"
    nonus = bool(NONUS_RE.search(s))
    us = bool(US_RE.search(s))
    if NYC_RE.search(s):
        return "nyc"
    if REMOTE_RE.search(s):
        if us or not nonus:
            return "remote_us" if us else "remote_unspecified"
        return "nonus"
    if us and not (nonus and not re.search(r"\b(united states|usa|us)\b", s, re.I)):
        return "us"
    if nonus:
        return "nonus"
    return "unknown"


def tier_of(locations, remote_flag=False, country=""):
    classes = {loc_class(l) for l in locations}
    if country:
        us_country = bool(re.search(r"^(us|usa|united states)", country.strip(), re.I))
        if us_country and remote_flag:
            classes.add("remote_us")
        elif us_country:
            classes.add("us")
    if "nyc" in classes:
        return "nyc"
    if "remote_us" in classes:
        return "remote_us"
    if "us" in classes:
        return "us_other"
    if "remote_unspecified" in classes and "nonus" not in classes:
        return "remote_unspecified"
    return None  # non-US or unknown -> drop


ROLE_HITS = re.compile(
    r"\b(backend|back-end|infrastructure|infra|platform|distributed|systems|storage|database|data platform|"
    r"data infrastructure|reliability|sre|low[- ]latency|trading|execution|exchange|core|runtime|compute|"
    r"inference|serving|networking|kernel|performance|api|payments|ledger|cloud|devops|production engineering|"
    r"developer productivity|observability|streaming)\b", re.I)
LANG_HITS = re.compile(r"(?<![\w+])(c\+\+|python|rust|go(?:lang)?|java|kotlin|scala|typescript)(?![\w+])", re.I)

WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
NUM = r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)"
YRS = re.compile(rf"\b{NUM}\s*\+?\s*(?:(?:-|–|—|to)\s*{NUM}\s*\+?\s*)?(?:years?|yrs?)\b", re.I)
EXP_CTX = re.compile(r"experience|exp\b|industry|professional|engineering|building|developing|working|background|track record", re.I)
PREF_LINE = re.compile(r"prefer|ideally|ideal |bonus|nice to have|nice-to-have|a plus|is a plus|desired|great if|stand out", re.I)
PREF_HDR = re.compile(r"prefer|nice to have|nice-to-have|bonus|plus|ideal|desired|stand out|extra credit|even better", re.I)
REQ_HDR = re.compile(r"require|minimum|basic|qualif|must|you have|what you|looking for|about you|you bring|who you are|you.ll need|you will need", re.I)


def _n(x):
    return WORDNUM.get(x.lower(), None) if not x.isdigit() else int(x)


def experience(text: str):
    """Return (req_min, pref_max, lines). Numbers > 15 are ignored (company age etc.)."""
    mode, req, pref, lines = "req", [], [], []
    for ln in text.splitlines():
        m = list(YRS.finditer(ln))
        if not m:
            if len(ln) < 80:
                low = ln.lower()
                if PREF_HDR.search(low) and not REQ_HDR.search(re.sub(r"prefer\w* qualifications?", "", low)):
                    mode = "pref"
                elif REQ_HDR.search(ln):
                    mode = "req"
            continue
        if not EXP_CTX.search(ln):
            continue
        nums = [n for g in m for n in (_n(g.group(1)), _n(g.group(2)) if g.group(2) else None) if n is not None and n <= 15]
        if not nums:
            continue
        lo = min(nums)
        is_pref = bool(PREF_LINE.search(ln)) or mode == "pref"
        (pref if is_pref else req).append(lo)
        if len(lines) < 3:
            lines.append(("[preferred] " if is_pref else "") + ln[:200])
    return (min(req) if req else None), (max(pref) if pref else None), lines


PAY = re.compile(
    r"\$\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d+)?\s?[kK])\s*(?:-|–|—|to)\s*\$?\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d+)?\s?[kK])"
)


def _money(s):
    s = s.replace(",", "").replace(" ", "")
    return int(float(s[:-1]) * 1000) if s[-1] in "kK" else int(s)


def pay_from_text(text):
    for m in PAY.finditer(text or ""):
        lo, hi = _money(m.group(1)), _money(m.group(2))
        if 40_000 <= lo <= hi <= 2_000_000:
            return f"${lo // 1000}k–${hi // 1000}k"
    return ""


# ---------------------------------------------------------------- board fetchers
# Each returns (jobs, total_count). A job is a dict with: id, title, url, locations,
# remote(bool), country, dept, posted, desc (plain text), pay.


def gh(b):
    t = b["token"]
    st, d, err = http(f"https://boards-api.greenhouse.io/v1/boards/{t}/jobs?content=true")
    if not d:
        raise RuntimeError(err or f"HTTP {st}")
    out = []
    for j in d.get("jobs", []):
        locs = [j.get("location", {}).get("name", "")]
        locs += [o.get("location") or o.get("name") or "" for o in j.get("offices", [])]
        locs = [p.strip() for l in locs for p in re.split(r";|\|| / ", l or "") if p.strip()]
        desc = html_to_text(j.get("content", ""))
        out.append(dict(
            id=str(j["id"]), title=j.get("title", ""), url=j.get("absolute_url", ""),
            locations=locs, remote=False, country="",
            dept=", ".join(x.get("name", "") for x in j.get("departments", [])),
            posted=(j.get("first_published") or j.get("updated_at") or "")[:10],
            desc=desc, pay=pay_from_text(desc)))
    return out, len(out)


def lever(b):
    t = b["token"]
    st, d, err = http(f"https://api.lever.co/v0/postings/{t}?mode=json")
    if d is None or not isinstance(d, list):
        raise RuntimeError(err or f"HTTP {st}")
    out = []
    for j in d:
        c = j.get("categories", {}) or {}
        locs = c.get("allLocations") or [c.get("location", "")]
        desc = "\n".join(filter(None, [
            j.get("descriptionPlain", ""),
            *[f"{x.get('text', '')}\n{html_to_text(x.get('content', ''))}" for x in j.get("lists", [])],
            j.get("additionalPlain", "")]))
        sr = j.get("salaryRange") or {}
        pay = ""
        if sr.get("min") and sr.get("max") and (sr.get("interval", "") or "").startswith("per-year"):
            pay = f"${int(sr['min']) // 1000}k–${int(sr['max']) // 1000}k"
        out.append(dict(
            id=j["id"], title=j.get("text", ""), url=j.get("hostedUrl", ""),
            locations=[l for l in locs if l], remote=(j.get("workplaceType") == "remote"),
            country=j.get("country", "") or "", dept=" / ".join(filter(None, [c.get("department"), c.get("team")])),
            posted=dt.datetime.fromtimestamp(j.get("createdAt", 0) / 1000, dt.timezone.utc).date().isoformat() if j.get("createdAt") else "",
            desc=desc, pay=pay or pay_from_text(desc)))
    return out, len(out)


def ashby(b):
    t = b["token"]
    st, d, err = http(f"https://api.ashbyhq.com/posting-api/job-board/{t}?includeCompensation=true")
    if not d or "jobs" not in d:
        raise RuntimeError(err or f"HTTP {st}")
    out = []
    for j in d["jobs"]:
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [x.get("location", "") for x in j.get("secondaryLocations", []) or []]
        country = (((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry") or "")
        comp = j.get("compensation") or {}
        desc = j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", ""))
        out.append(dict(
            id=j["id"], title=j.get("title", ""), url=j.get("jobUrl", ""),
            locations=[l for l in locs if l], remote=bool(j.get("isRemote")) or j.get("workplaceType") == "Remote",
            country=country, dept=" / ".join(filter(None, [j.get("department"), j.get("team")])),
            posted=(j.get("publishedAt") or "")[:10], desc=desc,
            pay=comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary") or pay_from_text(desc)))
    return out, len(out)


def workday(b):
    host, tenant, site = b["host"], b["tenant"], b["site"]
    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    posts, seen, total = [], set(), 0
    for q in b.get("queries", ["software engineer", "backend engineer", "infrastructure engineer"]):
        for offset in range(0, 200, 20):
            st, d, err = http(f"{base}/jobs", body={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": q})
            if not d:
                if not posts:
                    raise RuntimeError(err or f"HTTP {st}")
                break
            total = max(total, d.get("total", 0))
            batch = d.get("jobPostings", [])
            for p in batch:
                if p.get("externalPath") not in seen:
                    seen.add(p.get("externalPath"))
                    posts.append(p)
            if len(batch) < 20:
                break
    out = []
    details = 0
    for p in posts:
        title = p.get("title", "")
        if not title_ok(title)[0]:
            continue
        lt = p.get("locationsText", "")
        if not re.search(r"locations", lt, re.I) and tier_of([lt]) is None:
            continue
        if details >= b.get("max_details", 80):
            break
        details += 1
        st, d, err = http(f"{base}{p['externalPath']}")
        info = (d or {}).get("jobPostingInfo", {})
        locs = [info.get("location") or lt] + list(info.get("additionalLocations") or [])
        desc = html_to_text(info.get("jobDescription", ""))
        out.append(dict(
            id=info.get("jobReqId") or p["externalPath"], title=title,
            url=info.get("externalUrl") or f"https://{host}/{site}{p['externalPath']}",
            locations=locs, remote=bool(re.search("remote", " ".join(locs), re.I)), country=info.get("country", {}).get("descriptor", "") if isinstance(info.get("country"), dict) else "",
            dept="", posted=(info.get("startDate") or "")[:10], desc=desc, pay=pay_from_text(desc)))
    return out, total


def smartrecruiters(b):
    cid = b["token"]
    posts, offset = [], 0
    while offset < 1000:
        st, d, err = http(f"https://api.smartrecruiters.com/v1/companies/{cid}/postings?limit=100&offset={offset}&country=us")
        if not d:
            if not posts:
                raise RuntimeError(err or f"HTTP {st}")
            break
        posts += d.get("content", [])
        if len(d.get("content", [])) < 100:
            break
        offset += 100
    out = []
    for p in posts:
        if not title_ok(p.get("name", ""))[0]:
            continue
        L = p.get("location", {}) or {}
        loc = ", ".join(filter(None, [L.get("city"), L.get("region"), L.get("country")]))
        st, d, err = http(p.get("ref", ""))
        sec = ((d or {}).get("jobAd") or {}).get("sections") or {}
        desc = "\n".join(html_to_text((sec.get(k) or {}).get("text", "")) for k in ("jobDescription", "qualifications", "additionalInformation"))
        out.append(dict(
            id=p["id"], title=p.get("name", ""), url=(d or {}).get("postingUrl") or f"https://jobs.smartrecruiters.com/{cid}/{p['id']}",
            locations=[loc], remote=bool(L.get("remote")), country=L.get("country", ""),
            dept="", posted=(p.get("releasedDate") or "")[:10], desc=desc, pay=pay_from_text(desc)))
    return out, len(posts)


def eightfold(b):
    host, domain = b["host"], b["domain"]
    out, total, seen = [], 0, set()
    for loc in b.get("locations", ["New York", "United States"]):
        for start in range(0, 300, 10):
            q = urllib.parse.urlencode({"domain": domain, "start": start, "num": 10, "location": loc,
                                        "query": b.get("query", "software engineer"), "sort_by": "relevance"})
            st, d, err = http(f"https://{host}/api/apply/v2/jobs?{q}")
            if not d:
                if not out and start == 0 and loc == b.get("locations", ["New York"])[0]:
                    raise RuntimeError(err or f"HTTP {st}")
                break
            total = max(total, d.get("count", 0))
            ps = d.get("positions", [])
            for p in ps:
                if p.get("id") in seen:
                    continue
                seen.add(p.get("id"))
                if not title_ok(p.get("name", ""))[0]:
                    continue
                st2, dd, _ = http(f"https://{host}/api/apply/v2/jobs/{p['id']}?domain={domain}")
                desc = html_to_text((dd or {}).get("job_description", ""))
                out.append(dict(
                    id=str(p["id"]), title=p.get("name", ""),
                    url=p.get("canonicalPositionUrl") or f"https://{host}/careers/job/{p['id']}",
                    locations=p.get("locations") or [p.get("location", "")], remote=bool(p.get("work_location_option") == "remote"),
                    country="", dept=p.get("department", ""),
                    posted=dt.datetime.fromtimestamp(p["t_create"], dt.timezone.utc).date().isoformat() if p.get("t_create") else "",
                    desc=desc, pay=pay_from_text(desc)))
            if len(ps) < 10:
                break
    return out, total


def uber(b):
    url = "https://www.uber.com/api/loadSearchJobsResults?localeCode=en"
    out, total = [], 0
    for page in range(0, 10):
        body = {"params": {"location": [{"country": "USA", "region": "New York", "city": "New York"}],
                           "department": ["Engineering"]}, "page": page, "limit": 50}
        st, d, err = http(url, body=body, headers={"x-csrf-token": "x"})
        res = ((d or {}).get("data") or {}).get("results")
        if res is None:
            if page == 0:
                raise RuntimeError(err or f"HTTP {st}")
            break
        total = ((d.get("data") or {}).get("totalResults") or {}).get("low", total) or total
        for j in res:
            locs = [", ".join(filter(None, [l.get("city"), l.get("region"), l.get("countryName")])) for l in (j.get("allLocations") or [j.get("location") or {}])]
            desc = j.get("description", "")
            out.append(dict(
                id=str(j["id"]), title=j.get("title", ""), url=f"https://www.uber.com/global/en/careers/list/{j['id']}/",
                locations=locs, remote=False, country="USA", dept=j.get("team", ""),
                posted=(j.get("creationDate") or "")[:10], desc=desc, pay=pay_from_text(desc)))
        if len(res) < 50:
            break
    return out, total


FETCHERS = {"greenhouse": gh, "lever": lever, "ashby": ashby, "workday": workday,
            "smartrecruiters": smartrecruiters, "eightfold": eightfold, "uber": uber}


# ---------------------------------------------------------------- discovery

PROBES = {
    "greenhouse": lambda s: f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs",
    "lever": lambda s: f"https://api.lever.co/v0/postings/{s}?mode=json&limit=1",
    "ashby": lambda s: f"https://api.ashbyhq.com/posting-api/job-board/{s}",
}


def slugs(name, hints):
    base = re.sub(r"\(.*?\)", "", name).strip().lower()
    words = re.findall(r"[a-z0-9]+", base)
    cand = list(hints or []) + ["".join(words), "-".join(words)]
    seen, outl = set(), []
    for c in cand:
        if c and c not in seen:
            seen.add(c)
            outl.append(c)
    return outl[:5]


def _mentions(name, d):
    """True if the company name shows up in the board's postings (guards against same-slug other companies)."""
    words = [w for w in re.findall(r"[a-z0-9]+", re.sub(r"\(.*?\)", "", name).lower()) if len(w) > 2] or [name.lower()]
    alt = re.findall(r"\((.*?)\)", name.lower())
    jobs = d if isinstance(d, list) else d.get("jobs", [])
    blob = json.dumps(jobs[:15]).lower()
    return words[0] in blob or any(a in blob for a in alt)


def discover(name, hints):
    """Probe ATS APIs in slug order (hints first). First board with jobs that mention the company wins."""
    for s in slugs(name, hints):
        best = None
        for ats, mk in PROBES.items():
            url = mk(s).replace("&limit=1", "") if ats == "lever" else mk(s)
            if ats == "greenhouse":
                url += "?content=true"
            st, d, _ = http(url, timeout=30)
            if not d:
                continue
            n = len(d) if isinstance(d, list) else len(d.get("jobs", []))
            if n and _mentions(name, d) and (best is None or n > best["n"]):
                best = {"ats": ats, "token": s, "n": n}
        if best:
            return best
    return None


# ---------------------------------------------------------------- main


def clean_url(u):
    """Drop tracking params (utm_*, gh_src, ref, source, lever-*) but keep job ids like gh_jid."""
    p = urllib.parse.urlsplit(u)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query)
         if not re.match(r"(utm_|gh_src|ref$|source$|lever-|src$)", k, re.I)]
    return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/") or "/", urllib.parse.urlencode(q), ""))


def title_ok(title):
    if not ROLE.search(title):
        return False, "role"
    if SENIOR.search(title):
        return False, "senior"
    if JUNIOR.search(title):
        return False, "junior"
    if OFFTRACK.search(title):
        return False, "offtrack"
    if NONUS_RE.search(title) and not US_RE.search(title):
        return False, "nonus_title"
    return True, ""


def load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def main():
    DATA.mkdir(exist_ok=True)
    cfg = json.loads((ROOT / "companies.json").read_text())
    disc = load(DATA / "discovered.json", {})
    seen = load(DATA / "seen.json", {})

    # 1. resolve boards (configured + discovered)
    boards, to_probe, own_site = [], [], []
    for name, spec in cfg["companies"].items():
        if spec == "own_site":
            own_site.append(name)
            continue
        if isinstance(spec, dict) and spec.get("boards"):
            for b in spec["boards"]:
                boards.append({"company": name, **b})
            continue
        hints = spec.get("slugs", []) if isinstance(spec, dict) else []
        rec = disc.get(name)
        stale = not rec or (NOW.date() - dt.date.fromisoformat(rec["checked"])).days >= DISCOVERY_RECHECK_DAYS
        if rec and rec.get("ats") and not stale:
            boards.append({"company": name, "ats": rec["ats"], "token": rec["token"], "discovered": True})
        elif stale:
            to_probe.append((name, hints))
        else:
            own_site.append(name)  # probed recently, nothing found

    with cf.ThreadPoolExecutor(6) as ex:
        for (name, hints), res in zip(to_probe, ex.map(lambda nh: discover(*nh), to_probe)):
            disc[name] = {"checked": TODAY, **({"ats": res["ats"], "token": res["token"]} if res else {})}
            if res:
                boards.append({"company": name, "ats": res["ats"], "token": res["token"], "discovered": True})
            else:
                own_site.append(name)

    # 2. fetch all boards
    def run(b):
        t0 = time.time()
        try:
            jobs, total = FETCHERS[b["ats"]](b)
            return b, jobs, total, ""
        except Exception as e:
            return b, [], 0, str(e)[:200]

    results = []
    with cf.ThreadPoolExecutor(8) as ex:
        results = list(ex.map(run, boards))

    # 3. filter
    out_jobs, dropped_exp, board_status = [], [], []
    seen_urls = set()
    for b, jobs, total, err in results:
        matched = 0
        why_dropped = collections.Counter()
        for j in jobs:
            ok, why = title_ok(j["title"])
            if not ok:
                why_dropped["title_" + why] += 1
                continue
            tier = tier_of(j["locations"], j.get("remote", False), j.get("country", ""))
            if tier is None:
                why_dropped["location"] += 1
                continue
            req_min, pref_max, yoe_lines = experience(j["desc"])
            if (req_min is not None and req_min >= 3) or (pref_max is not None and pref_max >= 4):
                why_dropped["experience"] += 1
                dropped_exp.append({"company": b["company"], "title": j["title"], "req_min": req_min, "pref_max": pref_max, "url": j["url"]})
                continue
            url = clean_url(j["url"])
            ident = (b["company"], str(j["id"]))
            if ident in seen_urls or url in seen_urls:
                continue
            seen_urls.update([ident, url])
            key = f"{b['ats']}:{b.get('token') or b.get('tenant') or b.get('domain') or 'x'}:{j['id']}"
            rec = seen.setdefault(key, {"first_seen": TODAY})
            rec["last_seen"] = TODAY
            matched += 1
            out_jobs.append({
                "company": b["company"], "title": j["title"], "url": url, "ats": b["ats"],
                "tier": tier, "locations": j["locations"][:6], "remote": j.get("remote", False),
                "dept": j.get("dept", ""), "posted": j.get("posted", ""), "first_seen": rec["first_seen"],
                "pay": j.get("pay", ""), "yoe_min": req_min, "yoe_pref": pref_max, "yoe_text": yoe_lines,
                "role_hits": sorted(set(m.lower() for m in ROLE_HITS.findall(j["title"] + " " + j.get("dept", "")))),
                "lang_hits": sorted(set(m.lower() for m in LANG_HITS.findall(j["desc"])))[:6],
            })
        board_status.append({"company": b["company"], "ats": b["ats"], "token": b.get("token") or b.get("tenant") or b.get("domain") or "",
                             "discovered": bool(b.get("discovered")), "status": "failed" if err else "ok",
                             "error": err, "total": total, "matched": matched, "dropped": dict(why_dropped)})

    # 4. prune seen + write
    cutoff = (NOW.date() - dt.timedelta(days=SEEN_TTL_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if v.get("last_seen", "") >= cutoff}
    out_jobs.sort(key=lambda x: (x["first_seen"], x["company"]), reverse=True)
    summary = {
        "generated_at": NOW.isoformat(timespec="seconds"),
        "counts": {"boards": len(boards), "boards_ok": sum(s["status"] == "ok" for s in board_status),
                   "boards_failed": sum(s["status"] == "failed" for s in board_status),
                   "jobs": len(out_jobs), "new_today": sum(j["first_seen"] == TODAY for j in out_jobs),
                   "dropped_experience": len(dropped_exp)},
        "not_covered": sorted(own_site),
        "boards": sorted(board_status, key=lambda s: (s["status"], s["company"])),
        "jobs": out_jobs,
        "dropped_experience": dropped_exp[:300],
    }
    (DATA / "jobs.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    (DATA / "seen.json").write_text(json.dumps(seen, indent=0, sort_keys=True))
    (DATA / "discovered.json").write_text(json.dumps(disc, indent=1, sort_keys=True))
    c = summary["counts"]
    print(f"boards {c['boards_ok']}/{c['boards']} ok · {c['jobs']} jobs ({c['new_today']} new) · "
          f"{c['dropped_experience']} dropped for experience · not covered: {len(own_site)}")
    for s in board_status:
        if s["status"] == "failed":
            print(f"  FAILED {s['company']} [{s['ats']}:{s['token']}] {s['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
