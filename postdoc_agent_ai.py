#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║        POSTDOC JOB BOARD AGENT — AI-Powered Daily Digest        ║
║  Checks mathjobs.org · academicpositions.com · mathhire.org     ║
║  Uses Groq + Llama 3.1 (FREE) for intelligent job matching      ║
║  Sends a formatted HTML digest to your Gmail every morning      ║
╚══════════════════════════════════════════════════════════════════╝

WHY GROQ?
  - 100% free, no credit card needed
  - Uses Llama 3.1 8B — a powerful open-source model
  - 14,400 free API calls/day (you will use ~3-10 per run)
  - Understands CONTEXT: won't reject a postdoc just because it
    mentions "internship experience is a plus" in passing
  - Specifically designed for automated scripts like this one

SETUP (one time, ~15 minutes):
  1. Get your FREE Groq API key:
       console.groq.com -> Sign up (free) -> API Keys -> Create key
       Paste it into YOUR_CONFIG below

  2. Get your Gmail App Password:
       myaccount.google.com -> Security -> 2-Step Verification -> App passwords
       Create one called "Postdoc Agent" -> paste below

  3. Install dependencies:
       pip install requests beautifulsoup4 feedparser lxml groq

  4. Test: python postdoc_agent_ai.py

  5. Schedule daily on PythonAnywhere (free) - instructions at bottom
"""

import os
import random
import subprocess, sys

def _install(pkg):
    subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "--quiet", "--default-timeout=1000"])

packages = ["requests", "beautifulsoup4", "feedparser", "lxml", "groq", "curl_cffi", "playwright", "python-dotenv"]

for _pkg in packages:
    # Handle the fact that beautifulsoup4 is imported as 'bs4'
    import_name = "bs4" if _pkg == "beautifulsoup4" else _pkg.replace("-","_").split(".")[0]
    
    try:
        __import__(import_name)
    except ImportError:
        print(f"Installing {_pkg}...")
        _install(_pkg)

# Ensure the Playwright Chromium browser binary is installed.
# Running this via `sys.executable -m` ensures it installs in the correct Python environment.
# It is safe to run every time; if it's already installed, it will just exit instantly.
print("Checking Playwright Chromium installation...")
subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])

from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
#  YOUR CONFIG — FILL THIS IN
# ─────────────────────────────────────────────
YOUR_CONFIG = {
    # Free API key from console.groq.com — no credit card needed
    "groq_api_key": os.environ.get("GROQ_API_KEY"),

    # Your Gmail address (must have 2-Step Verification enabled)
    "sender_email": os.environ.get("SENDER_EMAIL"),

    # App Password — NOT your real password
    # Get it: myaccount.google.com -> Security -> App passwords
    "gmail_app_password": os.environ.get("EMAIL_PASSWORD"),

    # Where to deliver the digest (can be same as sender)
    "recipient_email": os.environ.get("RECIPIENT_EMAIL"),

    # How many days back to consider a listing "new"
    # 1 = only today | 2 = last 2 days (useful if the script missed a day)
    "days_back": 1,

    # Groq model — llama-3.1-8b-instant is fast and free
    # Smarter alternative: "llama3-70b-8192" (still free, slower)
    "groq_model": "llama-3.1-8b-instant",

    # How many listings to send to the AI per batch
    # Lower = more accurate, more API calls | Higher = fewer calls
    "ai_batch_size": 5,
}

# ─────────────────────────────────────────────
#  YOUR RESEARCH PROFILE
#  The AI uses this to judge relevance — edit freely
# ─────────────────────────────────────────────
RESEARCHER_PROFILE = """
I am a Nigerian mathematician with:
- BSc in Mathematics
- MSc in Complex Analysis
- PhD in Numerical Analysis
- Several journal publications and conference papers
- Teaching and lecturing experience at university level (over 12 years of lecturing experience)

I am looking for POSTDOCTORAL positions and also open to faculty positions and teaching positions (not PhD studentships, not internships) in:
- Numerical Analysis (core expertise)
- Computational Mathematics
- Applied Mathematics
- Scientific Computing
- Numerical methods for PDEs
- Numerical Linear Algebra
- High-Performance Computing (HPC)
- Complex Analysis and Operator Theory
- Mathematical modelling and simulation

I am open to positions in Europe, USA, Canada, Nigeria and Australia.

I am NOT interested in positions requiring:
- A medical degree (MD/PhD programs)
- US/EU citizenship or security clearance only
- A background purely in biology, chemistry, or wet-lab sciences
"""

# ─────────────────────────────────────────────
#  IMPORTS
# ─────────────────────────────────────────────
import os, json, smtplib, logging
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from time import sleep

# import requests
import feedparser
from bs4 import BeautifulSoup
from groq import Groq
from curl_cffi import requests
from playwright.sync_api import sync_playwright

master_handler = logging.FileHandler("master.log", mode="a", encoding="utf-8")

# 2. Daily Log: mode='w' wipes the file clean every time the script starts
daily_handler = logging.FileHandler("daily.log", mode="w", encoding="utf-8")

# 3. Console output
console_handler = logging.StreamHandler()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[master_handler, daily_handler, console_handler]
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0 Safari/537.36"
    )
}

SEEN_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "seen_listings.json"
)

IMPERSONATE = "chrome120"

TIMEOUT = 30

EMAIL_PORT = 587

# ─────────────────────────────────────────────
#  SEEN LISTINGS TRACKER
# ─────────────────────────────────────────────
def load_seen() -> set:
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE) as f:
        data = json.load(f)
    return set(data.get("seen", []))


def save_seen(seen: set):
    entries = list(seen)[-2000:]
    with open(SEEN_FILE, "w") as f:
        json.dump({
            "seen": entries,
            "last_run": datetime.now().isoformat(),
            "total_ever_seen": len(seen)
        }, f, indent=2)


# ─────────────────────────────────────────────
#  AI VALIDATOR  (replaces keyword matching)
# ─────────────────────────────────────────────
class AIValidator:
    """
    Uses Groq + Llama 3.1 to intelligently decide which listings
    are genuinely relevant to your research profile.

    Key advantages over keyword matching:
    - Understands context: "internship is a plus" != an internship ad
    - Catches relevant listings even without exact keyword matches
    - Reads the full description holistically, not word by word
    - Returns a short plain-English reason for each decision
    """

    def __init__(self, config: dict):
        self.client = Groq(api_key=config["groq_api_key"])
        self.model  = config["groq_model"]
        self.batch  = config["ai_batch_size"]
        self._calls = 0
        self._kept  = 0

    def _validate_batch(self, listings: list) -> list:
        listing_text = ""
        for i, item in enumerate(listings, 1):
            listing_text += (
                f"\n[{i}]\n"
                f"Title: {item['title']}\n"
                f"Institution: {item.get('institution', 'Unknown')}\n"
                f"Description: {item['summary'][:500]}\n"
            )

        prompt = f"""You are a precise academic job-matching assistant.

                        RESEARCHER PROFILE:
                        {RESEARCHER_PROFILE}

                        TASK:
                        Review {len(listings)} job listings from academic job boards.
                        For each, decide if it is a GOOD MATCH for the researcher above.

                        Be SMART about context:
                        - "internship experience is a plus" in a postdoc ad -> RELEVANT (it's still a postdoc)
                        - A PhD studentship (not a postdoc) -> NOT RELEVANT
                        - A field like biology or medicine with no math component -> NOT RELEVANT
                        - US-citizens-only security clearance role -> NOT RELEVANT
                        - Adjacent fields (mathematical physics, data science with strong math) -> use judgement, lean RELEVANT

                        LISTINGS TO EVALUATE:
                        {listing_text}

                        Respond ONLY with valid JSON, no markdown fences, no extra text:
                        {{
                        "results": [
                            {{"index": 1, "relevant": true,  "reason": "One sentence why it matches"}},
                            {{"index": 2, "relevant": false, "reason": "One sentence why it does not match"}}
                        ]
                        }}
                    """

        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=900,
            )
            self._calls += 1
            raw = resp.choices[0].message.content.strip()

            # Strip markdown fences if the model added them
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()

            data    = json.loads(raw)
            results = data.get("results", [])
            kept    = []

            for r in results:
                idx = r.get("index", 0) - 1
                if 0 <= idx < len(listings) and r.get("relevant", False):
                    item = listings[idx].copy()
                    item["ai_reason"] = r.get("reason", "Matched researcher profile")
                    kept.append(item)
                    self._kept += 1

            return kept

        except json.JSONDecodeError:
            log.warning("AI returned invalid JSON — including full batch as fallback")
            for item in listings:
                item["ai_reason"] = "Included (AI parse error — please review manually)"
            return listings

        except Exception as e:
            log.warning(f"Groq API error: {e} — including full batch as fallback")
            for item in listings:
                item["ai_reason"] = "Included (AI unavailable — please review manually)"
            return listings

    def validate_all(self, listings: list) -> list:
        if not listings:
            return []

        total_batches = (len(listings) + self.batch - 1) // self.batch
        log.info(f"AI validation: {len(listings)} listings across {total_batches} batch(es)")

        relevant = []
        for i in range(0, len(listings), self.batch):
            batch     = listings[i : i + self.batch]
            batch_num = i // self.batch + 1
            log.info(f"  Batch {batch_num}/{total_batches} — {len(batch)} listings...")
            relevant.extend(self._validate_batch(batch))
            if i + self.batch < len(listings):
                sleep(1.5)   # Stay well within Groq rate limits

        log.info(
            f"AI done: {self._kept}/{len(listings)} relevant "
            f"({self._calls} API call{'s' if self._calls != 1 else ''})"
        )
        return relevant


# ─────────────────────────────────────────────
#  BOARD 1 — mathjobs.org  (RSS feed)
# ─────────────────────────────────────────────
def fetch_mathjobs(seen: set, days_back: int) -> list:
    results = []
    cutoff  = datetime.now(timezone.utc) - timedelta(days=days_back + 1)
    feedparser.USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    log.info("Fetching mathjobs.org...")

    for url in [
        "https://www.mathjobs.org/jobs?joblst-0-0----rss--",
    ]:
        try:
            feed = feedparser.parse(url)

            if not feed.entries:
                log.warning(f"No entries found for {url}. The site might be blocking the request.")

            for entry in feed.entries:
                uid = entry.get("id") or entry.get("link", "")
                if uid in seen:
                    continue
                published = entry.get("published_parsed") or entry.get("updated_parsed")
                if published:
                    pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                    if pub_dt < cutoff:
                        continue
                title   = entry.get("title", "").strip()
                summary = BeautifulSoup(
                    entry.get("summary", ""), "lxml"
                ).get_text()[:500].strip()
                results.append({
                    "source":      "mathjobs.org",
                    "title":       title,
                    "institution": entry.get("author", "").strip(),
                    "link":        entry.get("link", ""),
                    "summary":     summary,
                    "uid":         uid,
                })
        except Exception as e:
            log.warning(f"mathjobs.org error: {e}")

    deduped = list({r["uid"]: r for r in results}.values())
    log.info(f"{len(deduped)} unseen listings from mathjobs.org")
    return deduped


# ─────────────────────────────────────────────
#  BOARD 2 — mathhire.org
# ─────────────────────────────────────────────
def fetch_mathhire(seen: set) -> list:
    results    = []
    seen_links = set()
    log.info("Fetching mathhire.org...")

    for url in [
        "https://mathhire.org/jobs/?position_type=postdoc",
        "https://mathhire.org/jobs/?search=numerical+analysis",
        "https://mathhire.org/jobs/?search=applied+mathematics",
        "https://mathhire.org/jobs/?search=computational+mathematics",
        "https://mathhire.org/jobs/?search=scientific+computing",
    ]:
        try:
            resp = requests.get(url, headers=HEADERS, impersonate=IMPERSONATE, timeout=TIMEOUT)
            if resp.status_code != 200:
                log.warning(f"Failed to fetch {url} - Status Code: {resp.status_code}")
                continue
            soup  = BeautifulSoup(resp.text, "lxml")
            cards = (
                soup.find_all("article", class_=lambda c: c and "job" in c.lower())
                or soup.find_all("li",   class_=lambda c: c and "job" in c.lower())
                or soup.find_all("div",  class_=lambda c: c and "job" in c.lower())
                or soup.find_all("a", href=lambda h: h and "/jobs/" in h and h != "/jobs/")
            )
            for card in cards:
                try:
                    if card.name == "a":
                        link  = card.get("href", "")
                        title = card.get_text(strip=True)
                        inst  = ""
                    else:
                        a = card.find("a", href=lambda h: h and "/jobs/" in str(h))
                        if not a:
                            continue
                        link  = a.get("href", "")
                        title = a.get_text(strip=True)
                        inst  = ""
                        it = card.find(class_=lambda c: c and "instit" in str(c).lower())
                        if it:
                            inst = it.get_text(strip=True)

                    if not link.startswith("http"):
                        link = "https://mathhire.org" + link
                    uid = f"mathhire_{link}"
                    if uid in seen or link in seen_links:
                        continue
                    seen_links.add(link)
                    results.append({
                        "source":      "mathhire.org",
                        "title":       title[:150].strip(),
                        "institution": inst,
                        "link":        link,
                        "summary":     card.get_text(" ", strip=True)[:500],
                        "uid":         uid,
                    })
                except Exception:
                    continue
            sleep(1)
        except Exception as e:
            log.warning(f"mathhire.org error ({url}): {e}")

    log.info(f"{len(results)} unseen listings from mathhire.org")
    return results


# ─────────────────────────────────────────────
#  BOARD 3 — academicpositions.com
# ─────────────────────────────────────────────
def fetch_academicpositions(seen: set) -> list:
    results    = []
    seen_links = set()
    base       = "https://academicpositions.com"
    positions = "&positions[0]=post-doc&positions[1]=associate-professor&positions[2]=lecturersenior-lecturer&positions[3]=researcher&positions[4]=research-assistant&positions[5]=professor"
    log.info("Fetching academicpositions.com...")

    for url in [
        f"{base}/find-jobs?search=numerical+analysis{positions}",
        f"{base}/find-jobs?search=computational+mathematics{positions}",
        f"{base}/find-jobs?search=applied+mathematics{positions}",
        f"{base}/find-jobs?search=scientific+computing{positions}",
        f"{base}/find-jobs?search=numerical+methods{positions}",
    ]:
        try:
            resp = requests.get(url, headers=HEADERS, impersonate=IMPERSONATE, timeout=TIMEOUT)
            if resp.status_code != 200:
                log.warning(f"Failed to fetch {url} - Status Code: {resp.status_code}")
                continue

            soup = BeautifulSoup(resp.content, "lxml", from_encoding="utf-8")
            cards = (
                soup.find_all("article")
                or soup.find_all("div", class_=lambda c: c and any(
                    x in str(c).lower() for x in ["job-", "position-", "listing"]
                ))
            )
            for card in cards:
                try:
                    a = card.find("a", href=lambda h: h and (
                        "/jobs/" in str(h) or "/ad/" in str(h)
                    ))
                    if not a:
                        a = card.find("a", href=True)
                    if not a:
                        continue
                    link = a.get("href", "")
                    if not link.startswith("http"):
                        link = base + link
                    uid = f"ap_{link}"
                    if uid in seen or link in seen_links:
                        continue
                    seen_links.add(link)
                    title = a.get_text(strip=True)[:150]
                    inst  = ""
                    for tag in card.find_all(["span", "p", "div"]):
                        cls = " ".join(tag.get("class", []))
                        if any(x in cls.lower() for x in ["employer","institution","university"]):
                            inst = tag.get_text(strip=True)
                            break
                    results.append({
                        "source":      "academicpositions.com",
                        "title":       title.strip(),
                        "institution": inst,
                        "link":        link,
                        "summary":     card.get_text(" ", strip=True)[:500],
                        "uid":         uid,
                    })
                except Exception:
                    continue
            sleep(random.uniform(3.0, 7.0))
        except Exception as e:
            log.warning(f"academicpositions.com error: {e}")

    log.info(f"  {len(results)} unseen listings from academicpositions.com")
    return results

def fetch_academicpositions_playwright(seen: set) -> list:
    results    = []
    seen_links = set()
    base       = "https://academicpositions.com"
    positions  = "&positions[0]=post-doc&positions[1]=associate-professor&positions[2]=lecturersenior-lecturer&positions[3]=researcher&positions[4]=research-assistant&positions[5]=professor"
    
    queries = [
        "numerical+analysis",
        "computational+mathematics",
        "applied+mathematics",
        "scientific+computing",
        "numerical+methods"
    ]
    
    log.info("Fetching academicpositions.com using Playwright...")

    # Start the Playwright context manager
    with sync_playwright() as p:
        # Launch headless Chromium browser
        browser = p.chromium.launch(headless=True)
        
        # Spoof a real browser's User-Agent and viewport
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        page = context.new_page()

        for query in queries:
            url = f"{base}/find-jobs?search={query}{positions}"
            try:
                # 1. Go to the URL and wait for the base HTML to load
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                
                # 2. Crucial Step: Wait for the JavaScript to inject the job cards
                # We tell Playwright to pause for 3 seconds to let Livewire finish its background requests
                page.wait_for_timeout(3000) 
                
                # 3. Extract the fully rendered HTML
                html = page.content()
                soup = BeautifulSoup(html, "lxml")
                
                # --- NEW BULLETPROOF LOGIC ---
                # Find EVERY link on the page that points to a job ad
                job_links = soup.find_all("a", href=lambda h: h and ("/ad/" in h.lower() or "/jobs/" in h.lower()))
                
                # We use a set to avoid processing the same job card multiple times 
                # (since a card might have a link on the title AND a link on the logo)
                processed_links = set()

                for a in job_links:
                    try:
                        link = a.get("href", "")
                        if not link.startswith("http"):
                            link = base + link
                            
                        # If we've already parsed this exact job URL, skip it
                        if link in processed_links or link in seen_links:
                            continue
                            
                        # Go up the HTML tree to find the "Card" container
                        # We climb up 3 to 4 parent elements to grab the whole block of text
                        card = a
                        for _ in range(4): 
                            if card.parent and card.parent.name in ['div', 'li', 'ul', 'section']:
                                card = card.parent
                                # If the container has enough text (title + institution + summary), we stop climbing
                                if len(card.get_text(strip=True)) > 80:
                                    break
                                    
                        processed_links.add(link)
                        seen_links.add(link)
                        
                        uid = f"ap_{link}"
                        if uid in seen:
                            continue
                        
                        # The title is usually the text inside the <a> tag itself, or the first bold element
                        title_text = a.get_text(strip=True)
                        if len(title_text) < 5: # If the link was just an image/logo, look for a heading
                            heading = card.find(["h2", "h3", "h4", "strong"])
                            title_text = heading.get_text(strip=True) if heading else "Unknown Title"
                            
                        title = title_text[:150].encode("utf-8", errors="ignore").decode("utf-8")
                        
                        # Extract the rest of the text from the card
                        card_text = card.get_text(" | ", strip=True).encode("utf-8", errors="ignore").decode("utf-8")
                        
                        # Guess the institution (Usually the first or second line of text in the card)
                        text_parts = [p.strip() for p in card_text.split("|") if len(p.strip()) > 2]
                        
                        # If the title is the first thing, the institution is likely the second thing
                        inst = text_parts[1] if len(text_parts) > 1 else "Academic Positions"
                        
                        # The summary is everything else combined
                        summary = " ".join(text_parts)[:500]
                        
                        results.append({
                            "source":      "academicpositions.com",
                            "title":       title,
                            "institution": inst,
                            "link":        link,
                            "summary":     summary,
                            "uid":         uid,
                        })
                    except Exception as e:
                        # Silently skip malformed links
                        continue
                        
            except Exception as e:
                log.warning(f"academicpositions.com error on {query}: {e}")
                
        # Clean up the browser instance
        browser.close()

    log.info(f"  {len(results)} unseen listings from academicpositions.com")
    return results

# ─────────────────────────────────────────────
#  EMAIL BUILDER
# ─────────────────────────────────────────────
def build_email(listings: list, run_date: str, stats: dict) -> str:
    source_colors = {
        "mathjobs.org":          "#1F4E79",
        "mathhire.org":          "#1D6A2E",
        "academicpositions.com": "#7D3C98",
    }

    by_source = {}
    for item in listings:
        by_source.setdefault(item["source"], []).append(item)

    cards_html = ""
    for source, items in by_source.items():
        color = source_colors.get(source, "#444")
        cards_html += f"""
        <tr><td style="padding:24px 0 8px;">
          <span style="background:{color};color:#fff;padding:5px 14px;
            border-radius:20px;font-size:13px;font-weight:bold;
            font-family:Arial,sans-serif;">
            {source} &mdash; {len(items)} listing{'s' if len(items)!=1 else ''}
          </span>
        </td></tr>"""

        for item in items:
            title  = item.get("title")  or "Untitled"
            inst   = item.get("institution", "")
            link   = item.get("link",   "#")
            reason = item.get("ai_reason", "")
            summ   = item.get("summary", "")[:230]
            if len(item.get("summary","")) > 230:
                summ += "..."

            inst_html   = f'<p style="margin:4px 0 0;font-size:13px;color:#555;font-family:Arial,sans-serif;">{inst}</p>' if inst else ""
            summ_html   = f'<p style="margin:8px 0 0;font-size:13px;color:#666;font-family:Arial,sans-serif;line-height:1.5;">{summ}</p>' if summ else ""
            reason_html = f'<p style="margin:8px 0 0;padding:8px 12px;background:#f0f6fb;border-radius:4px;font-size:12px;color:#1F4E79;font-family:Arial,sans-serif;"><b>AI verdict:</b> {reason}</p>' if reason else ""

            cards_html += f"""
        <tr><td style="padding:0 0 16px;">
          <table width="100%" cellpadding="0" cellspacing="0" border="0"
                 style="background:#fff;border:1px solid #e0e0e0;
                        border-left:5px solid {color};border-radius:6px;">
            <tr><td style="padding:16px 20px;">
              <a href="{link}" style="font-size:16px;font-weight:bold;color:{color};
                 text-decoration:none;font-family:Arial,sans-serif;line-height:1.4;">{title}</a>
              {inst_html}{summ_html}{reason_html}
              <p style="margin:10px 0 0;">
                <a href="{link}" style="display:inline-block;padding:6px 16px;
                   background:{color};color:#fff;border-radius:4px;font-size:12px;
                   font-weight:bold;text-decoration:none;font-family:Arial,sans-serif;">
                  View Position &rarr;
                </a>
              </p>
            </td></tr>
          </table>
        </td></tr>"""

    total     = len(listings)
    scanned   = stats.get("scanned", 0)
    api_calls = stats.get("api_calls", 0)

    count_txt = (
        f"{total} relevant postdoc listing{'s' if total!=1 else ''} found today"
        if total > 0
        else "No new relevant listings today &mdash; boards checked, seen-list updated"
    )
    no_listings = """
        <tr><td style="padding:40px 0;text-align:center;color:#888;
                font-size:15px;font-family:Arial,sans-serif;">
          No new listings matched your profile today.<br>All boards have been checked.
        </td></tr>""" if not listings else ""

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f4f6f8;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f4f6f8">
<tr><td align="center" style="padding:30px 10px;">
<table width="640" cellpadding="0" cellspacing="0" border="0" style="max-width:640px;width:100%;">

  <tr><td style="background:#1F4E79;border-radius:8px 8px 0 0;padding:28px 32px;">
    <p style="margin:0;color:#fff;font-size:22px;font-weight:bold;">Daily Postdoc Digest</p>
    <p style="margin:6px 0 0;color:#aad4f5;font-size:14px;">
      Numerical Analysis &middot; Computational Mathematics &middot; Applied Mathematics
    </p>
    <p style="margin:4px 0 0;color:#cce4f7;font-size:13px;">{run_date}</p>
  </td></tr>

  <tr><td style="background:#2E75B6;padding:10px 32px;">
    <p style="margin:0;color:#fff;font-size:14px;font-weight:bold;">{count_txt}</p>
  </td></tr>

  <tr><td style="background:#EBF3FB;padding:8px 32px;border-bottom:1px solid #cde0f0;">
    <p style="margin:0;font-size:12px;color:#1F4E79;font-family:Arial,sans-serif;">
      AI-powered validation via Groq + Llama 3.1 (open-source, free) &nbsp;&middot;&nbsp;
      {scanned} listings scanned &nbsp;&middot;&nbsp;
      {api_calls} Groq API call{'s' if api_calls!=1 else ''} used &nbsp;&middot;&nbsp;
      {total} passed validation
    </p>
  </td></tr>

  <tr><td style="background:#f4f6f8;padding:20px 32px;">
    <table width="100%" cellpadding="0" cellspacing="0" border="0">
      {cards_html}
      {no_listings}
    </table>
  </td></tr>

  <tr><td style="background:#fff;border:1px solid #e0e0e0;padding:18px 32px;">
    <p style="margin:0 0 8px;font-size:13px;font-weight:bold;color:#333;">Quick Links</p>
    <p style="margin:0;font-size:13px;">
      <a href="https://www.mathjobs.org/jobs/list" style="color:#1F4E79;">mathjobs.org</a>
      &nbsp;&middot;&nbsp;
      <a href="https://mathhire.org/jobs/" style="color:#1D6A2E;">mathhire.org</a>
      &nbsp;&middot;&nbsp;
      <a href="https://academicpositions.com/find-jobs" style="color:#7D3C98;">academicpositions.com</a>
    </p>
  </td></tr>

  <tr><td style="background:#e8f0f7;border-radius:0 0 8px 8px;padding:14px 32px;text-align:center;">
    <p style="margin:0;font-size:12px;color:#888;font-family:Arial,sans-serif;">
      Postdoc Agent &middot; Powered by Groq + Llama 3.1 (open-source, free)<br>
      Validated against your researcher profile &mdash; not rigid keyword rules <br>
      Created by <a href="https://www.linkedin.com/in/victor-arowosaye/">Victor Arowosaye</a>
    </p>
  </td></tr>

</table>
</td></tr>
</table>
</body></html>"""

def build_logging_email(log_file_name: str) -> str:
    with open(log_file_name, "r", encoding="utf-8") as f:
        log_contents = f.read()
    # Append it to your HTML message inside a <pre> tag so it looks like code
        log_html = f"<h3>Daily Execution Logs:</h3><pre style='background:#f4f4f4; padding:10px;'>{log_contents}</pre>"

    return f"""<!DOCTYPE html>
                <html><head><meta charset="UTF-8"></head>
                <body style="margin:0;padding:0;background:#f4f6f8;font-family:Arial,sans-serif;">
                    {log_html}
                </body></html>
            """
# ─────────────────────────────────────────────
#  EMAIL SENDER
# ─────────────────────────────────────────────
def send_email(html: str, count: int, config: dict):
    sender    = config["sender_email"]
    recipient = config["recipient_email"]
    date_str  = datetime.now().strftime("%A %d %B %Y")
    subject   = (
        f"Postdoc Digest - {count} new listing{'s' if count!=1 else ''} - {date_str}"
        if count > 0
        else f"Postdoc Digest - No new listings - {date_str}"
    )
    if isinstance(recipient, (list, tuple)):
        for recp in recipient:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = f"Postdoc Agent <{sender}>"
            msg["To"]      = recp
            msg.attach(MIMEText(html, "html"))
            log.info(f"Sending digest to {recipient}...")
            with smtplib.SMTP("smtp.gmail.com", EMAIL_PORT, timeout=TIMEOUT) as server:
                server.ehlo()         # Say hello to the server
                server.starttls()     # Upgrade the connection to secure SSL/TLS
                server.login(sender, config["gmail_app_password"])
                server.send_message(msg)
            log.info(f"Email sent to {recp} successfully!")
    else:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"Postdoc Agent <{sender}>"
        msg["To"]      = recipient
        msg.attach(MIMEText(html, "html"))
        log.info(f"Sending digest to {recipient}...")
        with smtplib.SMTP("smtp.gmail.com", EMAIL_PORT, timeout=TIMEOUT) as server:
                server.ehlo()         # Say hello to the server
                server.starttls()     # Upgrade the connection to secure SSL/TLS
                server.login(sender, config["gmail_app_password"])
                server.send_message(msg)
        log.info(f"Email sent to {recipient} successfully!")

def send_log_email(html: str, email: str, config: dict):
    sender    = config["sender_email"]
    recipient = email
    date_str  = datetime.now().strftime("%A %d %B %Y")
    subject   = f"Log for - {date_str}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"Postdoc Log <{sender}>"
    msg["To"]      = recipient
    msg.attach(MIMEText(html, "html"))
    log.info(f"Sending Logs to {recipient}...")
    with smtplib.SMTP("smtp.gmail.com", EMAIL_PORT, timeout=TIMEOUT) as server:
            server.ehlo()         # Say hello to the server
            server.starttls()     # Upgrade the connection to secure SSL/TLS
            server.login(sender, config["gmail_app_password"])
            server.send_message(msg)
    log.info(f"Log Email sent to {recipient} successfully!")



# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    log.info("=" * 62)
    log.info("POSTDOC AGENT (AI-powered) — Daily run starting")
    log.info("=" * 62)

    cfg = YOUR_CONFIG

    missing = []
    if "your_groq_key" in cfg["groq_api_key"] or not cfg["groq_api_key"].startswith("gsk_"):
        missing.append("groq_api_key  ->  get free at console.groq.com")
    if "your.email" in cfg["sender_email"]:
        missing.append("sender_email")
    if "xxxx" in cfg["gmail_app_password"]:
        missing.append("gmail_app_password  ->  get at myaccount.google.com -> App passwords")
    if missing:
        print("\n" + "="*62)
        print("SETUP REQUIRED - fill these in YOUR_CONFIG at the top:")
        for m in missing:
            print(f"  * {m}")
        print("="*62 + "\n")
        return

    seen = load_seen()
    log.info(f"Loaded {len(seen)} previously seen listing IDs")

    # Fetch all boards
    raw = []
    raw.extend(fetch_mathjobs(seen, cfg["days_back"]))
    raw.extend(fetch_mathhire(seen))
    raw.extend(fetch_academicpositions(seen))
    raw.extend(fetch_academicpositions_playwright(seen))

    # Deduplicate
    raw = list({item["uid"]: item for item in raw}.values())
    log.info(f"Total unseen before AI validation: {len(raw)}")

    # AI validation
    validator = AIValidator(cfg)
    relevant  = validator.validate_all(raw)

    stats = {"scanned": len(raw), "api_calls": validator._calls}

    # Mark everything as seen
    for item in raw:
        seen.add(item["uid"])
    save_seen(seen)

    # Build and send
    run_date = datetime.now().strftime("%A, %d %B %Y - %I:%M %p")
    html     = build_email(relevant, run_date, stats)
    send_email(html, len(relevant), cfg)

    log.info("=" * 62)
    log.info(
        f"Done. {len(relevant)}/{len(raw)} listings relevant. "
        f"{validator._calls} Groq API call(s) used."
    )
    log.info("=" * 62)
    
    log_html = build_logging_email("daily.log")
    send_log_email(log_html, "victordman15@gmail.com", cfg)

if __name__ == "__main__":
    main()


# ================================================================
#  DEPLOYMENT GUIDE
# ================================================================
#
#  OPTION A: PythonAnywhere (Free, Recommended for automation)
#  -----------------------------------------------------------
#  1. Sign up free at pythonanywhere.com
#  2. Files tab -> Upload this script
#  3. Open a Bash console and run:
#       pip install requests beautifulsoup4 feedparser lxml groq
#  4. Test it once:
#       python3 postdoc_agent_ai.py
#  5. Tasks tab -> Add Scheduled Task:
#       Time:    07:00  (= 8:00am Nigerian WAT time)
#       Command: python3 /home/YOURUSERNAME/postdoc_agent_ai.py
#  6. Done - digest email arrives every morning automatically
#
#  OPTION B: Your own Linux or Mac machine (cron)
#  -----------------------------------------------
#  crontab -e
#  Add this line:
#    0 7 * * * python3 /path/to/postdoc_agent_ai.py >> /tmp/agent.log 2>&1
#
#  OPTION C: Windows Task Scheduler
#  -----------------------------------
#  Trigger: Daily at 07:00
#  Action:  python.exe  C:\path\to\postdoc_agent_ai.py
#
#  GROQ FREE TIER LIMITS (for reference)
#  ----------------------------------------
#  Model: llama-3.1-8b-instant
#    - 30 requests per minute
#    - 14,400 requests per day
#    - 1,000,000 tokens per day
#
#  This agent uses roughly 3-10 API calls per daily run.
#  That is less than 0.1% of your free daily allowance.
#
#  Want smarter (but slower) validation? Change groq_model to:
#    "llama3-70b-8192"    <- much more capable, still free
#    "mixtral-8x7b-32768" <- good balance of speed and intelligence
#
# ================================================================
