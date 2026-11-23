#!/usr/bin/env python3
"""Ian's auto-applier.

1. Reads the newest "Job Digest" emails from Gmail (IMAP + app password) and pulls
   the machine-readable APPLY-DATA block the morning task adds to each digest.
2. For each packet job on Greenhouse, Lever or Ashby that hasn't been handled yet:
   opens the application in a real browser, uploads the résumé and a cover letter
   PDF built from Ian's template, fills every field it has an answer for, and
   submits it.
3. It never gets around bot checks. If a CAPTCHA appears, a required question has
   no known answer, or anything looks off, it leaves that tab open and lists it
   under "Needs you" for Ian to finish by hand.

Run:  run.bat   (or:  python apply.py  /  python apply.py --dry-run)
"""
import argparse
import os
import datetime as dt
import email
import imaplib
import json
import re
import sys
import time
from email.header import decode_header
from pathlib import Path

import yaml
from fpdf import FPDF
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
OUT = HERE / "output"
LOG = HERE / "applied.json"
START, END = "===APPLY-DATA-START===", "===APPLY-DATA-END==="

# --------------------------------------------------------------------------- config / log

def load_config():
    p = HERE / "config.yaml"
    if not p.exists():
        sys.exit("config.yaml not found. Copy your config.yaml into the auto_apply folder first.")
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    todo = [k for k, v in flatten(cfg).items() if isinstance(v, str) and "TODO" in v]
    if todo:
        sys.exit("Fill in these settings in config.yaml first:\n  " + "\n  ".join(todo))
    return cfg


def flatten(d, prefix=""):
    out = {}
    for k, v in (d or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


def load_log():
    return json.loads(LOG.read_text(encoding="utf-8")) if LOG.exists() else {}


def save_log(log):
    LOG.write_text(json.dumps(log, indent=1, ensure_ascii=False), encoding="utf-8")

# --------------------------------------------------------------------------- Gmail

def fetch_packets(cfg, days):
    g = cfg["gmail"]
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(g["address"], g["app_password"].replace(" ", ""))
    M.select('"[Gmail]/All Mail"', readonly=True)
    typ, data = M.search(None, "X-GM-RAW", f'"subject:(Job Digest) newer_than:{days}d"')
    packets = {}
    for num in (data[0].split() if data and data[0] else []):
        typ, msg_data = M.fetch(num, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])
        body = ""
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                body += part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        if not body:  # HTML-only email: strip tags
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    h = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                    body += re.sub(r"<[^>]+>", "", h.replace("<br>", "\n"))
        if START not in body:
            continue
        raw = body.split(START, 1)[1].split(END, 1)[0]
        raw = re.sub(r"&quot;", '"', raw).strip()
        try:
            for p in json.loads(raw):
                packets[p["id"]] = p
        except json.JSONDecodeError as e:
            subj = decode_header(msg["Subject"])[0][0]
            print(f"  ! couldn't read the apply data in '{subj}': {e}")
    M.logout()
    return list(packets.values())

# --------------------------------------------------------------------------- cover letter PDF

def build_cover_letter(cfg, packet):
    tpl = (HERE / cfg["files"]["cover_letter_template"]).read_text(encoding="utf-8")
    cl = packet.get("cover_letter") or {}
    text = (tpl.replace("[DATE]", cl.get("date") or dt.date.today().strftime("%B %d, %Y").replace(" 0", " "))
               .replace("[COMPANY]", cl.get("company") or packet["company"])
               .replace("[JOB TITLE]", cl.get("job_title") or packet["title"])
               .replace("[WHY THIS COMPANY PARAGRAPH]", (cl.get("why_paragraph") or "").strip()))
    ch = cl.get("changed_sentence") or {}
    if ch.get("old") and ch.get("new") and ch["old"] in text:
        text = text.replace(ch["old"], ch["new"])
    if "[" in text and "]" in text and re.search(r"\[(DATE|COMPANY|JOB TITLE|WHY THIS COMPANY PARAGRAPH)\]", text):
        raise ValueError("cover letter still has an unfilled blank")
    OUT.mkdir(exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9]+", "_", f"{packet['company']}_{packet['title']}")[:80]
    pdf_path = OUT / f"IanFletcher_CoverLetter_{safe}.pdf"
    fonts = []
    for cand in [r"C:\Windows\Fonts\calibri.ttf", r"C:\Windows\Fonts\arial.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
        if Path(cand).exists():
            bold = cand.replace("calibri.ttf", "calibrib.ttf").replace("arial.ttf", "arialbd.ttf").replace("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")
            fonts = [cand, bold if Path(bold).exists() else cand]
            break
    if not fonts:
        text = text.encode("latin-1", "replace").decode("latin-1")
    for size in (11, 10.5, 10):  # shrink only if needed to keep it on one page
        pdf = FPDF(format="Letter")
        pdf.set_margins(22, 18, 22)
        pdf.set_auto_page_break(True, 16)
        pdf.add_page()
        font = "Helvetica"
        if fonts:
            pdf.add_font("Body", "", fonts[0])
            pdf.add_font("Body", "B", fonts[1])
            font = "Body"
        first = True
        for line in text.splitlines():
            if first and line.strip():
                pdf.set_font(font, "B", 16)
                pdf.multi_cell(0, 8, line.strip(), align="L", new_x="LMARGIN", new_y="NEXT")
                first = False
                continue
            pdf.set_font(font, "", size)
            if not line.strip():
                pdf.ln(3)
            else:
                pdf.multi_cell(0, size * 0.47, line.strip(), align="L", new_x="LMARGIN", new_y="NEXT")
        if pdf.page_no() == 1:
            break
    pdf.output(str(pdf_path))
    return pdf_path, text

# --------------------------------------------------------------------------- answering

def norm(s):
    s = re.sub(r"[✱*•]|â\S{0,2}", " ", (s or "").lower())  # required-field markers (and garbled ones)
    return re.sub(r"\s+", " ", s).strip()


def answer_for(label, kind, cfg, packet, letter_text):
    """Return (answer, confident). answer=None means leave blank."""
    me, a = cfg["me"], cfg["answers"]
    L = norm(label)
    pk = packet.get("answers") or {}
    for extra in pk.get("extra") or []:
        q = norm(extra.get("question"))
        if q and (q in L or L in q):
            return extra.get("answer"), True
    yrs = re.search(r"year|how (long|much)|experience", L)
    rules = [
        # questions first, so "relocate to our location" or "email opt-in" don't match contact fields
        (r"text message|\bsms\b|opt.?in|marketing|newsletter", "NO_OPTIN"),
        (r"privacy|consent|acknowledge|i agree|terms of|certify|attest|confirm (that|the)", "AGREE"),
        (r"legally authori[sz]ed|authori[sz]ed to work|eligible to work|work authori[sz]ation|right to work", a["authorized_to_work_us"]),
        (r"sponsor|visa", a["need_sponsorship"]),
        (r"18 years|at least 18|over 18|of legal age", "Yes"),
        (r"relocat", a["willing_to_relocate"]),
        (r"on-?site|in[- ]office|hybrid|commut", a["ok_with_onsite_or_hybrid"]),
        (r"start date|available to start|earliest|notice period|when can you start", a["start_date"]),
        (r"salary|compensation|pay (expectation|requirement)|desired pay|expected pay", a["salary_expectation"]),
        (r"how did you (hear|find|learn)|where did you (see|find|hear)|^source\b", a["how_did_you_hear"]),
        (r"referr|who referred", a["referral"]),
        (r"security clearance|\bclearance\b", a["security_clearance"]),
        (r"background check|drug (test|screen)", a["ok_with_background_check"]),
        (r"previously (worked|been employed|applied)|former employee|ever (worked|been employed)", "No"),
        (r"relative|family member|related to (anyone|an employee)", "No"),
        (r"non-?compete|non-?solicit", "No"),
        (r"pronoun", a["pronouns"]),
        (r"sexual orientation|lgbt|transgender", a["eeo_decline"]),
        (r"hispanic|latino", a["eeo_hispanic"]),
        (r"\brace\b|ethnic", a["eeo_race"]),
        (r"veteran", a["eeo_veteran"]),
        (r"disabilit", a["eeo_disability"]),
        (r"\bgender\b|\bsex\b", a["eeo_gender"]),
        (r"unreal", a["years_unreal"] if yrs else None),
        (r"\bunity\b", a["years_unity"] if yrs else None),
        (r"c\+\+", a["years_cpp"] if yrs else None),
        (r"years.*(professional|industry|work|software|programming)|(professional|industry) experience", a["years_professional"]),
        (r"shipped|released (a )?(title|game)", a["shipped_title"]),
        (r"why .*(work|join|interested|apply)|interest(ed)? in (this|the|our)|what (draws|excites|interests) you|why (this|us)", pk.get("why_company")),
        (r"project|tell us about|describe (a|something)|example of (your )?work|proudest|accomplishment", pk.get("relevant_project")),
        (r"cover letter", letter_text),
        # contact / profile fields
        (r"e-?mail", me["email"]),
        (r"phone|mobile", me["phone"]),
        (r"linkedin", me["linkedin"]),
        (r"github", me["github"]),
        (r"portfolio|personal (web)?site|website|^urls?\b", me["portfolio"]),
        (r"school|university|college|institution", me["school"]),
        (r"current (company|employer)|^company( name)?\*?$|^org", me["current_company"]),
        (r"current (job )?title|current role", me["current_title"]),
        (r"\bdegree\b", me["degree"]),
        (r"discipline|major|field of study", me["major"]),
        (r"graduat", me["grad_date"]),
        (r"\bgpa\b", me["gpa"]),
        (r"\bpreferred (first )?name\b", me["first_name"]),
        (r"\bfirst name\b|^first$|given name", me["first_name"]),
        (r"\blast name\b|^last$|surname|family name", me["last_name"]),
        (r"^(full |legal )?name\b|your name", f"{me['first_name']} {me['last_name']}"),
        (r"\bcity\b|location|where are you (located|based)|address", me["location"]),
        (r"^(state|province)\b", me["state"]),
        (r"zip|postal", me["zip"]),
        (r"country", "United States"),
    ]
    for pat, val in rules:
        if re.search(pat, L):
            if val is None or str(val).strip() == "":
                break
            return str(val), True
    return None, False

# --------------------------------------------------------------------------- page helpers

COLLECT_JS = r"""
() => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return (r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none') || el.type === 'file'; };
  const txt = el => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const labelOf = el => {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = lb.split(' ').map(i => txt(document.getElementById(i))).join(' ').trim(); if (t) return t; }
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l && txt(l)) return txt(l); }
    const wrap = el.closest('label'); if (wrap && txt(wrap)) return txt(wrap);
    let p = el.parentElement;
    for (let i = 0; i < 6 && p; i++, p = p.parentElement) {
      const l = p.querySelector(':scope > label, :scope > legend, :scope > .application-label, :scope > div > label, :scope > .text, :scope > [class*="label"], :scope > [class*="Label"]');
      if (l && txt(l) && !l.contains(el)) return txt(l);
    }
    return el.getAttribute('placeholder') || el.name || '';
  };
  const groupLabel = el => {
    const fs = el.closest('fieldset'); if (fs) { const lg = fs.querySelector('legend'); if (lg) return txt(lg); }
    let p = el.parentElement;
    for (let i = 0; i < 7 && p; i++, p = p.parentElement) {
      const l = p.querySelector(':scope > label, :scope > legend, :scope > .application-label, :scope > [class*="label"], :scope > [class*="Label"], :scope > .text');
      if (l && txt(l) && !l.contains(el) && !l.querySelector('input')) return txt(l);
    }
    return el.name || '';
  };
  const out = []; let i = 0; const groups = {};
  document.querySelectorAll('input, textarea, select').forEach(el => {
    const t = (el.type || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(t) || el.disabled) return;
    if (!vis(el) && t !== 'radio' && t !== 'checkbox') return;
    if ((t === 'radio' || t === 'checkbox') && el.name && document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`).length > 1) {
      if (!groups[el.name]) { groups[el.name] = {kind: t + '-group', name: el.name, label: groupLabel(el), options: [],
        required: !!el.closest('[class*="required"], fieldset[aria-required="true"]') || el.required}; out.push(groups[el.name]); }
      el.setAttribute('data-aa', 'aa' + (i++));
      groups[el.name].options.push({sel: '[data-aa="' + el.getAttribute('data-aa') + '"]', label: labelOf(el) || el.value});
      return;
    }
    el.setAttribute('data-aa', 'aa' + (i++));
    const req = el.required || el.getAttribute('aria-required') === 'true' ||
      /\*\s*$/.test(labelOf(el)) || !!(el.closest('[class*="required"]'));
    out.push({kind: el.getAttribute('role') === 'combobox' ? 'combobox' : t, sel: '[data-aa="' + el.getAttribute('data-aa') + '"]',
              label: labelOf(el), required: req, value: el.type === 'file' ? (el.files.length ? 'file' : '') : el.value,
              accept: el.accept || '', options: el.tagName === 'SELECT' ? [...el.options].map(o => o.text) : []});
  });
  return out;
}
"""

CAPTCHA_JS = r"""
() => {
  const frames = [...document.querySelectorAll('iframe')].filter(f => {
    const r = f.getBoundingClientRect(); const src = f.src || '';
    return r.width > 60 && r.height > 60 && /recaptcha\/(api2|enterprise)\/bframe|hcaptcha\.com.*challenge|challenges\.cloudflare|captcha/i.test(src);
  });
  const text = (document.body.innerText || '').toLowerCase();
  return frames.length > 0 || /verify you are (a )?human|are you a robot|complete the captcha|security check/.test(text);
}
"""

DONE_RE = re.compile(r"thank you for (applying|your (application|interest))|application (has been |was )?(submitted|received)|we('ve| have) received your application|thanks for applying", re.I)


def best_option(options, answer):
    """Pick the option text that best matches the answer."""
    A = norm(answer)
    if not options:
        return None
    opts = [o for o in options if norm(o) and not norm(o).startswith(("select", "please select", "--"))]
    for o in opts:
        if norm(o) == A:
            return o
    for o in opts:
        if A and (A in norm(o) or norm(o) in A):
            return o
    decline = ["decline", "don't wish", "do not wish", "prefer not", "not to answer", "choose not"]
    if any(d in A for d in decline):
        for o in opts:
            if any(d in norm(o) for d in decline):
                return o
    if A in ("yes", "no"):
        for o in opts:
            if norm(o).startswith(A):
                return o
    return None


def fill_page(page, cfg, packet, resume, letter_pdf, letter_text):
    """Fill everything we can. Returns list of problems (unanswered required fields)."""
    problems, filled = [], []
    fields = page.evaluate(COLLECT_JS)
    # 1) files first (résumé parsing can pre-fill other fields)
    for f in [f for f in fields if f["kind"] == "file"]:
        L = norm(f["label"]) + " " + norm(f.get("accept"))
        target = letter_pdf if "cover" in L else resume
        if "cover" not in L and not re.search(r"resume|résumé|cv|attach|upload|file|pdf|doc", L) and filled:
            continue
        try:
            page.set_input_files(f["sel"], str(target))
            filled.append(("file", f["label"] or "résumé", target.name))
            time.sleep(2.5)
        except Exception as e:
            problems.append(f"upload failed ({f['label']}): {e}")
    time.sleep(1)
    fields = page.evaluate(COLLECT_JS)
    for f in fields:
        k, label = f["kind"], f["label"]
        if k == "file":
            if f["required"] and not f["value"]:
                problems.append(f"file field not filled: {label}")
            continue
        ans, ok = answer_for(label, k, cfg, packet, letter_text)
        try:
            if k in ("checkbox",):
                if ans == "AGREE":
                    page.check(f["sel"])
                    filled.append((k, label, "checked"))
                elif f["required"]:
                    problems.append(f"required checkbox: {label}")
                continue
            if k in ("radio-group", "checkbox-group"):
                if ans in (None, "AGREE", "NO_OPTIN"):
                    if ans == "AGREE" and k == "checkbox-group":
                        page.check(f["options"][0]["sel"])
                        continue
                    if f["required"]:
                        problems.append(f"required choice: {label}")
                    continue
                pick = best_option([o["label"] for o in f["options"]], ans)
                if pick is None:
                    if f["required"]:
                        problems.append(f"no matching option for '{label}' (answer: {ans})")
                    continue
                sel = next(o["sel"] for o in f["options"] if o["label"] == pick)
                page.check(sel)
                filled.append((k, label, pick))
                continue
            if ans in ("AGREE", "NO_OPTIN"):
                continue
            if f.get("value") and k not in ("select-one", "combobox"):
                continue  # already filled (e.g., parsed from résumé)
            if ans is None:
                if f["required"]:
                    problems.append(f"required question with no answer: {label}")
                continue
            if k == "select-one":
                pick = best_option(f["options"], ans)
                if pick is None:
                    if f["required"]:
                        problems.append(f"no matching option for '{label}' (answer: {ans})")
                    continue
                page.select_option(f["sel"], label=pick)
                filled.append((k, label, pick))
            elif k == "combobox":
                page.click(f["sel"])
                page.fill(f["sel"], "")
                page.type(f["sel"], ans[:40], delay=25)
                time.sleep(0.8)
                opts = page.locator('[role="option"]:visible')
                texts = [t.strip() for t in opts.all_inner_texts()] if opts.count() else []
                pick = best_option(texts, ans) if texts else None
                if pick:
                    opts.filter(has_text=pick).first.click()
                    filled.append((k, label, pick))
                elif texts:
                    page.keyboard.press("Escape")
                    if f["required"]:
                        problems.append(f"no matching option for '{label}' (answer: {ans})")
                else:
                    page.keyboard.press("Enter")
                    filled.append((k, label, ans))
            else:
                page.fill(f["sel"], ans)
                filled.append((k, label, ans[:60]))
        except Exception as e:
            if f["required"]:
                problems.append(f"couldn't fill '{label}': {type(e).__name__}")
    return problems, filled


def open_application(page, url, ats):
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    time.sleep(2)
    if ats == "lever" and not page.url.rstrip("/").endswith("/apply"):
        page.goto(page.url.split("?")[0].rstrip("/") + "/apply", wait_until="domcontentloaded")
    elif ats == "ashby" and "/application" not in page.url:
        page.goto(page.url.split("?")[0].rstrip("/") + "/application", wait_until="domcontentloaded")
    else:
        for txt in ["Apply for this job", "Apply Now", "Apply now", "Apply"]:
            btn = page.get_by_role("button", name=txt).or_(page.get_by_role("link", name=txt))
            if btn.count() and not page.locator("input[type=file]").count():
                try:
                    btn.first.click(timeout=3000)
                    time.sleep(2)
                except Exception:
                    pass
                break
    try:
        page.wait_for_selector("input[type=file], input[type=email]", timeout=15000)
    except PWTimeout:
        pass


def submit(page):
    for name in ["Submit application", "Submit Application", "Submit"]:
        btn = page.get_by_role("button", name=name)
        if btn.count():
            btn.first.click()
            return True
    btn = page.locator("button[type=submit], input[type=submit]")
    if btn.count():
        btn.first.click()
        return True
    return False


def detect_ats(url):
    u = url.lower()
    if "greenhouse.io" in u or "gh_jid=" in u:
        return "greenhouse"
    if "lever.co" in u:
        return "lever"
    if "ashbyhq.com" in u:
        return "ashby"
    if "myworkdayjobs" in u or "workday" in u:
        return "workday"
    return "other"

# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="fill forms but never click Submit")
    ap.add_argument("--days", type=int, default=4, help="look at digest emails from the last N days")
    ap.add_argument("--packets", help="read packets from a JSON file instead of Gmail (testing)")
    ap.add_argument("--retry", action="store_true", help="also retry jobs that were left for you before")
    args = ap.parse_args()

    cfg = load_config()
    s = cfg["settings"]
    dry = args.dry_run or not s.get("auto_submit", True)
    resume = (HERE / cfg["files"]["resume_pdf"]).resolve()
    if not resume.exists():
        sys.exit(f"Résumé not found: {resume}")

    print("Reading digest emails..." if not args.packets else f"Reading {args.packets}...")
    packets = json.loads(Path(args.packets).read_text(encoding="utf-8")) if args.packets else fetch_packets(cfg, args.days)
    log = load_log()
    done = ("submitted", "skipped") if args.retry else ("submitted", "skipped", "needs_you")
    todo = [p for p in packets if log.get(p["id"], {}).get("status") not in done]
    print(f"{len(packets)} packets found, {len(todo)} not handled yet.")
    if not todo:
        return

    results = {"submitted": [], "needs_you": [], "skipped": []}
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(HERE / "browser_profile"), headless=bool(os.environ.get("AA_HEADLESS")),
                                                    viewport={"width": 1280, "height": 900},
                                                    executable_path=s.get("browser_path") or None)
        for p in todo[: s.get("max_per_run", 15)]:
            name = f"{p['title']} — {p['company']}"
            url = p.get("apply_url") or p.get("url")
            ats = p.get("ats") or detect_ats(url or "")
            if ats not in ("greenhouse", "lever", "ashby"):
                if ats == "workday" or s.get("open_other_sites_for_me", True):
                    page = ctx.new_page()
                    try:
                        page.goto(url, timeout=45000)
                    except Exception:
                        pass
                results["needs_you"].append((name, url, f"{ats} site — apply by hand (tab left open)"))
                log[p["id"]] = {"status": "needs_you", "reason": ats, "when": str(dt.datetime.now())[:16], "url": url}
                save_log(log)
                continue
            print(f"\n→ {name}  [{ats}]")
            page = ctx.new_page()
            try:
                letter_pdf, letter_text = build_cover_letter(cfg, p)
                open_application(page, url, ats)
                if page.evaluate(CAPTCHA_JS):
                    raise RuntimeError("CAPTCHA/bot check on the page")
                if re.search(r"no longer (accepting|available)|job (is )?closed|position has been filled|page not found|404", page.inner_text("body")[:3000], re.I):
                    results["skipped"].append((name, url, "job closed"))
                    log[p["id"]] = {"status": "skipped", "reason": "closed", "when": str(dt.datetime.now())[:16], "url": url}
                    save_log(log)
                    page.close()
                    continue
                problems, filled = fill_page(page, cfg, p, resume, letter_pdf, letter_text)
                for k, label, val in filled:
                    print(f"   filled: {label[:50]!r} = {str(val)[:50]!r}")
                if problems:
                    raise RuntimeError("; ".join(problems[:4]))
                if dry:
                    results["needs_you"].append((name, url, "dry run — review and click Submit yourself"))
                    log[p["id"]] = {"status": "needs_you", "reason": "dry run", "when": str(dt.datetime.now())[:16], "url": url}
                    save_log(log)
                    continue
                if not submit(page):
                    raise RuntimeError("couldn't find the Submit button")
                time.sleep(4)
                if page.evaluate(CAPTCHA_JS):
                    raise RuntimeError("CAPTCHA appeared after Submit")
                body = page.inner_text("body")
                if not DONE_RE.search(body):
                    page.wait_for_timeout(5000)
                    body = page.inner_text("body")
                if DONE_RE.search(body):
                    print("   ✓ submitted")
                    results["submitted"].append((name, url, ""))
                    log[p["id"]] = {"status": "submitted", "when": str(dt.datetime.now())[:16], "url": url,
                                    "cover_letter": letter_pdf.name}
                    save_log(log)
                    page.close()
                else:
                    raise RuntimeError("clicked Submit but didn't see a confirmation — check the page")
            except Exception as e:
                msg = str(e).split("\n")[0][:220]
                print(f"   ! needs you: {msg}")
                results["needs_you"].append((name, url, msg))
                log[p["id"]] = {"status": "needs_you", "reason": msg, "when": str(dt.datetime.now())[:16], "url": url}
                save_log(log)
            time.sleep(s.get("pause_between_jobs_sec", 8))

        print("\n================ SUMMARY ================")
        print(f"Submitted: {len(results['submitted'])}")
        for n, u, _ in results["submitted"]:
            print(f"  ✓ {n}")
        print(f"Needs you (tabs left open): {len(results['needs_you'])}")
        for n, u, why in results["needs_you"]:
            print(f"  • {n}\n      {why}\n      {u}")
        if results["skipped"]:
            print(f"Skipped: {len(results['skipped'])}")
            for n, u, why in results["skipped"]:
                print(f"  - {n} ({why})")
        if results["needs_you"]:
            input("\nFinish the open tabs, then press Enter here to close the browser...")
        ctx.close()


if __name__ == "__main__":
    main()
