#!/usr/bin/env python3
"""
Scraper with Playwright fallback and improved logging/robustness.

Changes:
- Use logging instead of prints
- Atomic write for data.json
- Log exceptions with tracebacks
- Reuse Playwright browser/context across multiple pages when falling back
"""
import json
import re
import sys
import time
import datetime
import random
import os
import tempfile
import logging
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# Optional cloudscraper
try:
    import cloudscraper
except Exception:
    cloudscraper = None

# Optional Playwright (sync API)
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except Exception:
    PLAYWRIGHT_AVAILABLE = False

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BASE = "https://kohokohdat.fi"
MONTH_SLUGS = {
    1: "tammikuu", 2: "helmikuu", 3: "maaliskuu", 4: "huhtikuu",
    5: "toukokuu", 6: "kesakuu", 7: "heinakuu", 8: "elokuu",
    9: "syyskuu", 10: "lokakuu", 11: "marraskuu", 12: "joulukuu",
}

GENRE_KEYWORDS = {
    "metal":      ["metal", "punk", "hardcore", "black metal", "death metal", "core"],
    "jazz":       ["jazz", "blues", "soul", "country", "swing"],
    "electronic": ["dj", "dj:t", "vinyyli", "disco", "techno", "electro", "silent disco"],
    "hiphop":     ["hip hop", "hiphop", "rap", "pop"],
    "folk":       ["folk", "reggae", "salsa", "latin", "afro", "world", "kansanmusiikki"],
    "classical":  ["klassinen", "sinfonia", "filharmonia", "ooppera", "orkesteri"],
    "festival":   ["festivaali", "festival"],
}
DEFAULT_GENRE = "rock"

EXCLUDE_KEYWORDS = [
    "teatterikesä", "telttalab", "näytelmä", "teatteriesitys",
    "komedian superilta", "stand up", "stand-up", "improvisaatioteatteri",
    "elokuvanäytös", "leffailta", "kirjailijavierailu", "kirjamessu",
    "taidenäyttely", "luento", "urheiluottelu", "jalkapallo-ottelu",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "fi-FI,fi;q=0.9,en;q=0.8",
}


def log_http_error(source, exc):
    resp = getattr(exc, "response", None)
    if resp is not None:
        log.error("[%s] FAILED: %s | body: %r", source, exc, resp.text[:500])
    else:
        log.error("[%s] FAILED: %s", source, exc)
    log.exception(exc)


def guess_genre(title, venue):
    text = f"{title} {venue}".lower()
    for genre, kws in GENRE_KEYWORDS.items():
        if any(kw in text for kw in kws):
            return genre
    return DEFAULT_GENRE


def month_url(year, month):
    slug = MONTH_SLUGS[month]
    return f"{BASE}/tampere/tapahtumat-tampere/keikat-tampere-{slug}/"


def parse_time(text):
    m = re.search(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", text)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return ""


def parse_date(text, year_hint):
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", text)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(?!\d)", text)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        return f"{year_hint:04d}-{mo:02d}-{d:02d}"
    return None


def parse_month_page(html, year, month):
    soup = BeautifulSoup(html, "html.parser")
    events = []
    current_date = None
    excluded_count = 0

    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "a", "li", "div"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue

        maybe_date = parse_date(text[:40], year)
        if maybe_date and len(text) < 60:
            current_date = maybe_date
            continue

        if el.name == "a":
            href = el.get("href", "")
            if "/tampere/tapahtuma/" not in href:
                continue
            title = text
            if not title or len(title) > 140:
                continue
            url = urljoin(BASE, href)
            venue = ""
            parent_text = el.find_parent(["li", "div", "article"])
            if parent_text:
                pt = parent_text.get_text(" ", strip=True)
                venue_match = re.search(r"Tampere\s+([A-ZÅÄÖ][\w &'\-]{2,40})", pt)
                if venue_match:
                    venue = venue_match.group(1).strip()
            if any(kw in f"{title} {venue}".lower() for kw in EXCLUDE_KEYWORDS):
                excluded_count += 1
                continue

            time_str = parse_time(text)
            date_str = current_date or f"{year:04d}-{month:02d}-01"
            events.append({
                "date": date_str,
                "time": time_str,
                "title": title,
                "venue": venue or "Tampere",
                "genre": guess_genre(title, venue),
                "free": 0,
                "url": url,
            })

    seen = set()
    deduped = []
    for e in events:
        if e["url"] in seen:
            continue
        seen.add(e["url"])
        deduped.append(e)

    if excluded_count:
        log.info("filtered out %d non-music listing(s)", excluded_count)
    return deduped


def fetch_with_retries(method, url, *, headers=None, params=None, timeout=20, retries=3, backoff=1, allow_redirects=True, use_scraper=False):
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            if use_scraper and cloudscraper is not None:
                scraper = cloudscraper.create_scraper()
                resp = scraper.get(url, headers=headers or HEADERS, params=params, timeout=timeout, allow_redirects=allow_redirects)
            else:
                resp = requests.request(method, url, headers=headers or HEADERS, params=params, timeout=timeout, allow_redirects=allow_redirects)
            resp.raise_for_status()
            return resp
        except Exception as exc:
            last_exc = exc
            log.warning("[fetch] attempt %d for %s failed: %s", attempt, url, exc)
            log.debug("Exception details:", exc_info=True)
            if attempt < retries:
                time.sleep(backoff * attempt)
            continue
    # raise the last exception so caller can decide
    raise last_exc


def fetch_month(year, month):
    url = month_url(year, month)
    resp = fetch_with_retries("GET", url, headers=HEADERS, timeout=20, retries=3, backoff=1)
    return parse_month_page(resp.text, year, month)


# ---------------------------------------------------------------------------
# METELI.NET with Playwright fallback (with browser reuse)
# ---------------------------------------------------------------------------
METELI_BASE = "https://www.meteli.net"
METELI_TAMPERE_URL = "https://www.meteli.net/kaupunki/tampere"
METELI_LINK_RE = re.compile(
    r"^[A-ZÅÄÖ]{2}\s+(\d{1,2})\.(\d{1,2})\.\s*(?:meteli dummy\s*)?(.+)$",
    re.IGNORECASE,
)

KNOWN_VENUES = [
    "G Livelab Tampere", "Vastavirta-Klubi", "Vastavirta-klubi", "Paapan Kapakka",
    "Telakka", "Tavara-asema", "Bar Kotelo", "Pethaus", "Ruby & Fellas",
    "John Scott's Ratina", "Tampere-talo", "Tampereen stadion", "Tähti Areena",
    "Liikelaituri", "Kalevan kirkko", "Pub Simon", "O'Connell's Irish Bar",
    "O'Connell's", "Ilona", "Bar Ihku", "Axu Katubaari", "Paja Bar", "Armo",
    "Maanalainen", "Varjobaari", "Moro Sky Bar", "Gastropub Soho",
    "Winebistro 1910", "Ravintola Muusa", "Atmos Taproom",
    "Kulttuuriravintola Railo", "Tallipiha", "Tammelan Stadion",
    "Järvensivunpuisto", "Uittotunneli", "Laikunlava", "Tahmelan Huvila",
    "Pub Kujakolli", "Panimoravintola Plevna", "Saivo Kitchen & Bar",
    "Sisko ja sen Veli", "Mustalahden satama", "Ratinan stadion",
    "Ratinanniemen festivaalipuisto", "Sorsapuisto", "Finlaysonin Palatsi",
    "Väinö Linnan aukio", "Tyrvään Pappila", "Kuudes Linja", "Semifinal",
    "Kulttuuritehdas Korjaamo", "Tavastia-klubi", "Nekala", "Ylöjärven kaupungintalo",
]
KNOWN_VENUES_SORTED = sorted(KNOWN_VENUES, key=len, reverse=True)


def split_title_venue(rest):
    for v in KNOWN_VENUES_SORTED:
        suffix = f"{v}, Tampere"
        if rest.lower().endswith(suffix.lower()):
            return rest[: -len(suffix)].strip(" -–:"), v
    for v in KNOWN_VENUES_SORTED:
        if rest.lower().endswith(v.lower()) and len(rest) > len(v):
            return rest[: -len(v)].strip(" -–:"), v
    m = re.search(r"(?P<venue>[A-ZÅÄÖ0-9][\w .'&\-]*?)\s*,\s*Tampere\s*$", rest)
    if m:
        return rest[:m.start()].strip(" -–:"), m.group("venue").strip()
    return None, None


def parse_meteli_anchor_text(text, year_hint, today):
    m = METELI_LINK_RE.match(text.strip())
    if not m:
        return None
    day, month, rest = int(m.group(1)), int(m.group(2)), m.group(3)
    year = year_hint
    try:
        candidate = datetime.date(year, month, day)
        if candidate < today - datetime.timedelta(days=3):
            year += 1
    except ValueError:
        return None

    rest = re.sub(r"\s*Löydä liput\s*$", "", rest).strip()
    free = 0
    price_match = re.search(r"-\s*alk\.\s*([\d,\.\/\-]+)\s*€", rest)
    if price_match:
        rest = rest[:price_match.start()].strip()
        if price_match.group(1).strip().rstrip(",0.") == "" or price_match.group(1).strip() == "0":
            free = 1

    title, venue = split_title_venue(rest)
    if not venue or not title:
        return None

    return {
        "date": f"{year:04d}-{month:02d}-{day:02d}",
        "time": "",
        "title": title,
        "venue": venue,
        "free": free,
    }


def fetch_meteli(max_pages=4):
    """Fetch meteli pages, using requests/cloudscraper first and reusing a Playwright browser if needed.
    Returns list of events.
    """
    events = []
    today = datetime.date.today()
    use_scraper = cloudscraper is not None

    playwright = None
    browser = None
    context = None

    try:
        for page_num in range(1, max_pages + 1):
            url = METELI_TAMPERE_URL if page_num == 1 else f"{METELI_TAMPERE_URL}/page/{page_num}"
            html = None
            try:
                resp = fetch_with_retries("GET", url, headers=HEADERS, timeout=20, retries=4, backoff=1, use_scraper=use_scraper)
                html = resp.text
            except Exception as exc:
                log_http_error(f"meteli page {page_num}", exc)
                if PLAYWRIGHT_AVAILABLE:
                    # start playwright/browser/context lazily and reuse for subsequent pages
                    if playwright is None:
                        try:
                            playwright = sync_playwright().start()
                            # small randomized sleep to be less bot-like
                            time.sleep(1 + random.random())
                            browser = playwright.chromium.launch(args=["--no-sandbox", "--disable-blink-features=AutomationControlled"], headless=True)
                            context = browser.new_context(
                                user_agent=HEADERS.get("User-Agent"),
                                locale="fi-FI",
                                extra_http_headers={"Accept-Language": HEADERS.get("Accept-Language", "fi-FI,fi;q=0.9")},
                            )
                            try:
                                context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
                            except Exception:
                                # ignore if not supported
                                pass
                        except Exception as exc2:
                            log.exception("failed to start playwright/browser/context: %s", exc2)
                            break

                    try:
                        page = context.new_page()
                        # small randomized sleep between Playwright navigations
                        time.sleep(0.5 + random.random() * 0.5)
                        page.goto(url, wait_until="networkidle", timeout=20000)
                        try:
                            page.wait_for_selector('a[href*="/tapahtuma/"]', timeout=10000)
                        except Exception:
                            # continue even if selector not found
                            log.debug("meteli: selector not found on %s", url)
                        html = page.content()
                        try:
                            page.close()
                        except Exception:
                            pass
                    except Exception as exc2:
                        log.exception("meteli playwright page %s failed: %s", page_num, exc2)
                        break
                else:
                    break

            if not html:
                continue

            soup = BeautifulSoup(html, "html.parser")
            found_this_page = 0
            for a in soup.find_all("a", href=True):
                if "/tapahtuma/" not in a["href"]:
                    continue
                text = a.get_text(" ", strip=True)
                parsed = parse_meteli_anchor_text(text, today.year, today)
                if not parsed:
                    continue
                if any(kw in f"{parsed['title']} {parsed['venue']}".lower() for kw in EXCLUDE_KEYWORDS):
                    continue
                parsed["genre"] = guess_genre(parsed["title"], parsed["venue"])
                parsed["url"] = urljoin(METELI_BASE, a["href"])
                events.append(parsed)
                found_this_page += 1
            log.info("[meteli page %d] parsed %d events", page_num, found_this_page)
            if found_this_page == 0:
                break
    finally:
        # cleanup playwright resources if started
        try:
            if context is not None:
                context.close()
        except Exception:
            log.debug("error closing context", exc_info=True)
        try:
            if browser is not None:
                browser.close()
        except Exception:
            log.debug("error closing browser", exc_info=True)
        try:
            if playwright is not None:
                playwright.stop()
        except Exception:
            log.debug("error stopping playwright", exc_info=True)

    return events


def normalize_title(title):
    return re.sub(r"[^a-z0-9]", "", title.lower())[:40]


def merge_events(*event_lists):
    seen = {}
    merged = []
    for events in event_lists:
        for e in events:
            key = (e["date"], normalize_title(e["title"]))
            if key in seen:
                continue
            seen[key] = True
            merged.append(e)
    return merged


# ---------------------------------------------------------------------------
# VisitTampere scraper
# ---------------------------------------------------------------------------

def parse_fuzzy_date(s):
    """Try several human-friendly date formats and return YYYY-MM-DD or None."""
    if not s:
        return None
    s = s.strip()
    # 1) ISO-like 2026-08-05 or 2026.08.05
    m = re.search(r"(\d{4})[-\.](\d{1,2})[-\.](\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except Exception:
            pass
    # 2) DD.MM.YYYY or D.M.YYYY
    m = re.search(r"\b(\d{1,2})[.\-\/](\d{1,2})[.\-\/](\d{4})\b", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except Exception:
            pass
    # 3) DD Month YYYY  (e.g., 21 August 2026)
    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b", s)
    if m:
        d, mon_name, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        months = {
            'january':1,'february':2,'march':3,'april':4,'may':5,'june':6,
            'july':7,'august':8,'september':9,'october':10,'november':11,'december':12
        }
        mo = months.get(mon_name[:3]) or months.get(mon_name)
        if mo:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    # 4) Month DD, YYYY (e.g., August 21, 2026)
    m = re.search(r"\b([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\b", s)
    if m:
        mon_name, d, y = m.group(1).lower(), int(m.group(2)), int(m.group(3))
        months = {
            'january':1,'february':2,'march':3,'april':4,'may':5,'june':6,
            'july':7,'august':8,'september':9,'october':10,'november':11,'december':12
        }
        mo = months.get(mon_name[:3]) or months.get(mon_name)
        if mo:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def fetch_visittampere(url="https://visittampere.fi/en/articles/events-in-tampere/"):
    """
