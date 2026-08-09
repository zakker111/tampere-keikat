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


def _recover_leaked_time(title, venue, time_str):
    """A venue starting with 1-2 bare digits is never real (no venue name
    starts with a number) \u2014 it's leaked time debris from wherever the real
    corruption happens (couldn't pin down the exact cause without real HTML
    access to keikat.org, see chat). If the title also ends in 1-2 bare
    digits right where it got cut, these are almost certainly the two
    halves of a HH:MM time that lost its separator during text extraction.
    Reassemble them instead of just discarding the event."""
    if not venue or not title:
        return title, venue, time_str
    m_venue = re.match(r'^(\d{1,2})\s+(\S.*)$', venue)
    if not m_venue:
        return title, venue, time_str
    m_title = re.search(r'(\d{1,2})\s*$', title)
    if m_title:
        hh, mm = m_title.group(1), m_venue.group(1)
        if len(mm) == 2:
            recovered_time = f"{int(hh):02d}:{mm}"
            new_title = title[:m_title.start()].strip(" -\u2013:")
            new_venue = m_venue.group(2).strip()
            return new_title, new_venue, time_str or recovered_time
    # Couldn't pair it with a title-side fragment \u2014 still strip the leaked
    # digits from venue so a recoverable event isn't dropped over noise.
    return title, m_venue.group(2).strip(), time_str


def guess_genre(title, venue):
    text = f"{title} {venue}".lower()
    for genre, kws in GENRE_KEYWORDS.items():
        if any(kw in text for kw in kws):
            return genre
    return DEFAULT_GENRE


def venue_looks_valid(venue):
    """Rejects garbled venue splits (e.g. '00 Tampereen Messu...' from a
    mis-parsed time like '20:00') that individual parsers might still let
    through. Applied as a last line of defense in main()'s post-filter too."""
    if not venue or len(venue.strip()) < 2:
        return False
    v = venue.strip()
    if v[0].isdigit():
        return False
    if not re.search(r"[a-zA-ZåäöÅÄÖ]", v):
        return False
    return True


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


def parse_date_with_year(text):
    """Only matches a date that includes an explicit 4-digit year (e.g.
    'torstai 6.8.2026'). Used for the STICKY section-heading tracker \u2014
    real day headings on kohokohdat include the year; per-event date/time
    stamps like 'pe 7.8.' or 'to 6.8. - la 8.8. 16:00' don't. Treating both
    as equally sticky was the actual bug behind entire months piling onto
    one date: a yearless per-event stamp would overwrite the broader
    section tracker, and for a multi-day range like 'to 6.8. - la 8.8.' only
    the first date got captured and then stuck around for whatever came
    next. See parse_month_page for how yearless stamps are handled instead
    (single-use hint for the next event only, not sticky)."""
    if not text:
        return None
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", text)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{y:04d}-{mo:02d}-{d:02d}"


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
    """Text following this anchor that still belongs to THIS event card
    (e.g. a 'Tampere Venue Name' line right after the title). Skips
    insignificant whitespace-only text nodes between tags \u2014 without that,
    this returned empty for every event on kohokohdat's real markup, since
    the actual next sibling there is just whitespace before the venue div,
    not the venue div itself (that's what caused venues to come out
    off-by-one: pending_venue_hint was the only thing left to fall back on,
    but that reflects the PREVIOUS event's venue line, not this one's).
    Still refuses to follow into a block that itself contains another
    /tampere/tapahtuma/ link, since that means we've crossed into the next
    event's card rather than still being in this one's."""
    node = el.next_sibling
    hops = 0
    while node is not None and hops < 5:
        hops += 1
        if _is_event_anchor(node):
            return ""
        name = getattr(node, "name", None)
        if name is None:
            s = str(node).strip()
            if s:
                return s
            node = node.next_sibling
            continue
        if hasattr(node, "find") and node.find("a", href=lambda h: h and "/tampere/tapahtuma/" in h):
            return ""  # this block contains a different event's title link
        return node.get_text(" ", strip=True) if hasattr(node, "get_text") else ""
    return ""


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
    pending_date_hint = None
    pending_time_hint = ""

    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "a", "li", "div"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue

        # Real section heading (has a year, e.g. "torstai 6.8.2026") \u2014
        # sticky, applies to every event until the next one of these.
        heading_date = parse_date_with_year(text[:60])
        if heading_date and len(text) < 120:
            current_date = heading_date
            pending_date_hint = None  # a new day section starts fresh
            pending_time_hint = ""
            continue

        # Per-event date/time stamp with no year (e.g. "pe 7.8." or
        # "to 6.8. - la 8.8.   16:00") \u2014 single-use, applies only to the
        # very next event anchor, then cleared. NOT sticky \u2014 this is
        # exactly the distinction that was missing before.
        if el.name != "a" and len(text) < 60:
            yearless_date = parse_date(text, year)
            if yearless_date:
                pending_date_hint = yearless_date
                tm = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text)
                pending_time_hint = f"{int(tm.group(1)):02d}:{tm.group(2)}" if tm else ""
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

            # 1c) Not whitelisted, no ", Tampere" suffix, but fwd still
            # looks like "<city> <venue name>" (kohokohdat prefixes every
            # event's venue line with its municipality, e.g. "Vesilahti
            # Laukon kartano"). Strip the known city prefix and use the
            # rest \u2014 this is still THIS event's own text, so it's more
            # trustworthy than falling back to pending_venue_hint below,
            # which reflects a DIFFERENT event and was misattributing
            # venues whenever the real one just wasn't whitelisted yet.
            if not venue and fwd and len(fwd) < 60:
                city_m = re.match(r"^(Tampere|Vesilahti|Yl[öo]j[äa]rvi|Nokia|Kangasala|Pirkkala|Valkeakoski|Sastamala|Orivesi|Lempäälä|Ikaalinen|Ylöjärvi)\s+(\S.*)$", fwd, re.IGNORECASE)
                if city_m:
                    candidate = city_m.group(2).strip()
                    if candidate and not is_suspicious_heading(candidate):
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

            # Determine date: a per-event hint (e.g. "pe 7.8.") is more
            # specific than the broad section heading, so it wins when present.
            date_str = pending_date_hint or current_date
            event_time_hint = pending_time_hint
            pending_date_hint = None  # single-use, consumed here
            pending_time_hint = ""
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

            time_str = parse_time(text) or event_time_hint
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


def _looks_like_stuck_date_tracking(events, year, month):
    """Detects the failure mode where current_date tracking finds one real
    date heading then silently fails to update for the rest of the page,
    piling every remaining event onto that single date. This showed up on
    kohokohdat's September page (all events landing on 2026-09-02) despite
    August working fine \u2014 same parser, different month's markup apparently
    trips it. Can't inspect the real HTML to fix the root cause (network
    blocked from the sandbox that built this), so this catches the *symptom*
    instead: if effectively all of a month's events share one date, that's
    not a real gig calendar, that's a bug."""
    if len(events) < 12:
        return False
    from collections import Counter
    counts = Counter(e["date"] for e in events)
    top_date, top_count = counts.most_common(1)[0]
    return top_count / len(events) > 0.6


def fetch_month(year, month):
    url = month_url(year, month)
    resp = fetch_with_retries("GET", url, headers=HEADERS, timeout=20, retries=3, backoff=1)
    events = parse_month_page(resp.text, year, month)
    if _looks_like_stuck_date_tracking(events, year, month):
        print(f"[kohokohdat {year}-{month:02d}] REJECTING all {len(events)} events \u2014 "
              f"date tracking looks stuck on one day (see _looks_like_stuck_date_tracking). "
              f"This month's data needs a human to check the real page.", file=sys.stderr)
        return []
    return events


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
    "Tampereen Messu- ja Urheilukeskus", "Ratinanniemi", "Hakametsän jäähalli",
    "Spiral Tampere", "Sorin Sirkus", "Vooninki", "Hiedanrannan Tehdasaukio",
    "Teatteri Telakka", "TTT-Klubi", "Kulttuuritalo Laikku", "Pyynikki",
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


def fetch_with_playwright_content(url, timeout=25000):
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError("playwright not available")
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
        try:
            # networkidle never fires on a Cloudflare "Just a moment..."
            # challenge page \u2014 it has persistent background JS activity by
            # design, so networkidle guarantees a timeout every time the
            # challenge appears rather than just occasionally. domcontentloaded
            # fires immediately regardless; the actual gate we care about is
            # real event links showing up, which the selector wait below checks.
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            try:
                page.wait_for_selector('a[href*="/tapahtuma/"]', timeout=25000)
            except Exception:
                pass  # checked explicitly below instead of assuming success
            content = page.content()
        finally:
            context.close()
            browser.close()

    if "Just a moment" in content or "/tapahtuma/" not in content:
        raise RuntimeError("playwright fetch returned a challenge/empty page, not real content")
    return content


def fetch_with_playwright_retries(url, attempts=2):
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return fetch_with_playwright_content(url)
        except Exception as exc:
            last_exc = exc
            print(f"[playwright] attempt {attempt}/{attempts} for {url} failed: {exc}", file=sys.stderr)
    raise last_exc


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
                    html = fetch_with_playwright_retries(url)
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


NAV_TITLE_BLACKLIST = [
    "contact information", "accommodation", "for media", "eat and drink",
    "see and do", "map", "professionals", "for travel industry professionals",
    "top attractions", "articles", "destinations", "visibility on",
    "a day in tampere", "summer cruises", "cafés in tampere",
    "top tips for summer",
]


def fetch_visittampere(url="https://visittampere.fi/en/events/"):
    # Real fix (previous version pointed at the blog-style "/en/articles/
    # events-in-tampere/" page, which is prose, not a card listing \u2014 that's
    # why it only ever found generic page elements). Confirmed via search
    # that the real per-event pages live under /en/events/ (e.g.
    # /en/events/tove-festivaali/), so the sanity-check below now requires
    # "/events/" in the URL instead of the old "/tapahtuma/" check, which
    # was checking for kohokohdat's URL pattern on the wrong site \u2014 that
    # bug is exactly why "Contact information" slipped through before.
    # Still unverified against the live site (no internet access in the
    # sandbox that built this), so keep an eye on its raw count in
    # source_note after the first real run.
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
        if any(bad in title.lower() for bad in NAV_TITLE_BLACKLIST):
            continue

        key = (title.lower(), url_ or "")
        if key in seen:
            continue
        seen.add(key)

        # Positive signal required regardless of where the date came from:
        # this site's real event pages live under /events/. No URL match,
        # no event \u2014 this replaces the old date_from_time bypass that let
        # non-event pages through.
        if not url_ or "/events/" not in url_.lower():
            continue

        date_str = None
        time_str = ""
        dt = node.find(["time", "span"], attrs={"datetime": True})
        if dt and dt.get("datetime"):
            date_str = parse_fuzzy_date(dt["datetime"])

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
                    break

        if not date_str:
            try:
                art_resp = fetch_with_retries("GET", url_, headers=HEADERS, timeout=15, retries=1, backoff=1)
                art_soup = BeautifulSoup(art_resp.text, "html.parser")
                t = art_soup.find("time")
                if t and t.get("datetime"):
                    date_str = parse_fuzzy_date(t["datetime"])
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
                                time_str = f"{int(tm.group(1)):02d}:{tm.group(2)}"
                            break
            except Exception:
                pass

        if not date_str:
            print(f"[visittampere] skipping candidate without reliable date: title={title!r} url={url_}", file=sys.stderr)
            continue

        if any(kw in title.lower() for kw in EXCLUDE_KEYWORDS):
            continue

        events.append({
            "date": date_str,
            "time": time_str,
            "title": title,
            "venue": "Tampere",
            "genre": guess_genre(title, "Tampere"),
            "free": 0,
            "url": url_,
        })

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


def _fetch_visittampere_impl(url="https://visittampere.fi/en/articles/events-in-tampere/"):
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
            time_str = parsed.get("time", "")
        elif current_date and 2 < len(text) < 120:
            title, venue = split_title_venue(text)
            if not venue:
                continue
            date_str = current_date
            time_str = ""
        else:
            continue

        title, venue, time_str = _recover_leaked_time(title, venue, time_str)
        if not venue_looks_valid(venue):
            print(f"[keikat.org] dropping unrecoverable venue: {venue!r} title={title!r}", file=sys.stderr)
            continue

        if any(kw in f"{title} {venue}".lower() for kw in EXCLUDE_KEYWORDS):
            continue
        events.append({
            "date": date_str, "time": time_str, "title": title, "venue": venue, "free": 0,
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



# ---------------------------------------------------------------------------
# KEIKAT.LIVE — Tampere city calendar
# ---------------------------------------------------------------------------
KEIKAT_LIVE_URL = "https://keikat.live/kaupunki/tampere"
KEIKAT_LIVE_DATE_RE = re.compile(
    r"^(\d{1,2})\.(\d{1,2})\.(?:[A-Za-zÄÖÅäöå]{2})\b", re.IGNORECASE
)
KEIKAT_LIVE_TIME_RE = re.compile(r"\bklo\s*([01]?\d|2[0-3])\.([0-5]\d)\b", re.IGNORECASE)
KEIKAT_LIVE_YEAR_RE = re.compile(r"#\s*Tampere\s+keikat\s+(\d{4})", re.IGNORECASE)
KEIKAT_LIVE_FREE_RE = re.compile(r"\bIlmainen\b", re.IGNORECASE)
KEIKAT_LIVE_HREF_RE = re.compile(r"/(?:keikka|tapahtuma)/", re.IGNORECASE)


def _keikat_live_repeated_title(before_time):
    """Recover title/venue from the compact card text used by keikat.live.

    Cards commonly render as:
        TITLE [genre] TITLE VENUE klo 19.00 [Ilmainen]
    The title is therefore repeated. Find the longest useful repeated piece
    and treat everything after its second occurrence as the venue.
    """
    text = re.sub(r"\s+", " ", before_time).strip()
    if not text:
        return "", ""

    # Remove trailing price/age metadata that can sit after the venue.
    text = re.sub(r"\s*(?:\d+[,.]?\d*\s*€|K\s*-?\s*18|K18)\s*$", "", text, flags=re.I).strip()

    words = text.split()
    if len(words) < 3:
        return "", ""

    best = None
    # Find the longest contiguous prefix/subsequence that appears again later.
    # Limiting the first part avoids expensive O(n^3) behavior on long titles.
    max_words = min(25, len(words) // 2 + 8)
    for n in range(max_words, 1, -1):
        candidate = " ".join(words[:n])
        for j in range(1, len(words) - n + 1):
            if " ".join(words[j:j+n]).casefold() == candidate.casefold():
                venue = " ".join(words[j+n:]).strip()
                if venue:
                    best = (candidate, venue)
                    break
        if best:
            break

    if best:
        return best

    # The first token can be a genre label, so try to find any long repeated
    # run and choose the occurrence whose remainder is a plausible venue.
    for start in range(1, min(8, len(words))):
        remaining = words[start:]
        for n in range(min(20, len(remaining)//2), 1, -1):
            candidate = " ".join(remaining[:n])
            for j in range(n, len(remaining) - n + 1):
                if " ".join(remaining[j:j+n]).casefold() == candidate.casefold():
                    venue = " ".join(remaining[j+n:]).strip()
                    if venue:
                        return candidate, venue
    return "", ""


def _parse_keikat_live_card(text, date_str, url):
    tm = KEIKAT_LIVE_TIME_RE.search(text)
    if not tm or not date_str:
        return None

    before_time = text[:tm.start()].strip()
    title, venue = _keikat_live_repeated_title(before_time)
    if not title or not venue:
        return None

    # Clean known display metadata accidentally left around the venue.
    venue = re.sub(r"\b(?:Ilmainen|K-18|K18)\b", "", venue, flags=re.I).strip(" -–·")
    title = title.strip(" -–·")
    if not title or not venue or not venue_looks_valid(venue):
        return None

    if any(kw in f"{title} {venue}".lower() for kw in EXCLUDE_KEYWORDS):
        return None

    return {
        "date": date_str,
        "time": f"{int(tm.group(1)):02d}:{tm.group(2)}",
        "title": title,
        "venue": venue,
        "genre": guess_genre(title, venue),
        "free": 1 if KEIKAT_LIVE_FREE_RE.search(text) else 0,
        "url": url,
    }


def fetch_keikat_live(url=KEIKAT_LIVE_URL):
    """Scrape the Tampere city calendar without opening individual events.

    The page is already city-scoped, so every qualifying event card belongs to
    Tampere. We only need a date heading plus an event card containing 'klo'.
    This is intentionally independent from the other source parsers.
    """
    try:
        resp = fetch_with_retries("GET", url, headers=HEADERS, timeout=20, retries=2, backoff=1)
    except Exception as exc:
        log_http_error("keikat_live", exc)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    year = datetime.date.today().year
    m_year = KEIKAT_LIVE_YEAR_RE.search(soup.get_text(" ", strip=True))
    if m_year:
        year = int(m_year.group(1))

    events = []
    current_date = None
    seen_urls = set()
    card_count = 0

    # Walk the document in order. The live page has compact date headings such
    # as '11.8.TI 1 keikka', followed by anchors containing the event card.
    for el in soup.find_all(["h1", "h2", "h3", "h4", "a", "div", "li"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue

        dm = re.match(r"^(\d{1,2})\.(\d{1,2})\.(?:[A-Za-zÄÖÅäöå]{2})\b", text, re.I)
        if dm and len(text) < 60:
            day, month = int(dm.group(1)), int(dm.group(2))
            try:
                current_date = f"{year:04d}-{month:02d}-{day:02d}"
            except ValueError:
                current_date = None
            continue

        if el.name != "a" or not current_date:
            continue
        if not KEIKAT_LIVE_TIME_RE.search(text):
            continue

        href = el.get("href", "")
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        full_url = urljoin(url, href)
        if full_url in seen_urls:
            continue

        # Require an event-like link when the site provides one. If the site
        # changes its slug, the time-bearing anchor is still accepted.
        if "keikat.live" not in full_url:
            continue

        parsed = _parse_keikat_live_card(text, current_date, full_url)
        if not parsed:
            continue
        seen_urls.add(full_url)
        events.append(parsed)
        card_count += 1

    # Fallback: some HTML versions put the clickable event URL on a wrapper
    # while the text-bearing anchor is nested. Scan all anchors once more using
    # their parent card text, but still require a time and current date.
    if not events:
        current_date = None
        for el in soup.find_all(["h1", "h2", "h3", "h4", "a", "div", "li"]):
            text = el.get_text(" ", strip=True)
            if not text:
                continue
            dm = re.match(r"^(\d{1,2})\.(\d{1,2})\.(?:[A-Za-zÄÖÅäöå]{2})\b", text, re.I)
            if dm and len(text) < 60:
                current_date = f"{year:04d}-{int(dm.group(2)):02d}-{int(dm.group(1)):02d}"
                continue
            if el.name != "a" or not current_date:
                continue
            tm = KEIKAT_LIVE_TIME_RE.search(text)
            if not tm:
                continue
            href = el.get("href", "")
            if not href:
                continue
            full_url = urljoin(url, href)
            if full_url in seen_urls:
                continue
            parsed = _parse_keikat_live_card(text, current_date, full_url)
            if parsed:
                seen_urls.add(full_url)
                events.append(parsed)

    print(f"[keikat_live] parsed {len(events)} events from Tampere calendar", file=sys.stderr)
    if not events:
        print(f"[keikat_live] NO EVENTS: page fetched successfully but no event cards matched (card candidates with time were {card_count})", file=sys.stderr)
    return events

def main():
    today = datetime.date.today()
    kohokohdat_events = []
    meteli_events = []
    keikat_org_events = []
    linkedevents_events = []
    visittampere_events = []
    keikat_live_events = []
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

    try:
        keikat_live_events = fetch_keikat_live()
    except Exception as exc:
        errors.append(f"keikat_live: {exc}")
        print(f"[keikat_live] FAILED: {exc}", file=sys.stderr)

    all_events = merge_events(meteli_events, kohokohdat_events, keikat_org_events, linkedevents_events, visittampere_events, keikat_live_events)

    if not all_events:
        print("No events parsed from any source — leaving existing data.json untouched.", file=sys.stderr)
        sys.exit(1)

    all_events.sort(key=lambda e: (e["date"], e["time"] or "99:99"))
    raw = [[e["date"], e["time"], e["title"], e["venue"], e.get("genre", "rock"), e.get("free", 0), e.get("url", "")] for e in all_events]

    filtered = []
    date_window_start = (today - datetime.timedelta(days=3)).isoformat()
    date_window_end = (today + datetime.timedelta(days=200)).isoformat()
    for e in raw:
        date, time_s, title, venue, genre, free, url = e
        title_l = (title or "").lower()
        venue_l = (venue or "").lower()
        if not title or not venue:
            continue
        if not venue_looks_valid(venue):
            print(f"[filter] dropping garbled venue: {venue!r} title={title!r} url={url}", file=sys.stderr)
            continue
        if not (date_window_start <= date <= date_window_end):
            print(f"[filter] dropping out-of-range date {date}: title={title!r} url={url}", file=sys.stderr)
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
        "keikat_live": len(keikat_live_events),
    }
    output = {
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "source_note": (
            f"Auto-scraped from 6 sources (raw counts: {counts}), merged to "
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
