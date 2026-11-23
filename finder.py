#!/usr/bin/env python3
"""Daily job finder for Ian.

Reads companies.json, pulls each company's public job feed (Greenhouse, Lever,
Ashby, SmartRecruiters, Workday), keeps roles that could fit Ian's rules, and
writes:
  data/latest.json  - every currently-open matching job (with first_seen date)
  data/seen.json    - first-seen date per job (so "new" can be worked out)
  data/status.json  - which feeds worked / failed and how many jobs each had

Filtering here is deliberately broad (title, seniority, location). The morning
Claude task does the careful filtering (experience, pay, fit, exclusions).
Standard library only.
"""
import datetime as dt
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
UA = "IanF7-job-finder/1.0 (personal job search; github.com/IanF7)"
TIMEOUT = int(__import__("os").environ.get("FINDER_TIMEOUT", "25"))
TODAY = dt.datetime.now(dt.timezone(dt.timedelta(hours=-7))).date()  # Arizona

# ---------------------------------------------------------------- HTTP

def http(url, data=None):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < 2:
                time.sleep(3)
                continue
            raise


def strip_html(s):
    s = html.unescape(s or "")
    s = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"[ \t\r\f\v]+", " ", s).strip()

# ---------------------------------------------------------------- feeds
# Each returns a list of dicts: title, location, url, posted (YYYY-MM-DD or ""),
# description (plain text), remote (bool or None), id (str)

def greenhouse(src):
    j = http(f"https://boards-api.greenhouse.io/v1/boards/{src['slug']}/jobs?content=true")
    out = []
    for x in j.get("jobs", []):
        out.append({
            "id": f"gh-{src['slug']}-{x['id']}",
            "title": x.get("title", ""),
            "location": (x.get("location") or {}).get("name", ""),
            "url": x.get("absolute_url", ""),
            "posted": (x.get("first_published") or x.get("updated_at") or "")[:10],
            "description": strip_html(x.get("content", "")),
            "remote": None,
        })
    return out


def lever(src):
    j = http(f"https://api.lever.co/v0/postings/{src['slug']}?mode=json")
    out = []
    for x in j:
        cats = x.get("categories") or {}
        locs = [cats.get("location") or ""] + list(cats.get("allLocations") or [])
        desc = (x.get("descriptionPlain") or "") + "\n" + "\n".join(
            strip_html(l.get("content", "")) for l in x.get("lists", []))
        posted = ""
        if x.get("createdAt"):
            posted = dt.datetime.utcfromtimestamp(x["createdAt"] / 1000).date().isoformat()
        sal = x.get("salaryRange") or {}
        if sal.get("min"):
            desc += f"\nSalary range: ${sal.get('min')}-${sal.get('max')} {sal.get('interval', '')}"
        out.append({
            "id": f"lv-{src['slug']}-{x['id']}",
            "title": x.get("text", ""),
            "location": " / ".join(dict.fromkeys(l for l in locs if l)),
            "url": x.get("hostedUrl", ""),
            "posted": posted,
            "description": desc,
            "remote": (x.get("workplaceType") == "remote") or None,
        })
    return out


def ashby(src):
    j = http(f"https://api.ashbyhq.com/posting-api/job-board/{src['slug']}?includeCompensation=true")
    out = []
    for x in j.get("jobs", []):
        if x.get("isListed") is False:
            continue
        locs = [x.get("location") or ""] + [
            (a.get("location") or "") for a in x.get("secondaryLocations") or []]
        desc = x.get("descriptionPlain") or strip_html(x.get("descriptionHtml", ""))
        comp = (x.get("compensation") or {}).get("compensationTierSummary")
        if comp:
            desc += f"\nCompensation: {comp}"
        out.append({
            "id": f"ab-{src['slug']}-{x.get('id')}",
            "title": x.get("title", ""),
            "location": " / ".join(dict.fromkeys(l for l in locs if l)),
            "url": x.get("jobUrl", ""),
            "posted": (x.get("publishedAt") or "")[:10],
            "description": desc,
            "remote": bool(x.get("isRemote")) or None,
        })
    return out


def smartrecruiters(src):
    out, offset = [], 0
    while offset < 1000:
        j = http(f"https://api.smartrecruiters.com/v1/companies/{src['slug']}/postings?limit=100&offset={offset}")
        rows = j.get("content", [])
        for x in rows:
            loc = x.get("location") or {}
            if loc.get("country") and loc.get("country").lower() not in ("us", "usa"):
                if not loc.get("remote"):
                    continue
            locstr = ", ".join(p for p in [loc.get("city"), loc.get("region"), loc.get("country")] if p)
            out.append({
                "id": f"sr-{src['slug']}-{x.get('id')}",
                "title": x.get("name", ""),
                "location": locstr + (" (Remote)" if loc.get("remote") else ""),
                "url": f"https://jobs.smartrecruiters.com/{src['slug']}/{x.get('id')}",
                "posted": (x.get("releasedDate") or "")[:10],
                "description": "",
                "remote": bool(loc.get("remote")) or None,
                "_detail": x.get("ref"),
            })
        offset += 100
        if len(rows) < 100:
            break
    return out


def smartrecruiters_detail(job):
    ref = job.pop("_detail", None)
    if not ref:
        return
    try:
        d = http(ref)
        secs = (d.get("jobAd") or {}).get("sections") or {}
        job["description"] = "\n".join(strip_html((secs.get(k) or {}).get("text", ""))
                                       for k in ("jobDescription", "qualifications", "additionalInformation"))
        job["url"] = d.get("postingUrl") or job["url"]
    except Exception:
        pass


WD_POSTED = re.compile(r"Posted (Today|Yesterday|(\d+)\+? Days? Ago)", re.I)


def workday_posted(s):
    m = WD_POSTED.search(s or "")
    if not m:
        return ""
    if m.group(1).lower() == "today":
        d = 0
    elif m.group(1).lower() == "yesterday":
        d = 1
    else:
        d = int(m.group(2))
    return (TODAY - dt.timedelta(days=d)).isoformat()


def workday(src):
    base = f"https://{src['host']}/wday/cxs/{src['tenant']}/{src['site']}"
    seen, out = set(), []
    for q in src.get("queries", [""]):
        for page in range(5):
            j = http(f"{base}/jobs", {"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": q})
            rows = j.get("jobPostings", [])
            for x in rows:
                path = x.get("externalPath")
                if not path or path in seen:
                    continue
                seen.add(path)
                out.append({
                    "id": f"wd-{src['tenant']}-{path.rsplit('_', 1)[-1]}",
                    "title": x.get("title", ""),
                    "location": x.get("locationsText", ""),
                    "url": f"https://{src['host']}/en-US/{src['site']}{path}",
                    "posted": workday_posted(x.get("postedOn", "")),
                    "description": "",
                    "remote": None,
                    "_detail": f"{base}{path}",
                })
            if len(rows) < 20:
                break
            time.sleep(0.5)
    return out


def workday_detail(job):
    ref = job.pop("_detail", None)
    if not ref:
        return
    try:
        d = http(ref).get("jobPostingInfo", {})
        locs = [d.get("location") or ""] + list(d.get("additionalLocations") or [])
        job["location"] = " / ".join(l for l in locs if l) or job["location"]
        job["description"] = strip_html(d.get("jobDescription", ""))
        if d.get("remoteType"):
            job["location"] += f" ({d['remoteType']})"
        if d.get("externalUrl"):
            job["url"] = d["externalUrl"]
    except Exception:
        pass


FEEDS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby,
         "smartrecruiters": smartrecruiters, "workday": workday}

# ---------------------------------------------------------------- filters

SENIOR = re.compile(r"\b(senior|sr\.?|lead|principal|staff|director|manager|head of|architect|"
                    r"vp|vice president|chief|expert|distinguished|iii|iv|v)\b|\b(level|grade)\s*[3-9]\b", re.I)
ART = re.compile(r"\b(artist|animator|animation|vfx|concept art|illustrat|rigger|rigging|"
                 r"technical artist|tech artist|lighting|environment art|character art|ui artist|"
                 r"art director|sculpt|texture)\b", re.I)
GAME_TITLE = re.compile(r"\b(design(er)?|gameplay|game|programmer|engineer|developer|prototyp|"
                        r"tools|qa|quality assurance|tester|test analyst|producer|scripter|"
                        r"unity|unreal|ai|intern|apprentice)\b", re.I)
ENGINE = re.compile(r"\bunity(3d)?\b(?!\s+of\b)|\bunreal\b|\bue[45]\b|real[- ]time 3d|game engine", re.I)
SIMVR = re.compile(r"\bxr\b|\bvr\b|virtual reality|augmented reality|mixed reality|simulation|simulator", re.I)
SW_TITLE = re.compile(r"\b(software|developer|programmer|engineer)\b", re.I)
SW_NONWEB_BAD = re.compile(r"\b(web|front[- ]?end|full[- ]?stack|ui engineer|react|angular|"
                           r"wordpress|php|salesforce|servicenow|sap|sales|support engineer|"
                           r"network engineer|field|hardware|mechanical|electrical|manufacturing|"
                           r"process engineer|civil|technician)\b", re.I)
MOBILE_CASINO = re.compile(r"\b(casino|slots?|igaming|gambling|sweepstakes|sportsbook|roblox|uefn|fortnite creative|verse)\b", re.I)

AZ_NEAR = ["chandler", "gilbert", "tempe", "mesa", "ahwatukee", "sun lakes"]
AZ_BORDER = ["phoenix", "scottsdale", "queen creek", "guadalupe", "san tan", "laveen"]
AZ_ANY = AZ_NEAR + AZ_BORDER + ["glendale", "peoria", "surprise", "goodyear", "avondale",
                                "tolleson", "buckeye", "arizona", ", az", " az ", "az,"]
TX = re.compile(r"\b(texas|tx|austin|dallas|fort worth|houston|san antonio|plano|frisco|richardson|irving|round rock|el paso)\b", re.I)
CAROLINAS = re.compile(r"\b(north carolina|south carolina|nc|sc|raleigh|durham|cary|charlotte|morrisville|"
                       r"chapel hill|greensboro|wilmington|charleston|columbia|greenville)\b", re.I)
REMOTE = re.compile(r"\bremote\b|work from home|\bwfh\b|anywhere", re.I)
NON_US = re.compile(r"\b(canada|uk|united kingdom|england|london|ireland|germany|france|poland|"
                    r"netherlands|spain|sweden|finland|india|china|japan|korea|singapore|australia|"
                    r"brazil|mexico|emea|europe|apac|latam|montreal|toronto|vancouver|quebec)\b", re.I)


def location_tier(job):
    loc = (job["location"] or "").lower().replace("costa mesa", "costa-m")
    padded = f" {loc} "
    if any(c in padded for c in AZ_ANY):
        if any(c in loc for c in AZ_NEAR):
            return "az", "near"
        if any(c in loc for c in AZ_BORDER):
            return "az", "borderline"
        return "az", "far"
    is_remote = job.get("remote") or REMOTE.search(loc)
    if is_remote:
        if NON_US.search(loc) and not re.search(r"\b(us|usa|united states|u\.s\.)\b", loc):
            return None, None
        return "remote", ""
    if TX.search(loc):
        return "tx", ""
    if CAROLINAS.search(loc):
        return "carolinas", ""
    return None, None


PAY = re.compile(r"\$\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d)?\s?[kK])(?:\s*(?:-|–|—|to)\s*\$?\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d)?\s?[kK]))?")
HOURLY = re.compile(r"\$\s?(\d{2,3}(?:\.\d{2})?)\s*(?:-|–|to)?\s*\$?\s?(\d{2,3}(?:\.\d{2})?)?\s*(?:/|per)\s*(?:hr|hour)", re.I)
YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|to|–)?\s*\d{0,2}\s*\+?\s*years?", re.I)


def money(s):
    s = s.replace(",", "").replace(" ", "")
    if s[-1] in "kK":
        return int(float(s[:-1]) * 1000)
    return int(s)


def extract_pay(desc):
    vals = []
    for m in PAY.finditer(desc):
        lo = money(m.group(1))
        hi = money(m.group(2)) if m.group(2) else lo
        if 25000 <= lo <= 400000:
            vals.append((lo, hi))
    if vals:
        return {"min": min(v[0] for v in vals), "max": max(v[1] for v in vals), "unit": "year"}
    m = HOURLY.search(desc)
    if m:
        lo = float(m.group(1))
        hi = float(m.group(2)) if m.group(2) else lo
        return {"min": lo, "max": hi, "unit": "hour"}
    return None


def min_years(desc):
    yrs = [int(m.group(1)) for m in YEARS.finditer(desc) if int(m.group(1)) <= 15]
    return min(yrs) if yrs else None


def keep(company, job):
    t = job["title"]
    if SENIOR.search(t) or MOBILE_CASINO.search(t) or MOBILE_CASINO.search(company["name"]):
        return None
    tier, commute = location_tier(job)
    if not tier:
        return None
    cat = company["category"]
    desc = job.get("description", "")
    engine = bool(ENGINE.search(t) or ENGINE.search(desc))
    if cat == "game":
        if ART.search(t) or not GAME_TITLE.search(t):
            return None
        bucket = "game"
    else:
        # Unity/Unreal outside games: needs engine mention; otherwise general software.
        if engine and not ART.search(t) and SW_TITLE.search(t + " developer"):
            bucket = "unity-unreal"
        elif SW_TITLE.search(t) and not SW_NONWEB_BAD.search(t):
            bucket = "software"
        else:
            return None
    # Out-of-state is only for game roles (review only); Unity/Unreal + software need AZ or remote
    if tier in ("tx", "carolinas") and bucket != "game":
        return None
    return {"tier": tier, "commute": commute, "bucket": bucket, "engine_mentioned": engine,
            "sim_or_xr_mentioned": bool(SIMVR.search(t) or SIMVR.search(desc))}

# ---------------------------------------------------------------- main

def main():
    cfg = json.loads((ROOT / "companies.json").read_text())
    seen_path = DATA / "seen.json"
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}
    status, results = [], []

    for company in cfg["companies"]:
        for src in company["sources"]:
            label = src.get("slug") or f"{src.get('tenant')}/{src.get('site')}"
            entry = {"company": company["name"], "ats": src["ats"], "feed": label}
            try:
                jobs = FEEDS[src["ats"]](src)
                entry.update(ok=True, total_jobs=len(jobs))
            except Exception as e:  # broken slug, blocked, etc.
                entry.update(ok=False, error=f"{type(e).__name__}: {e}"[:200])
                status.append(entry)
                print(f"FAIL {company['name']} [{src['ats']}:{label}] {entry['error']}", file=sys.stderr)
                continue
            # First pass on title/location only (cheap), then fetch details for survivors
            kept = 0
            for job in jobs:
                pre = keep(company, {**job, "description": job.get("description", "")})
                needs_detail = "_detail" in job
                if not pre and not needs_detail:
                    continue
                if needs_detail:
                    # Only fetch details when the title could plausibly fit
                    if SENIOR.search(job["title"]) or ART.search(job["title"]):
                        continue
                    if not (GAME_TITLE.search(job["title"]) or SW_TITLE.search(job["title"])):
                        continue
                    (workday_detail if src["ats"] == "workday" else smartrecruiters_detail)(job)
                    time.sleep(0.3)
                    pre = keep(company, job)
                    if not pre:
                        continue
                job.pop("_detail", None)
                desc = job.get("description", "")
                first_seen = seen.setdefault(job["id"], TODAY.isoformat())
                results.append({
                    "company": company["name"],
                    "category": company["category"],
                    **pre,
                    "title": job["title"],
                    "location": job["location"],
                    "url": job["url"],
                    "posted": job["posted"],
                    "first_seen": first_seen,
                    "pay": extract_pay(desc),
                    "min_years_mentioned": min_years(desc),
                    "description_excerpt": desc[:3500],
                    "id": job["id"],
                })
                kept += 1
            entry["matched"] = kept
            status.append(entry)
            print(f"ok   {company['name']} [{src['ats']}:{label}] {len(jobs)} jobs, {kept} matched")
            time.sleep(1)

    # Drop seen entries older than 120 days that are no longer open
    open_ids = {r["id"] for r in results}
    cutoff = (TODAY - dt.timedelta(days=120)).isoformat()
    seen = {k: v for k, v in seen.items() if k in open_ids or v >= cutoff}

    results.sort(key=lambda r: (r["first_seen"], r["posted"]), reverse=True)
    DATA.mkdir(exist_ok=True)
    (DATA / "latest.json").write_text(json.dumps({
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "date_arizona": TODAY.isoformat(),
        "count": len(results),
        "new_today": sum(1 for r in results if r["first_seen"] == TODAY.isoformat()),
        "jobs": results,
    }, indent=1))
    (DATA / "seen.json").write_text(json.dumps(seen, indent=0, sort_keys=True))
    (DATA / "status.json").write_text(json.dumps({
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "feeds_ok": sum(1 for s in status if s["ok"]),
        "feeds_failed": sum(1 for s in status if not s["ok"]),
        "feeds": status,
    }, indent=1))
    print(f"\n{len(results)} matching jobs; {sum(1 for s in status if s['ok'])} feeds ok, "
          f"{sum(1 for s in status if not s['ok'])} failed")


if __name__ == "__main__":
    main()
