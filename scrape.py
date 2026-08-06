#!/usr/bin/env python3
"""
Scraper with Playwright fallback for meteli.net and VisitTampere integration.
"""
import json
import re
import sys
import time
import datetime
import random
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
        print(f"[{source}] FAILED: {exc} | body: {resp.text[:500]!r}", file=sys.stderr)
    else:
        print(f"[{source}] FAILED: {exc}", file=sys.stderr)


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
    if not text:
        return None
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", text)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(?!\d)", text)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        return f"{year_hint:04d}-{mo:02d}-{d:02d}"
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def _is_event_anchor(node):
    return getattr(node, "name", None) == "a" and "/tampere/tapahtuma/" in (node.get("href") or "")


def is_suspicious_heading(text):
    """Return True if text looks like a section/article heading rather than a venue."""
    if not text:
        return True
    text_l = text.lower()
    if re.search(r"\b(kesä|kesällä|home|info|article|artikkeli|kesän|dam|kesäkausi|artikkeleita|videos)\b", text_l, re.IGNORECASE):
        return True
    if len(text.strip()) > 60:
        return True
    return False


def _find_nearby_date(el, year):
    """Try to find a date near element el: check element text, parent, previous siblings, and preceding headings."""
    txt = el.get_text(" ", strip=True)
    d = parse_date(txt, year)
    if d:
        return d
    parent = el.find_parent(["li", "div", "article", "section"])
    if parent:
        pd = parse_date(parent.get_text(" ", strip=True), year)
        if pd:
            return pd
    sib = el.find_previous_sibling()
    tries = 0
    while sib and tries < 6:
        sd = parse_date(sib.get_text(" ", strip=True), year)
        if sd:
            return sd
        sib = sib.find_previous_sibling()
        tries += 1
    headings = el.find_all_previous(["h1", "h2", "h3", "h4"], limit=6)
    for h in headings:
        hd = parse_date(h.get_text(" ", strip=True), year)
        if hd:
            return hd
    return None


def _date_matches_month(date_str, year, month):
    try:
        y, m, d = map(int, date_str.split("-"))
    except Exception:
        return False
    return y == year and m == month


def _forward_adjacent_text(el):
    """Text immediately after this anchor, stopping at the next event
    anchor OR any block-level element — matches the common 'Title Venue,
    Tampere' inline convention without crossing into a neighboring event."""
    node = el.next_sibling
    if node is None or _is_event_anchor(node):
        return ""
    if getattr(node, "name", None) in {"p", "div", "li", "article", "section", "h1", "h2", "h3", "h4"}:
        return ""
    return node.get_text(" ", strip=True) if hasattr(node, "get_text") else str(node).strip()


def parse_month_page(html, year, month):
    soup = BeautifulSoup(html, "html.parser")
    events = []
    current_date = None
    excluded_count = 0
    # A short heading/paragraph naming a known venue (e.g. a festival name
    # introducing a lineup) is remembered as a hint for the VERY NEXT event
    # only, then cleared — unlike current_date, venue must not be sticky
    # across many events, or one festival name leaks onto everything until
    # the next heading (this was the actual reported bug: see chat).
    pending_venue_hint = None

    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "a", "li", "div"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue

        maybe_date = parse_date(text[:60], year)
        if maybe_date and len(text) < 120:
            current_date = maybe_date
            continue

        if el.name != "a":
            if len(text) < 60:
                for v in KNOWN_VENUES_SORTED:
                    if v.lower() in text.lower():
                        pending_venue_hint = v
                        break
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

            # 1) Text immediately after this anchor (most reliable: this is
            # this event's own inline description, can't belong to a
            # neighbor since we stop at the next anchor/block boundary).
            fwd = _forward_adjacent_text(el)
            for v in KNOWN_VENUES_SORTED:
                if v.lower() in fwd.lower():
                    venue = v
                    break
            if not venue and fwd:
                m = re.search(r"\b([A-ZÅÄÖ][\w &'\-]{2,40})\s*,\s*Tampere\b", fwd)
                if m:
                    candidate = m.group(1).strip()
                    if not is_suspicious_heading(candidate):
                        venue = candidate

            # 2) Otherwise, a heading naming a venue seen just before this
            # anchor (single-use — see pending_venue_hint comment above).
            if not venue and pending_venue_hint:
                venue = pending_venue_hint
            pending_venue_hint = None  # always consumed here, never carries to a 2nd event

            if venue and is_suspicious_heading(venue):
                print(f"[parse_month_page] suspicious venue `{venue}` for title `{title}` url={url}", file=sys.stderr)

            if any(kw in f"{title} {venue}".lower() for kw in EXCLUDE_KEYWORDS):
                excluded_count += 1
                continue

            # Determine date: prefer current_date from headings, otherwise try several nearby heuristics.
            date_str = current_date
            if not date_str:
                date_str = parse_date(title, year)
            if not date_str:
                date_str = _find_nearby_date(el, year)
            if not date_str:
                print(f"[parse_month_page] skipping anchor without reliable date: title={title!r} url={url}", file=sys.stderr)
                continue

            # Enforce date belongs to the month we are parsing
            if not _date_matches_month(date_str, year, month):
                print(f"[parse_month_page] skipping anchor because date {date_str} not in parsed month {year}-{month:02d}: title={title!r} url={url}", file=sys.stderr)
                continue

            time_str = parse_time(text)
            events.append({
                "date": date_str,
                "time": time_str,
                "title": title,
                "venue": venue or "Tampere",
                "genre": guess_genre(title, venue or "Tampere"),
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
        print(f"  filtered out {excluded_count} non-music listing(s)", file=sys.stderr)
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
            print(f"[fetch] attempt {attempt} for {url} failed: {exc}", file=sys.stderr)
            if attempt < retries:
                time.sleep(backoff * attempt)
            continue
    raise last_exc


def fetch_month(year, month):
    url = month_url(year, month)
    resp = fetch_with_retries("GET", url, headers=HEADERS, timeout=20, retries=3, backoff=1)
    return parse_month_page(resp.text, year, month)


# ---------------------------------------------------------------------------
# METELI.NET with Playwright fallback
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


def fetch_with_playwright_content(url, timeout=20000):
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError("playwright not available")
    try:
        with sync_playwright() as p:
            time.sleep(1 + random.random())
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-blink-features=AutomationControlled"], headless=True)
            context = browser.new_context(
                user_agent=HEADERS.get("User-Agent"),
                locale="fi-FI",
                extra_http_headers={"Accept-Language": HEADERS.get("Accept-Language", "fi-FI,fi;q=0.9")}
            )
            try:
                context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            except Exception:
                pass
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=timeout)
            try:
                page.wait_for_selector('a[href*="/tapahtuma/"]', timeout=10000)
            except Exception:
                pass
            content = page.content()
            context.close()
            browser.close()
            return content
    except Exception:
        raise


def fetch_meteli(max_pages=4):
    events = []
    today = datetime.date.today()
    use_scraper = cloudscraper is not None
    for page_num in range(1, max_pages + 1):
        url = METELI_TAMPERE_URL if page_num == 1 else f"{METELI_TAMPERE_URL}/page/{page_num}"
        try:
            resp = fetch_with_retries("GET", url, headers=HEADERS, timeout=20, retries=4, backoff=1, use_scraper=use_scraper)
            html = resp.text
        except Exception as exc:
            log_http_error(f"meteli page {page_num}", exc)
            if PLAYWRIGHT_AVAILABLE:
                try:
                    html = fetch_with_playwright_content(url)
                except Exception as exc2:
                    log_http_error(f"meteli playwright page {page_num}", exc2)
                    break
            else:
                break

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
        print(f"[meteli page {page_num}] parsed {found_this_page} events", file=sys.stderr)
        if found_this_page == 0:
            break
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
    m = re.search(r"(\d{4})[-\.](\d{1,2})[-\.](\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except Exception:
            pass
    m = re.search(r"\b(\d{1,2})[.\-\/](\d{1,2})[.\-\/](\d{4})\b", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except Exception:
            pass
    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b", s)
    if m:
        d, mon_name, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        months = {
            'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
            'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
            'january': 1, 'february': 2, 'march': 3, 'april': 4, 'may': 5, 'june': 6,
            'july': 7, 'august': 8, 'september': 9, 'october': 10, 'november': 11, 'december': 12,
            'tammikuu': 1, 'helmikuu': 2, 'maaliskuu': 3, 'huhtikuu': 4,
            'toukokuu': 5, 'kesakuu': 6, 'heinakuu': 7, 'elokuu': 8,
            'syyskuu': 9, 'lokakuu': 10, 'marraskuu': 11, 'joulukuu': 12,
        }
        mo = months.get(mon_name[:3]) or months.get(mon_name)
        if mo:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.search(r"\b([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\b", s)
    if m:
        mon_name, d, y = m.group(1).lower(), int(m.group(2)), int(m.group(3))
        months2 = {
            'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
            'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
            'january': 1, 'february': 2, 'march': 3, 'april': 4, 'may': 5, 'june': 6,
            'july': 7, 'august': 8, 'september': 9, 'october': 10, 'november': 11, 'december': 12,
        }
        mo = months2.get(mon_name[:3]) or months2.get(mon_name)
        if mo:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def _snippet_looks_like_date(snippet):
    if re.search(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|tammikuu|helmikuu|maaliskuu|huhtikuu|toukokuu|kesakuu|heinakuu|elokuu|syyskuu|lokakuu|marraskuu|joulukuu)\b", snippet, re.IGNORECASE):
        return True
    if re.search(r"\b\d{1,2}[.\-\/]\d{1,2}\b", snippet):
        return True
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", snippet):
        return True
    return False


def fetch_visittampere(url="https://visittampere.fi/en/articles/events-in-tampere/"):
    events = []
    try:
        resp = fetch_with_retries("GET", url, headers=HEADERS, timeout=20, retries=2, backoff=1)
    except Exception as exc:
        log_http_error("visittampere", exc)
        return events

    soup = BeautifulSoup(resp.text, "html.parser")

    candidates = []
    candidates.extend(soup.find_all("article"))
    candidates.extend(soup.select("[class*='card'], [class*='item'], [class*='article']"))
    for h in soup.find_all(["h2", "h3", "h4"]):
        a = h.find("a", href=True)
        if a:
            candidates.append(h)

    seen = set()
    for node in candidates:
        a = node.find("a", href=True)
        title = None
        url_ = None
        if a:
            title = a.get_text(" ", strip=True)
            url_ = urljoin(url, a["href"])
        else:
            h = node.find(["h2", "h3", "h4"])
            if h and h.get_text(strip=True):
                title = h.get_text(" ", strip=True)
        if not title:
            strong = node.find(["strong", "b"])
            if strong:
                title = strong.get_text(" ", strip=True)
        if not title:
            continue

        key = (title.lower(), url_ or "")
        if key in seen:
            continue
        seen.add(key)

        date_str = None
        date_from_time = False
        time_str = ""
        dt = node.find(["time", "span"], attrs={"datetime": True})
        if dt and dt.get("datetime"):
            date_str = parse_fuzzy_date(dt["datetime"])
            date_from_time = True

        if not date_str:
            text_snippets = []
            for el in node.find_all(["p", "div", "span", "li"], recursive=True):
                txt = el.get_text(" ", strip=True)
                if txt:
                    text_snippets.append(txt)
            for s in text_snippets:
                if not _snippet_looks_like_date(s):
                    continue
                d = parse_fuzzy_date(s)
                if d:
                    date_str = d
                    tm = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", s)
                    if tm:
                        time_str = f"{int(tm.group(1)):02d}:{tm.group(2)}"
                    date_from_time = False
                    break

        if not date_str and url_:
            try:
                art_resp = fetch_with_retries("GET", url_, headers=HEADERS, timeout=15, retries=1, backoff=1)
                art_soup = BeautifulSoup(art_resp.text, "html.parser")
                t = art_soup.find("time")
                if t and t.get("datetime"):
                    date_str = parse_fuzzy_date(t["datetime"])
                    date_from_time = True
                if not date_str:
                    for p in art_soup.find_all("p", limit=6):
                        ptxt = p.get_text(" ", strip=True)
                        if not _snippet_looks_like_date(ptxt):
                            continue
                        d = parse_fuzzy_date(ptxt)
                        if d:
                            date_str = d
                            tm = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", ptxt)
                            if tm:
                                time_str = f"{int(tm.group(1)):02d}:{ptxt[tm.end(1)+1:tm.end(2)]}"
                            date_from_time = False
                            break
            except Exception:
                pass

        if not date_str:
            print(f"[visittampere] skipping candidate without reliable date: title={title!r} url={url_}", file=sys.stderr)
            continue

        # If date was extracted from a paragraph (not a time element) require that the URL looks like an event page,
        # otherwise it's likely the article's publication date.
        if not date_from_time and url_ and "/tapahtuma/" not in url_.lower():
            print(f"[visittampere] skipping candidate where date likely publication date (no <time> and not /tapahtuma/): title={title!r} url={url_}", file=sys.stderr)
            continue

        ev = {
            "date": date_str,
            "time": time_str,
            "title": title,
            "venue": "Tampere",
            "genre": guess_genre(title, "Tampere"),
            "free": 0,
            "url": url_ or url,
        }
        events.append(ev)

    dedup = []
    seen_keys = set()
    for e in events:
        k = (e["date"], e["title"].lower())
        if k in seen_keys:
            continue
        seen_keys.add(k)
        dedup.append(e)

    print(f"[visittampere] parsed {len(dedup)} events", file=sys.stderr)
    return dedup


# keikat.org and linkedevents code follow the same approach:

KEIKAT_ORG_URL = "https://keikat.org/tampere"
KEIKAT_ORG_DATE_RE = re.compile(r"\d{1,2}\.\d{1,2}\.(\d{4})")


def parse_keikat_org_anchor_text(text):
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    rest = text[m.end():].strip(" ·-")
    time_str = parse_time(rest[:10])
    rest = re.sub(r"^\d{1,2}[:.]\d{2}\s*·?\s*", "", rest)
    rest = re.sub(r"\s*Liput\s*$", "", rest).strip()
    rest = re.sub(r"[\d,\.]+\s*€\s*$", "", rest).strip()
    half = len(rest) // 2
    if half > 3 and rest[:half].strip() == rest[half:].strip():
        rest = rest[:half].strip()
    title, venue = split_title_venue(rest)
    if not title or not venue:
        return None
    return {"date": f"{year:04d}-{month:02d}-{day:02d}", "time": time_str, "title": title, "venue": venue, "free": 0}


KEIKAT_DATE_HEAD_RE = re.compile(r"^[a-zäöå]{2}\s+(\d{1,2})\.(\d{1,2})\.(\d{4})", re.IGNORECASE)


def fetch_keikat_org(url=KEIKAT_ORG_URL):
    events = []
    try:
        resp = fetch_with_retries("GET", url, headers=HEADERS, timeout=20, retries=3, backoff=1)
    except Exception as exc:
        log_http_error("keikat.org", exc)
        return events

    soup = BeautifulSoup(resp.text, "html.parser")
    current_date = None

    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "a", "li", "div", "td", "tr"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue

        m = KEIKAT_DATE_HEAD_RE.match(text)
        if m and len(text) < 40:
            current_date = f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
            continue

        if el.name != "a":
            continue
        href = el.get("href", "")
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue

        parsed = parse_keikat_org_anchor_text(text)
        if parsed:
            title, venue, date_str = parsed["title"], parsed["venue"], parsed["date"]
        elif current_date and 2 < len(text) < 120:
            title, venue = split_title_venue(text)
            if not venue:
                continue
            date_str = current_date
        else:
            continue

        if any(kw in f"{title} {venue}".lower() for kw in EXCLUDE_KEYWORDS):
            continue
        events.append({
            "date": date_str, "time": "", "title": title, "venue": venue, "free": 0,
            "genre": guess_genre(title, venue), "url": urljoin(url, href),
        })

    seen = set()
    deduped = []
    for e in events:
        if e["url"] in seen:
            continue
        seen.add(e["url"])
        deduped.append(e)

    print(f"[keikat.org] parsed {len(deduped)} events", file=sys.stderr)
    return deduped


LINKEDEVENTS_URLS = [
    "https://linkedevents.tampere.fi/v1/event/",
    "http://linkedevents.tampere.fi/v1/event/",
]


def looks_like_music(title, venue):
    text = f"{title} {venue}".lower()
    if any(kw in text for kws in GENRE_KEYWORDS.values() for kw in kws):
        return True
    return any(v.lower() in venue.lower() for v in KNOWN_VENUES)


def fetch_linkedevents(days_ahead=45):
    events = []
    today = datetime.date.today()
    end = today + datetime.timedelta(days=days_ahead)
    params = {"start": today.isoformat(), "end": end.isoformat()}
    last_exc = None

    for base in LINKEDEVENTS_URLS:
        try:
            resp = fetch_with_retries("GET", base, headers=HEADERS, params=params, timeout=20, retries=3, backoff=1)
            payload = resp.json()
        except Exception as exc:
            last_exc = exc
            try:
                body = getattr(exc, "response", None).text if getattr(exc, "response", None) is not None else ""
            except Exception:
                body = ""
            if "Invalid host" in str(body) or "Invalid host" in str(exc):
                try:
                    headers = dict(HEADERS)
                    headers["Host"] = "linkedevents.tampere.fi"
                    resp = fetch_with_retries("GET", base, headers=headers, params=params, timeout=20, retries=2, backoff=1)
                    payload = resp.json()
                except Exception as exc2:
                    last_exc = exc2
                    continue
            else:
                continue

        for item in payload.get("data", []):
            try:
                name_field = item.get("name") or {}
                name = name_field.get("fi") or name_field.get("en") or ""
                loc_field = (item.get("location") or {}).get("name") or {}
                venue = loc_field.get("fi") or loc_field.get("en") or ""
                start_time = item.get("start_time") or ""
                if not name or len(start_time) < 10:
                    continue
                date_str = start_time[:10]
                time_str = start_time[11:16] if len(start_time) >= 16 else ""
                if any(kw in f"{name} {venue}".lower() for kw in EXCLUDE_KEYWORDS):
                    continue
                if not looks_like_music(name, venue):
                    continue
                events.append({
                    "date": date_str,
                    "time": time_str,
                    "title": name.strip(),
                    "venue": (venue or "Tampere").strip(),
                    "genre": guess_genre(name, venue),
                    "free": 0,
                    "url": item.get("info_url") or item.get("@id") or base,
                })
            except Exception:
                continue
        if events:
            break

    if not events and last_exc:
        log_http_error("linkedevents", last_exc)
    else:
        print(f"[linkedevents] parsed {len(events)} events", file=sys.stderr)
    return events


def main():
    today = datetime.date.today()
    kohokohdat_events = []
    meteli_events = []
    keikat_org_events = []
    linkedevents_events = []
    visittampere_events = []
    errors = []

    months_to_fetch = [(today.year, today.month)]
    nm = today.month + 1
    ny = today.year
    if nm > 12:
        nm = 1
        ny += 1
    months_to_fetch.append((ny, nm))

    for year, month in months_to_fetch:
        try:
            events = fetch_month(year, month)
            print(f"[kohokohdat {year}-{month:02d}] parsed {len(events)} events", file=sys.stderr)
            kohokohdat_events.extend(events)
        except Exception as exc:
            errors.append(f"kohokohdat {year}-{month:02d}: {exc}")
            print(f"[kohokohdat {year}-{month:02d}] FAILED: {exc}", file=sys.stderr)

    try:
        meteli_events = fetch_meteli(max_pages=4)
    except Exception as exc:
        errors.append(f"meteli: {exc}")
        print(f"[meteli] FAILED: {exc}", file=sys.stderr)

    try:
        keikat_org_events = fetch_keikat_org()
    except Exception as exc:
        errors.append(f"keikat.org: {exc}")
        print(f"[keikat.org] FAILED: {exc}", file=sys.stderr)

    try:
        linkedevents_events = fetch_linkedevents()
    except Exception as exc:
        errors.append(f"linkedevents: {exc}")
        print(f"[linkedevents] FAILED: {exc}", file=sys.stderr)

    try:
        visittampere_events = fetch_visittampere()
    except Exception as exc:
        errors.append(f"visittampere: {exc}")
        print(f"[visittampere] FAILED: {exc}", file=sys.stderr)

    all_events = merge_events(meteli_events, kohokohdat_events, keikat_org_events, linkedevents_events, visittampere_events)

    if not all_events:
        print("No events parsed from any source — leaving existing data.json untouched.", file=sys.stderr)
        sys.exit(1)

    all_events.sort(key=lambda e: (e["date"], e["time"] or "99:99"))
    raw = [[e["date"], e["time"], e["title"], e["venue"], e.get("genre", "rock"), e.get("free", 0), e.get("url", "")] for e in all_events]

    filtered = []
    for e in raw:
        date, time_s, title, venue, genre, free, url = e
        title_l = (title or "").lower()
        venue_l = (venue or "").lower()
        if not title or not venue:
            continue
        if len(venue) > 60:
            print(f"[filter] dropping because venue too long: {venue!r} title={title!r} url={url}", file=sys.stderr)
            continue
        if title_l == venue_l:
            print(f"[filter] dropping because title equals venue: {title!r}", file=sys.stderr)
            continue
        if re.search(r'\b(home|info|article|artikkeli|kesä|kesällä|dam|articles|events in tampere)\b', title_l, re.IGNORECASE):
            print(f"[filter] dropping heading-like title: {title!r}", file=sys.stderr)
            continue
        filtered.append(e)

    counts = {
        "meteli": len(meteli_events),
        "kohokohdat": len(kohokohdat_events),
        "keikat_org": len(keikat_org_events),
        "linkedevents": len(linkedevents_events),
        "visittampere": len(visittampere_events),
    }
    output = {
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "source_note": (
            f"Auto-scraped from 5 sources (raw counts: {counts}), merged to "
            f"{len(filtered)} events after de-duplication. Music gigs only \u2014 "
            f"theatre/comedy filtered out. Confidence varies by source."
        ),
        "errors": errors,
        "events": filtered,
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=1)

    print(f"Wrote {len(filtered)} events to data.json. Per-source raw counts: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
