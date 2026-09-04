"""
Shared utilities used by every source scraper: HTTP/Playwright fetching,
genre guessing, venue whitelist, date/time parsing, dedup logic, and the
known-venue list. Nothing in this file talks to a specific website — that
lives in sources/<name>.py.
"""
import re
import sys
import time
import random
import datetime
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

# Optional playwright-stealth for better Cloudflare bypass
try:
    from playwright_stealth import stealth_sync
    PLAYWRIGHT_STEALTH_AVAILABLE = True
except Exception:
    PLAYWRIGHT_STEALTH_AVAILABLE = False


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
    "komedian superilta", "komiikan festivaali", "komiikka", "stand up", "stand-up", "improvisaatioteatteri",
    "elokuvanäytös", "leffailta", "kirjailijavierailu", "kirjamessu",
    "taidenäyttely", "luento", "urheiluottelu", "jalkapallo-ottelu",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "fi-FI,fi;q=0.9,en;q=0.8",
}

KNOWN_VENUES = [
    "Näsinpuiston laululava", "Niihaman siirtolapuutarha", "Laikunlava",
    "Nekalan siirtolapuutarha", "Haiharan taidekeskus",
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


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
def log_http_error(source, exc):
    resp = getattr(exc, "response", None)
    if resp is not None:
        print(f"[{source}] FAILED: {exc} | body: {resp.text[:500]!r}", file=sys.stderr)
    else:
        print(f"[{source}] FAILED: {exc}", file=sys.stderr)


def _looks_like_block_page(html, status_code=None):
    """Return True when a response is clearly a bot/challenge/error shell.

    A blocked page must never be parsed as real events. This is especially
    important for Cloudflare pages such as Meteli/Tampere Events.
    """
    if status_code in (401, 403, 429, 503):
        return True
    if not html:
        return True
    sample = html[:200000].lower()
    markers = (
        "just a moment...", "checking your browser", "cf-chl-",
        "challenge-platform", "enable javascript and cookies to continue",
        "attention required! | cloudflare", "access denied",
        "captcha", "invalid host",
    )
    return any(marker in sample for marker in markers)


def _print_event_lines(source, events, limit=None):
    """Print every parsed event so a run is auditable from the console."""
    print(f"[{source}] parsed {len(events)} events", file=sys.stderr)
    shown = events if limit is None else events[:limit]
    for e in shown:
        print(
            f"  ✓ {e.get('date','')} {e.get('time','') or '--:--'} — "
            f"{e.get('title','')} — {e.get('venue','')} — {e.get('url','')}",
            file=sys.stderr,
        )
    if limit is not None and len(events) > limit:
        print(f"  ... {len(events) - limit} more event(s)", file=sys.stderr)


def _print_final_events(events):
    print("\n========================================", file=sys.stderr)
    print("FINAL EVENTS WRITTEN TO data.json", file=sys.stderr)
    print("========================================", file=sys.stderr)
    for e in events:
        print(
            f"{e[0]} {e[1] or '--:--'} — {e[2]} — {e[3]} — {e[6]}",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Genre / venue helpers
# ---------------------------------------------------------------------------
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
    # Reject bare city names used as lazy fallbacks — these aren't actual venues
    if v.lower() in ("tampere",):
        return False
    return True


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


def _recover_leaked_time(title, venue, time_str):
    """A venue starting with 1-2 bare digits is never real (no venue name
    starts with a number) — it's leaked time debris from wherever the real
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
            new_title = title[:m_title.start()].strip(" -–:")
            new_venue = m_venue.group(2).strip()
            return new_title, new_venue, time_str or recovered_time
    # Couldn't pair it with a title-side fragment — still strip the leaked
    # digits from venue so a recoverable event isn't dropped over noise.
    return title, m_venue.group(2).strip(), time_str


# ---------------------------------------------------------------------------
# Date / time parsing
# ---------------------------------------------------------------------------
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
    'torstai 6.8.2026'). Used for the STICKY section-heading tracker —
    real day headings on kohokohdat include the year; per-event date/time
    stamps like 'pe 7.8.' or 'to 6.8. - la 8.8. 16:00' don't. Treating both
    as equally sticky was the actual bug behind entire months piling onto
    one date: a yearless per-event stamp would overwrite the broader
    section tracker, and for a multi-day range like 'to 6.8. - la 8.8.' only
    the first date got captured and then stuck around for whatever came
    next."""
    if not text:
        return None
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", text)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{y:04d}-{mo:02d}-{d:02d}"


# ---------------------------------------------------------------------------
# HTTP / Playwright fetching
# ---------------------------------------------------------------------------
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
            # Apply stealth if available to bypass Cloudflare bot detection
            if PLAYWRIGHT_STEALTH_AVAILABLE:
                stealth_sync(page)
            # networkidle never fires on a Cloudflare "Just a moment..."
            # challenge page — it has persistent background JS activity by
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


def fetch_with_playwright_generic(url, timeout=25000, wait_selector=None):
    """Render a page with Playwright without assuming a specific event URL pattern."""
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError("playwright not available")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            headless=True,
        )
        context = browser.new_context(
            user_agent=HEADERS.get("User-Agent"),
            locale="fi-FI",
            extra_http_headers={"Accept-Language": HEADERS.get("Accept-Language", "fi-FI,fi;q=0.9")},
        )
        try:
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        except Exception:
            pass
        page = context.new_page()
        try:
            # Apply stealth if available to bypass Cloudflare bot detection
            if PLAYWRIGHT_STEALTH_AVAILABLE:
                stealth_sync(page)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=12000)
                except Exception:
                    pass
            page.wait_for_timeout(1500)
            content = page.content()
        finally:
            context.close()
            browser.close()
    lowered = content.lower()
    if "just a moment" in lowered or "cf-chl-" in lowered:
        raise RuntimeError("playwright fetch returned a Cloudflare challenge")
    if len(content) < 1000:
        raise RuntimeError("playwright fetch returned an empty/very small page")
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


# ---------------------------------------------------------------------------
# Dedup / merge
# ---------------------------------------------------------------------------
VENUE_ALIASES = {
    # Known equivalent venue-name variants seen across different sources,
    # confirmed by testing normalize_venue() against realistic pairs.
    # Deliberately explicit/targeted rather than generic fuzzy matching —
    # a wrong auto-merge here would silently hide a real, distinct gig,
    # which is worse than showing an occasional true duplicate.
    "kulttuuritalotelakka": "telakka",
}


def normalize_text(value):
    """Normalize text only for internal comparisons; never changes JSON output."""
    if not value:
        return ""
    value = value.casefold()
    # Treat Tampere/address suffixes as venue decoration, not identity.
    value = re.sub(r"\s*,\s*\d{5}\s+tampere\s*$", "", value)
    value = re.sub(r"\s*,\s*tampere\s*$", "", value)
    value = re.sub(r"[–—−]", "-", value)
    value = re.sub(r"[^\w]+", "", value, flags=re.UNICODE)
    return value


def normalize_title(title):
    return normalize_text(title)


def normalize_venue(venue):
    v = normalize_text(venue)
    if not v:
        return v
    # Strip a bare trailing "tampere" or "oy" (Finnish company suffix) that
    # survives normalize_text's comma-anchored stripping above — this shows
    # up when a source writes the venue without a comma before the city,
    # e.g. "G Livelab Tampere" vs "G Livelab", or "Tampere-talo Oy" vs
    # "Tampere-talo". Safe within our known-venue universe: nothing in
    # KNOWN_VENUES actually ends in the literal substring "tampere" as
    # part of its real identity (only ever as a trailing city suffix).
    stripped = re.sub(r"(tampere|oy)$", "", v)
    if stripped:
        v = stripped
    return VENUE_ALIASES.get(v, v)


def event_duplicate_key(event):
    """Internal identity: date + normalized title + normalized venue.

    The first event encountered wins, including its URL. This key is never
    written to data.json.
    """
    return (
        event.get("date", ""),
        normalize_title(event.get("title", "")),
        normalize_venue(event.get("venue", "")),
    )


def log_possible_duplicates(events, threshold=0.6):
    """Flags likely-duplicate pairs for a human to check in the logs —
    never removes anything. Exact (date, title, venue) duplicates are
    already handled by merge_events(); this catches the next tier down:
    same date + same canonical venue, but titles that are similar without
    being identical (e.g. one source adds a support-act name the other
    omits). Auto-merging on a fuzzy title match risks silently hiding two
    genuinely different gigs playing the same venue same night, which is
    worse than leaving an occasional real near-duplicate visible on the
    site — so this only ever logs, it doesn't touch the event list."""
    from difflib import SequenceMatcher
    from collections import defaultdict

    by_date_venue = defaultdict(list)
    for e in events:
        key = (e.get("date", ""), normalize_venue(e.get("venue", "")))
        by_date_venue[key].append(e)

    flagged = 0
    for (date, venue), group in by_date_venue.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                t1, t2 = group[i].get("title", ""), group[j].get("title", "")
                n1, n2 = normalize_title(t1), normalize_title(t2)
                if n1 == n2:
                    continue  # exact match — merge_events already handled this
                ratio = SequenceMatcher(None, n1, n2).ratio()
                if ratio >= threshold:
                    flagged += 1
                    print(
                        f"[possible duplicate] {date} @ {group[i].get('venue')} "
                        f"(similarity {ratio:.0%}):\n"
                        f"  1: {t1!r} — {group[i].get('url','')}\n"
                        f"  2: {t2!r} — {group[j].get('url','')}\n"
                        f"  NOT auto-removed — check manually if this is really one gig.",
                        file=sys.stderr,
                    )
    if flagged:
        print(f"[possible duplicates] {flagged} pair(s) flagged for manual review (see above)", file=sys.stderr)
    return flagged


def sanitize_events(events):
    """Defensive validation pass: guarantees every event dict downstream has
    all required fields with safe types, so a malformed dict from any one
    source (a bug, an unexpected None, a missing key) can't crash the whole
    pipeline. Found necessary via a chaos test that fed genuinely broken
    data through the real pipeline — a source returning a dict missing the
    'time' key crashed main() with an uncaught KeyError in the sort step
    before this existed, taking down every other source's good data along
    with it. Call this right after merge_events(), before anything does
    direct dict access like e["time"]."""
    sane = []
    dropped = 0
    for e in events:
        if not isinstance(e, dict):
            dropped += 1
            continue
        date = e.get("date")
        title = e.get("title")
        venue = e.get("venue")
        if not date or not isinstance(date, str):
            dropped += 1
            continue
        try:
            datetime.date.fromisoformat(date)
        except (ValueError, TypeError):
            # A string that merely *looks* like YYYY-MM-DD can still be an
            # impossible calendar date (e.g. "2026-13-45") — the date-window
            # filter in scrape.py only does lexicographic string comparison,
            # which such a string can pass despite not being a real date.
            # Found via a chaos test that fed exactly this case through the
            # real pipeline and watched it survive all the way to data.json.
            print(f"[sanitize] dropping event with invalid calendar date {date!r}: title={title!r}", file=sys.stderr)
            dropped += 1
            continue
        if not title or not isinstance(title, str):
            dropped += 1
            continue
        if not venue or not isinstance(venue, str):
            dropped += 1
            continue

        time_str = e.get("time")
        if not isinstance(time_str, str):
            time_str = ""

        # No source should ever produce a title this long — cap rather than
        # drop, so a legitimately verbose title (e.g. a big festival lineup)
        # still shows up, just trimmed instead of breaking card layout.
        if len(title) > 200:
            title = title[:197].rstrip() + "…"

        genre = e.get("genre")
        if not isinstance(genre, str) or not genre:
            genre = DEFAULT_GENRE

        free = e.get("free", 0)
        if not isinstance(free, int) or free not in (0, 1):
            free = 1 if free else 0

        url = e.get("url")
        if not isinstance(url, str):
            url = ""

        sane.append({
            "date": date, "time": time_str, "title": title, "venue": venue,
            "genre": genre, "free": free, "url": url,
        })
    if dropped:
        print(f"[sanitize] dropped {dropped} malformed event dict(s) before processing", file=sys.stderr)
    return sane


def merge_events(*event_lists):
    """Merge sources in order; first matching event keeps all its data/URL.

    Duplicate decisions are logged so the user can see exactly which URL won.
    """
    seen = {}
    merged = []
    duplicate_count = 0
    for events in event_lists:
        for event in events:
            key = event_duplicate_key(event)
            if not key[0] or not key[1] or not key[2]:
                print(
                    f"[merge] dropping event with missing date/title/venue: "
                    f"date={event.get('date')!r} title={event.get('title')!r} "
                    f"venue={event.get('venue')!r} url={event.get('url','')!r}",
                    file=sys.stderr,
                )
                continue
            if key in seen:
                duplicate_count += 1
                first = seen[key]
                print(
                    f"[duplicate] {event.get('date')} — {event.get('title')} — {event.get('venue')}\n"
                    f"  keeping first URL: {first.get('url','')}\n"
                    f"  ignoring later URL: {event.get('url','')}",
                    file=sys.stderr,
                )
                continue
            seen[key] = event
            merged.append(event)
    print(f"[duplicates] removed {duplicate_count} duplicate event(s); first-found URL wins", file=sys.stderr)
    return merged
