"""Broad, pay-filtered coverage beyond the curated company list.

Two jobs:
1. Grow a registry of extra job boards (data/broad.json) from:
   - Hacker News "Who is hiring?" threads: ATS links in NYC / US-remote posts.
   - YC companies that are hiring in NYC or remote (yc-oss API): careers-page scan for an
     ATS link, then slug probes on Greenhouse / Ashby / Lever.
2. Emit HN "Who is hiring?" posts that state a salary as jobs of their own.

Jobs from broad boards and HN are only kept by fetch.py when they POST a salary whose top
end clears PAY_FLOOR -- a posted range is the quality gate instead of a hand-picked list.
"""
import datetime as dt
import html
import re
import urllib.parse

import fetch as F

PAY_FLOOR = 170_000          # top of posted base range must reach this
RELOC_PAY_FLOOR = 230_000    # broad roles outside NYC / remote must clear this to be kept
YC_PROBES_PER_RUN = 150      # companies probed per night (each ~2-8 requests)
YC_RECHECK_DAYS = 45         # re-probe companies where no board was found
BOARD_DROP_AFTER = 3         # remove a board after this many consecutive failed/empty runs
YC_MIN_TEAM = 10

CRYPTO = re.compile(r"\b(crypto\w*|blockchain|web3|defi|stablecoins?|on-?chain|nfts?|bitcoin|ethereum|solana|"
                    r"tokeni[sz]ation|dao)\b", re.I)

# Broad-scan titles must look like software roles (curated boards rely on the company list instead).
SOFTWARE = re.compile(r"\b(software|swe|sde|developer|backend|back-end|infrastructure|infra|platform|systems|distributed|"
                      r"data|ml|ai|machine learning|mts|member of technical staff|site reliability|sre|devops|cloud|"
                      r"full[- ]?stack|founding engineer|product engineer|runtime|compiler|database|storage|kernel|inference)\b", re.I)


def crypto_heavy(text):
    return len(CRYPTO.findall(text or "")) >= 2


ATS_LINK = re.compile(
    r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?(?P<gh>[A-Za-z0-9_-]+)"
    r"|boards-api\.greenhouse\.io/v1/boards/(?P<gh2>[A-Za-z0-9_-]+)"
    r"|jobs\.lever\.co/(?P<lever>[A-Za-z0-9_.-]+)"
    r"|jobs\.ashbyhq\.com/(?P<ashby>[A-Za-z0-9_.%-]+)"
    r"|api\.ashbyhq\.com/posting-api/job-board/(?P<ashby2>[A-Za-z0-9_.-]+)"
    r"|(?P<wdtenant>[a-z0-9-]+)\.(?P<wdn>wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?(?P<wdsite>[A-Za-z0-9_-]+)")
BAD_TOKENS = {"embed", "api", "v1", "jobs", "job", "careers", "static", "images", "css", "js", "assets", "favicon.ico"}


def today():
    return F.TODAY


def days_since(d):
    try:
        return (dt.date.fromisoformat(F.TODAY) - dt.date.fromisoformat(d)).days
    except Exception:
        return 10 ** 6


def pay_max(pay):
    """Largest annual dollar figure in a pay string like '$150k–$200k' (0 if none)."""
    best = 0
    for m in re.finditer(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?([kK])?", pay or ""):
        v = float(m.group(1).replace(",", ""))
        v = v * 1000 if m.group(2) else v
        if 40_000 <= v <= 2_000_000:
            best = max(best, int(v))
    return best


def board_from_match(m):
    g = m.groupdict()
    if g.get("gh") or g.get("gh2"):
        return {"ats": "greenhouse", "token": (g.get("gh") or g.get("gh2")).lower()}
    if g.get("lever"):
        return {"ats": "lever", "token": g["lever"].rstrip(".")}
    if g.get("ashby") or g.get("ashby2"):
        return {"ats": "ashby", "token": urllib.parse.unquote(g.get("ashby") or g.get("ashby2")).rstrip(".")}
    if g.get("wdtenant"):
        return {"ats": "workday", "host": f"{g['wdtenant']}.{g['wdn']}.myworkdayjobs.com",
                "tenant": g["wdtenant"], "site": g["wdsite"], "queries": ["software engineer"], "max_details": 25}
    return None


def board_key(b):
    return f"{b['ats']}:{(b.get('token') or b.get('tenant') or '').lower()}"


def boards_in(text):
    out = {}
    for m in ATS_LINK.finditer(html.unescape(text or "")):
        b = board_from_match(m)
        tok = (b or {}).get("token") or (b or {}).get("tenant") or ""
        if b and tok and tok.lower() not in BAD_TOKENS and len(tok) >= 2:
            out[board_key(b)] = b
    return out


def probe_board(ats, token):
    """Number of postings on a board (0 if missing/empty)."""
    url = {"greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
           "lever": f"https://api.lever.co/v0/postings/{token}?mode=json",
           "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{token}"}[ats]
    st, d, _ = F.http(url, timeout=30)
    if not d:
        return 0
    return len(d) if isinstance(d, list) else len(d.get("jobs", []))


# ---------------------------------------------------------------- Hacker News


def hn_threads(n=2):
    st, d, err = F.http("https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=12")
    hits = [h for h in (d or {}).get("hits", []) if re.search(r"who is hiring", h.get("title", ""), re.I)]
    if not hits and err:
        raise RuntimeError(f"HN thread list: {err}")
    return [(h["objectID"], h["title"], (h.get("created_at") or "")[:10]) for h in hits[:n]]


def hn_tier(head):
    if F.NYC_RE.search(head):
        return "nyc"
    if F.REMOTE_RE.search(head):
        if F.US_RE.search(head) or not F.NONUS_RE.search(head):
            return "remote_us" if F.US_RE.search(head) else "remote_unspecified"
    return None


SENIOR_ROLE = re.compile(r"\b(senior|sr\.?|staff|principal|lead|head of|director|manager|vp)\b[\w\s/,&-]{0,40}?\b(engineer|developer|swe)", re.I)
ANY_ROLE = re.compile(r"\b(engineer|developer|swe)s?\b", re.I)


def hn_parse(item):
    """Turn one top-level 'Who is hiring' comment into (job_or_None, boards, tier)."""
    raw = item.get("text") or ""
    text = F.html_to_text(raw.replace("<p>", "\n"))
    if not text:
        return None, {}, None
    lines = text.splitlines()
    head = lines[0][:300]
    # Location usually sits in the header; fall back to the first few lines.
    tier = hn_tier(head) or hn_tier(" ".join(lines[:3])[:500])
    found = boards_in(raw)
    if not tier:
        return None, {}, None
    if CRYPTO.search(text):
        return None, {}, tier
    job = None
    roles_all = ANY_ROLE.findall(text)
    senior_only = roles_all and len(SENIOR_ROLE.findall(text)) >= len(roles_all)
    role_hits = sorted(set(m.lower() for m in F.ROLE_HITS.findall(text)))
    pay = F.pay_from_text(text)
    req_min, pref_max, yoe = F.experience(text)
    if (roles_all and not senior_only and role_hits and pay_max(pay) >= PAY_FLOOR
            and not (req_min is not None and req_min >= 3) and not (pref_max is not None and pref_max >= 4)
            and not re.search(r"\b(intern|internship|new grad)\b", head, re.I)):
        company = re.split(r"\s*[|•–—]\s*|\s+-\s+|\(", head)[0].strip()[:60] or item.get("author", "")
        links = re.findall(r'href="([^"]+)"', html.unescape(raw))
        url = next((u for u in links if ATS_LINK.search(u)), None) \
            or next((u for u in links if re.search(r"career|job|hiring|work", u, re.I)), None) \
            or f"https://news.ycombinator.com/item?id={item['id']}"
        job = dict(id=str(item["id"]), company=company, title=head[:160], url=url, locations=[head[:120]],
                   remote=tier != "nyc", country="", dept="HN Who is hiring", posted=(item.get("created_at") or "")[:10],
                   desc=text[:6000], pay=pay, tier=tier, hn_url=f"https://news.ycombinator.com/item?id={item['id']}")
    return job, found, tier


def hn_collect(state, log):
    """Returns HN jobs (latest thread) and adds ATS boards from the last two threads to the registry."""
    jobs, added = [], 0
    threads = hn_threads(2)
    done = state.setdefault("hn_threads", {})
    for i, (tid, title, created) in enumerate(threads):
        latest = i == 0
        if not latest and done.get(tid) == "harvested":
            continue
        st, d, err = F.http(f"https://hn.algolia.com/api/v1/items/{tid}", timeout=90)
        if not d:
            log.append(f"HN thread {tid}: {err}")
            continue
        for c in d.get("children", []):
            job, found, tier = hn_parse(c)
            if latest and job:
                jobs.append(job)
            for k, b in found.items():
                added += add_board(state, k, b, company=None, source=f"hn:{tid}")
        done[tid] = "harvested"
    return jobs, added


# ---------------------------------------------------------------- YC + careers-page discovery


def add_board(state, key, b, company=None, source=""):
    reg = state.setdefault("boards", {})
    if key in reg:
        if company and not reg[key].get("company"):
            reg[key]["company"] = company
        return 0
    reg[key] = {**b, "company": company or "", "source": source, "added": today(), "fails": 0}
    return 1


def site_boards(website):
    """Scan a company's homepage + common careers paths for ATS links."""
    if not website or not website.startswith("http"):
        return {}
    root = website.rstrip("/")
    found = {}
    for path in ("", "/careers", "/jobs", "/company/careers"):
        st, txt, err = F.http(root + path, raw=True, timeout=20,
                              headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"})
        if txt:
            found.update(boards_in(txt))
        if found:
            break
    return found


def yc_slugs(c):
    s = {c.get("slug") or ""}
    words = re.findall(r"[a-z0-9]+", (c.get("name") or "").lower())
    s |= {"".join(words), "-".join(words)}
    host = urllib.parse.urlsplit(c.get("website") or "").hostname or ""
    if host:
        s.add(host.removeprefix("www.").split(".")[0])
    return [x for x in s if x and len(x) >= 3][:4]


def yc_candidates():
    st, d, err = F.http("https://yc-oss.github.io/api/companies/hiring.json", timeout=90)
    if not isinstance(d, list):
        raise RuntimeError(f"YC list: {err or st}")
    out = []
    for c in d:
        locs = (c.get("all_locations") or "") + " " + " ".join(c.get("regions") or [])
        if not re.search(r"new york|nyc|remote|united states|america", locs, re.I):
            continue
        if (c.get("team_size") or 0) < YC_MIN_TEAM:
            continue
        blob = " ".join([c.get("one_liner") or "", c.get("industry") or "", c.get("subindustry") or "", " ".join(c.get("tags") or [])])
        if CRYPTO.search(blob):
            continue
        out.append(c)
    out.sort(key=lambda c: -(c.get("team_size") or 0))
    return out


def yc_discover(state, log, known_names):
    probed = state.setdefault("yc_probed", {})
    try:
        cands = yc_candidates()
    except Exception as e:
        log.append(str(e))
        return 0
    todo = [c for c in cands if c["name"].lower() not in known_names
            and days_since(probed.get(c["slug"], {}).get("checked", "")) >= YC_RECHECK_DAYS
            and not probed.get(c["slug"], {}).get("board")]
    todo = todo[:YC_PROBES_PER_RUN]

    def one(c):
        found = site_boards(c.get("website"))
        if not found:
            for s in yc_slugs(c):
                for ats in ("ashby", "greenhouse", "lever"):
                    if probe_board(ats, s):
                        found = {f"{ats}:{s}": {"ats": ats, "token": s}}
                        break
                if found:
                    break
        return c, found

    import concurrent.futures as cf
    added = 0
    with cf.ThreadPoolExecutor(8) as ex:
        for c, found in ex.map(one, todo):
            rec = {"checked": today()}
            for k, b in list(found.items())[:1]:
                added += add_board(state, k, b, company=c["name"], source="yc")
                rec["board"] = k
            probed[c["slug"]] = rec
    state["yc_candidates"] = len(cands)
    return added


def active_boards(state, curated_keys, curated_names):
    """Registry boards to fetch tonight (skips ones already covered by the curated list)."""
    out = []
    for k, b in state.get("boards", {}).items():
        if k in curated_keys or (b.get("company") or "").lower() in curated_names:
            continue
        if b.get("dead"):
            continue
        name = b.get("company") or b.get("token") or b.get("tenant")
        out.append({**{x: y for x, y in b.items() if x not in ("company", "source", "added", "fails", "dead", "last_ok")},
                    "company": name, "broad": True, "key": k})
    return out


def record_result(state, key, ok, n_jobs, company_guess=""):
    b = state.get("boards", {}).get(key)
    if not b:
        return
    if ok and n_jobs:
        b["fails"] = 0
        b["last_ok"] = today()
        if not b.get("company") and company_guess:
            b["company"] = company_guess
    else:
        b["fails"] = b.get("fails", 0) + 1
        if b["fails"] >= BOARD_DROP_AFTER:
            b["dead"] = today()


def company_from_token(token):
    """Display name for a board found via an HN link (Lever/Ashby postings don't carry the company name)."""
    t = re.sub(r"[-.]?(inc|hq|careers|jobs)$", "", (token or "").lower()) or token
    return t.replace("-", " ").replace(".", " ").title()
