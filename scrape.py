#!/usr/bin/env python3
"""
Scraper for Meteli, Kohokohdat, Keikat.org, Puistokonsertit and Kulttuuritoimitus.
"""
import json
import re
import sys
import time
import datetime
import random
from urllib.parse import urljoin, urlparse, parse_qs

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
    'torstai 6.8.2026'). Used for the STICKY section-heading tracker —
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
    """Find the date belonging to this event across Kohokohdat layout variants."""
    txt = el.get_text(" ", strip=True)
    d = parse_date(txt, year)
    if d:
        return d

    # The event card can be several DOM levels above the anchor. Only trust a
    # container that contains exactly one event link, so a calendar wrapper
    # cannot leak its first date onto hundreds of events.
    ancestor = el
    for _ in range(8):
        ancestor = ancestor.parent
        if ancestor is None:
            break
        event_links = [
            a for a in ancestor.find_all("a", href=True)
            if _is_event_anchor(a)
        ]
        if len(event_links) == 1:
            pd = parse_date(ancestor.get_text(" ", strip=True), year)
            if pd:
                return pd

        for sib in ancestor.find_previous_siblings(limit=5):
            st = sib.get_text(" ", strip=True)
            if not st or len(st) > 100:
                continue
            if re.search(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b", st):
                sd = parse_date(st, year)
                if sd:
                    return sd

    # Date headings are sometimes div/p rather than h2/h3/h4.
    for node in el.find_all_previous(
        ["h1", "h2", "h3", "h4", "p", "div", "li"], limit=30
    ):
        st = node.get_text(" ", strip=True)
        if not st or len(st) > 100:
            continue
        if not (
            re.search(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b", st)
            or re.search(
                r"\b(?:ma|ti|ke|to|pe|la|su)\s+\d{1,2}\.\d{1,2}\.",
                st, re.I
            )
        ):
            continue
        sd = parse_date(st, year)
        if sd:
            return sd
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
    insignificant whitespace-only text nodes between tags — without that,
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


def _kohokohdat_url_date(url, year_hint):
    m = re.search(r"(?:^|[-_/])(20\d{2})-(\d{1,2})-(\d{1,2})(?:[-_/]|$)", url or "")
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


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

        # Real section heading (has a year, e.g. "torstai 6.8.2026") —
        # sticky, applies to every event until the next one of these.
        heading_date = parse_date_with_year(text[:60])
        if heading_date and len(text) < 120:
            current_date = heading_date
            pending_date_hint = None  # a new day section starts fresh
            pending_time_hint = ""
            continue

        # Per-event date/time stamp with no year (e.g. "pe 7.8." or
        # "to 6.8. - la 8.8.   16:00") — single-use, applies only to the
        # very next event anchor, then cleared. NOT sticky — this is
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
            # rest — this is still THIS event's own text, so it's more
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
                url_date = _kohokohdat_url_date(url, year)
                if url_date and _date_matches_month(url_date, year, month):
                    date_str = url_date

            if not date_str:
                # Some Kohokohdat month pages contain secondary/sidebar event links
                # whose own card has no date. The event detail page is authoritative
                # and contains the exact date/time (for example The Wowels -> 29.8.2026).
                # Use it as a fallback instead of throwing away a real gig.
                try:
                    detail_resp = fetch_with_retries(
                        "GET", url, headers=HEADERS, timeout=12, retries=1, backoff=0.5
                    )
                    detail_soup = BeautifulSoup(detail_resp.text, "html.parser")
                    detail_text = detail_soup.get_text(" ", strip=True)

                    # Do NOT use the first date on the detail page: its header
                    # contains today's date. The event's date appears after the
                    # event title (h1), e.g. "The Wowels" -> "29.8.2026".
                    detail_date = None
                    title_pos = detail_text.casefold().find(title.casefold())
                    search_text = detail_text[title_pos:] if title_pos >= 0 else detail_text
                    for dm in re.finditer(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", search_text):
                        candidate = parse_date(dm.group(0), year)
                        if candidate and _date_matches_month(candidate, year, month):
                            detail_date = candidate
                            nearby = search_text[dm.start():dm.start() + 100]
                            detail_time = parse_time(nearby)
                            if detail_time:
                                event_time_hint = detail_time
                            break

                    if detail_date:
                        date_str = detail_date
                    elif PLAYWRIGHT_AVAILABLE:
                        try:
                            rendered = fetch_with_playwright_generic(url, wait_selector="body")
                            rendered_soup = BeautifulSoup(rendered, "html.parser")
                            rendered_text = rendered_soup.get_text(" ", strip=True)
                            title_pos = rendered_text.casefold().find(title.casefold())
                            rendered_search = rendered_text[title_pos:] if title_pos >= 0 else rendered_text
                            for dm in re.finditer(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", rendered_search):
                                candidate = parse_date(dm.group(0), year)
                                if candidate and _date_matches_month(candidate, year, month):
                                    date_str = candidate
                                    break
                        except Exception as pw_exc:
                            print(f"[parse_month_page] detail Playwright fallback failed for {url}: {pw_exc}", file=sys.stderr)
                except Exception as exc:
                    print(f"[parse_month_page] detail-date fallback failed for {url}: {exc}", file=sys.stderr)

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
    August working fine — same parser, different month's markup apparently
    trips it. Can't inspect the real HTML to fix the root cause (network
    blocked from the sandbox that built this), so this catches the *symptom*
    instead: if effectively all of a month's events share one date, that's
    not a real gig calendar, that's a bug."""
    if len(events) < 12:
        return False
    from collections import Counter
    counts = Counter(e["date"] for e in events)
    top_date, top_count = counts.most_common(1)[0]
    # A real monthly music calendar can legitimately have a busy date.
    # Reject only the unmistakable failure mode: almost everything collapsed
    # onto one date with no meaningful date diversity.
    return len(counts) <= 2 and top_count / len(events) > 0.85


def fetch_month(year, month):
    url = month_url(year, month)
    resp = fetch_with_retries("GET", url, headers=HEADERS, timeout=20, retries=3, backoff=1)
    events = parse_month_page(resp.text, year, month)
    if _looks_like_stuck_date_tracking(events, year, month):
        print(f"[kohokohdat {year}-{month:02d}] REJECTING all {len(events)} events — "
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


def fetch_meteli():
    events = []
    today = datetime.date.today()
    use_scraper = cloudscraper is not None
    seen_page_signatures = set()
    page_num = 1

    while True:
        url = METELI_TAMPERE_URL if page_num == 1 else f"{METELI_TAMPERE_URL}/page/{page_num}"
        try:
            resp = fetch_with_retries("GET", url, headers=HEADERS, timeout=20, retries=4, backoff=1, use_scraper=use_scraper)
            html = resp.text
            if _looks_like_block_page(html, getattr(resp, "status_code", None)):
                print(f"[meteli page {page_num}] BLOCKED/challenge page detected — not parsing it", file=sys.stderr)
                break
        except Exception as exc:
            log_http_error(f"meteli page {page_num}", exc)
            if PLAYWRIGHT_AVAILABLE:
                try:
                    html = fetch_with_playwright_retries(url)
                    if _looks_like_block_page(html):
                        print(f"[meteli page {page_num}] BLOCKED/challenge page detected in Playwright fallback — not parsing it", file=sys.stderr)
                        break
                except Exception as exc2:
                    log_http_error(f"meteli playwright page {page_num}", exc2)
                    break
            else:
                break

        soup = BeautifulSoup(html, "html.parser")

        # Stop if the site starts returning the same page repeatedly.
        # This prevents an infinite loop while still allowing the scraper
        # to continue through every real Meteli page.
        page_links = [
            urljoin(METELI_BASE, a["href"])
            for a in soup.find_all("a", href=True)
            if "/tapahtuma/" in a["href"]
        ]
        page_signature = tuple(sorted(set(page_links)))
        if page_signature and page_signature in seen_page_signatures:
            print(
                f"[meteli page {page_num}] same event page returned again — stopping pagination",
                file=sys.stderr,
            )
            break
        if page_signature:
            seen_page_signatures.add(page_signature)

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
        _print_event_lines(f"meteli page {page_num}", [events[-found_this_page + i] for i in range(found_this_page)] if found_this_page else [])
        if found_this_page == 0:
            break

        page_num += 1

    return events


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
    return normalize_text(venue)


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


NAV_TITLE_BLACKLIST = [
    "contact information", "accommodation", "for media", "eat and drink",
    "see and do", "map", "professionals", "for travel industry professionals",
    "top attractions", "articles", "destinations", "visibility on",
    "a day in tampere", "summer cruises", "cafés in tampere",
    "top tips for summer",
]


# Keikat.org scraper

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


def looks_like_music(title, venue):
    text = f"{title} {venue}".lower()
    if any(kw in text for kws in GENRE_KEYWORDS.values() for kw in kws):
        return True
    return any(v.lower() in venue.lower() for v in KNOWN_VENUES)


# ---------------------------------------------------------------------------
# Puistokonsertit.tampere.fi (Tampere park concerts)
# ---------------------------------------------------------------------------
# Unusually reliable source: each event's own URL carries the date and time
# as query params, e.g.
#   ?event-id=...&date=08.08.2026&time=14.00+-+15.00
# so the date/time don't need to be inferred from surrounding page text at
# all \u2014 just parsed straight out of the link itself. Venue/price still come
# from the page text near the link, same approach as the other sources.
PUISTOKONSERTIT_URL = "https://puistokonsertit.tampere.fi/ohjelma/"


def parse_puistokonsertit_page(html):
    soup = BeautifulSoup(html, "html.parser")
    events = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/tapahtuma/" not in href or "event-id=" not in href:
            continue
        if href in seen:
            continue

        qs = parse_qs(urlparse(href).query)
        date_raw = (qs.get("date") or [None])[0]
        time_raw = (qs.get("time") or [None])[0]
        if not date_raw:
            continue
        dm = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", date_raw)
        if not dm:
            continue
        d, mo, y = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
        date_str = f"{y:04d}-{mo:02d}-{d:02d}"

        time_str = ""
        if time_raw:
            tm = re.match(r"(\d{1,2})\.(\d{2})", time_raw)
            if tm:
                time_str = f"{int(tm.group(1)):02d}:{tm.group(2)}"

        title = re.sub(r"^Puistokonsertit:\s*", "", a.get_text(" ", strip=True)).strip()
        if not title:
            continue

        # Venue/price come from the surrounding card text, not the URL.
        container = a.find_parent(["article", "li", "div"]) or a.parent
        ctext = container.get_text(" ", strip=True) if container else ""

        venue = "Tampere"
        for v in KNOWN_VENUES_SORTED:
            if v.lower() in ctext.lower():
                venue = v
                break
        if venue == "Tampere":
            addr_m = re.search(r"([A-ZÅÄÖ][\w .,'\-]{2,60}?),?\s*\d{5}\s+Tampere", ctext)
            if addr_m:
                candidate = addr_m.group(1).strip().rstrip(",")
                # Prefer just the venue name over "Venue, Street 39" when a
                # street address is present as its own comma-separated part.
                candidate = candidate.split(",")[0].strip()
                if not is_suspicious_heading(candidate):
                    venue = candidate

        free = 1 if "maksuton" in ctext.lower() else 0

        seen.add(href)
        events.append({
            "date": date_str,
            "time": time_str,
            "title": title,
            "venue": venue,
            "genre": guess_genre(title, venue),
            "free": free,
            "url": href,
        })

    return events


def fetch_puistokonsertit(url=PUISTOKONSERTIT_URL):
    events = []
    try:
        resp = fetch_with_retries("GET", url, headers=HEADERS, timeout=20, retries=3, backoff=1)
        if _looks_like_block_page(resp.text, getattr(resp, "status_code", None)):
            print("[puistokonsertit] BLOCKED/challenge page detected \u2014 not parsing it", file=sys.stderr)
            return events
        events = parse_puistokonsertit_page(resp.text)
    except Exception as exc:
        log_http_error("puistokonsertit", exc)
        if PLAYWRIGHT_AVAILABLE:
            try:
                html = fetch_with_playwright_generic(url, wait_selector='a[href*="/tapahtuma/"]')
                events = parse_puistokonsertit_page(html)
            except Exception as exc2:
                log_http_error("puistokonsertit playwright", exc2)
    print(f"[puistokonsertit] parsed {len(events)} events", file=sys.stderr)
    return events



# ---------------------------------------------------------------------------
# Kulttuuritoimitus.fi (Tampere/Pirkanmaa concert listing)
# ---------------------------------------------------------------------------
KULTTUURITOIMITUS_URL = "https://kulttuuritoimitus.fi/konsertit-pirkanmaa/"
KULTTUURITOIMITUS_DATE_RE = re.compile(
    r"^(?:[A-Za-zÅÄÖåäö]+\s+)?(\d{1,2})\.(?:(?:[–-](\d{1,2}))\.)?(\d{1,2})\.?(?:\s*)"
)
KULTTUURITOIMITUS_TAMPERE_MARKERS = ("/ tampere", "/ tampere", "tampere", "tampere-")


def _parse_kulttuuritoimitus_date_prefix(text, year):
    """Parse '8.8.' or '28.–30.8.' from the beginning of a listing."""
    if not text:
        return None
    m = re.match(r"^(?:[A-Za-zÅÄÖåäö]+\s+)?(\d{1,2})\.(?:[–-](\d{1,2})\.)?(\d{1,2})\.?(?=\s|$)", text.strip())
    if not m:
        return None
    day = int(m.group(1))
    end_day = int(m.group(2)) if m.group(2) else day
    month = int(m.group(3))
    try:
        start = datetime.date(year, month, day)
        end = datetime.date(year, month, end_day)
    except ValueError:
        return None
    return start.isoformat(), end.isoformat(), m.end()


def _kulttuuritoimitus_location_is_tampere(location):
    if not location:
        return False
    loc = location.casefold()
    return "tampere" in loc or "hervanta" in loc


def _kulttuuritoimitus_clean_venue(venue):
    venue = re.sub(r"\s+", " ", venue or "").strip(" []")
    venue = re.sub(r"\s*/\s*Tampere\s*$", "", venue, flags=re.I)
    venue = re.sub(r",\s*Tampere\s*$", "", venue, flags=re.I)
    return venue.strip(" []") or "Tampere"


def parse_kulttuuritoimitus_page(html, year=None):
    """Parse Kulttuuritoimitus' Pirkanmaa concert list.

    Only Tampere listings are accepted.  The parser is intentionally isolated
    from the other source parsers so a layout change here cannot affect them.
    """
    year = year or datetime.date.today().year
    soup = BeautifulSoup(html, "html.parser")
    events = []
    seen = set()
    section = ""
    current_venue = None
    scanned = 0
    date_candidates = 0
    rejected_non_tampere = 0
    rejected_no_venue = 0
    rejected_excluded = 0
    rejected_invalid = 0

    # The page uses headings to separate Tampere from the rest of Pirkanmaa.
    # Keep both the Tampere concert section and the large/festival section,
    # because the latter also contains Tampere concerts with [Venue / Tampere].
    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue

        low = text.casefold()
        if el.name in ("h1", "h2"):
            if "konsertit | tampere" in low or low == "konsertit tampere":
                section = "tampere"
                current_venue = None
                continue
            if "muu pirkanmaa" in low:
                section = "other"
                current_venue = None
                continue
            if "festivaalit ja suuret konsertit" in low:
                section = "festivals"
                current_venue = None
                continue

        if section == "other":
            continue

        if el.name == "h3":
            # Venue headings are used only inside the Tampere concert section.
            if section == "tampere":
                current_venue = text
            continue

        parsed_date = _parse_kulttuuritoimitus_date_prefix(text, year)
        if not parsed_date:
            continue
        date_candidates += 1
        scanned += 1

        start_date, end_date, prefix_end = parsed_date
        rest = text[prefix_end:].strip(" –-:·")
        if not rest or len(rest) > 220:
            rejected_invalid += 1
            continue

        venue = current_venue or ""
        location = ""

        # Most festival/large-concert rows have a location suffix such as
        # "[Nokia Arena / Tampere]".  This is authoritative for city filtering.
        loc_match = re.search(r"\[([^\]]+)\]\s*$", rest)
        if loc_match:
            location = loc_match.group(1).strip()
            rest = rest[:loc_match.start()].strip()
            if "/" in location:
                venue_part, city_part = [x.strip() for x in location.split("/", 1)]
                if not _kulttuuritoimitus_location_is_tampere(city_part):
                    rejected_non_tampere += 1
                    continue
                venue = venue_part
            elif not _kulttuuritoimitus_location_is_tampere(location):
                rejected_non_tampere += 1
                continue
        elif section == "festivals":
            # Festival rows without a [venue / city] suffix cannot safely be
            # assigned to Tampere, so do not guess.
            rejected_non_tampere += 1
            continue

        if not venue:
            rejected_no_venue += 1
            continue

        venue = _kulttuuritoimitus_clean_venue(venue)
        if not venue_looks_valid(venue):
            rejected_invalid += 1
            continue

        # Prefer an event-specific link when the row contains one. Otherwise
        # the article itself is the correct source URL. Duplicate handling in
        # merge_events guarantees that an earlier source URL wins.
        event_url = KULTTUURITOIMITUS_URL
        for a in el.find_all("a", href=True):
            href = urljoin(KULTTUURITOIMITUS_URL, a["href"])
            if href.startswith("http") and href != KULTTUURITOIMITUS_URL:
                event_url = href
                break

        title = re.sub(r"^asti\s+", "", rest, flags=re.I).strip()
        if not title:
            rejected_invalid += 1
            continue
        if any(kw in f"{title} {venue}".casefold() for kw in EXCLUDE_KEYWORDS):
            rejected_excluded += 1
            continue

        # If the whole multi-day range has ended, there is nothing useful to
        # publish. Otherwise keep the start date as the event date.
        try:
            if datetime.date.fromisoformat(end_date) < datetime.date.today():
                rejected_invalid += 1
                continue
        except ValueError:
            rejected_invalid += 1
            continue

        key = (start_date, normalize_title(title), normalize_venue(venue))
        if key in seen:
            continue
        seen.add(key)
        events.append({
            "date": start_date,
            "time": "",
            "title": title,
            "venue": venue,
            "genre": guess_genre(title, venue),
            "free": 0,
            "url": event_url,
        })

    print(
        f"[kulttuuritoimitus] scanned dated listings={scanned}, "
        f"Tampere events={len(events)}, non-Tampere={rejected_non_tampere}, "
        f"no venue={rejected_no_venue}, excluded={rejected_excluded}, "
        f"other rejected={rejected_invalid}",
        file=sys.stderr,
    )
    _print_event_lines("kulttuuritoimitus", events)
    return events


def fetch_kulttuuritoimitus(url=KULTTUURITOIMITUS_URL):
    try:
        resp = fetch_with_retries("GET", url, headers=HEADERS, timeout=15, retries=2, backoff=1)
        if _looks_like_block_page(resp.text, getattr(resp, "status_code", None)):
            print("[kulttuuritoimitus] BLOCKED/challenge page detected — not parsing it", file=sys.stderr)
            return []
        return parse_kulttuuritoimitus_page(resp.text)
    except Exception as exc:
        log_http_error("kulttuuritoimitus", exc)
        return []

def main():
    today = datetime.date.today()
    kohokohdat_events = []
    meteli_events = []
    keikat_org_events = []
    puistokonsertit_events = []
    kulttuuritoimitus_events = []
    errors = []
    source_status = {}

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
            _print_event_lines(f"kohokohdat {year}-{month:02d}", events)
            kohokohdat_events.extend(events)
            source_status["kohokohdat"] = "OK" if events else "NO_EVENTS_OR_PARSE_FAILURE"
        except Exception as exc:
            errors.append(f"kohokohdat {year}-{month:02d}: {exc}")
            source_status["kohokohdat"] = "FAILED"
            print(f"[kohokohdat {year}-{month:02d}] FAILED: {exc}", file=sys.stderr)

    try:
        meteli_events = fetch_meteli()
        source_status["meteli"] = "OK" if meteli_events else "BLOCKED_OR_NO_EVENTS"
    except Exception as exc:
        errors.append(f"meteli: {exc}")
        source_status["meteli"] = "FAILED"
        print(f"[meteli] FAILED: {exc}", file=sys.stderr)

    try:
        keikat_org_events = fetch_keikat_org()
        source_status["keikat_org"] = "OK" if keikat_org_events else "NO_EVENTS_OR_PARSE_FAILURE"
    except Exception as exc:
        errors.append(f"keikat.org: {exc}")
        source_status["keikat_org"] = "FAILED"
        print(f"[keikat.org] FAILED: {exc}", file=sys.stderr)

    try:
        puistokonsertit_events = fetch_puistokonsertit()
        source_status["puistokonsertit"] = "OK" if puistokonsertit_events else "NO_EVENTS_OR_PARSE_FAILURE"
    except Exception as exc:
        errors.append(f"puistokonsertit: {exc}")
        source_status["puistokonsertit"] = "FAILED"
        print(f"[puistokonsertit] FAILED: {exc}", file=sys.stderr)

    try:
        kulttuuritoimitus_events = fetch_kulttuuritoimitus()
        source_status["kulttuuritoimitus"] = "OK" if kulttuuritoimitus_events else "NO_EVENTS_OR_PARSE_FAILURE"
    except Exception as exc:
        errors.append(f"kulttuuritoimitus: {exc}")
        source_status["kulttuuritoimitus"] = "FAILED"
        print(f"[kulttuuritoimitus] FAILED: {exc}", file=sys.stderr)

    # First source wins when the same gig appears on multiple sites.
    # puistokonsertit goes first: its date/time come straight from the
    # event URL itself rather than being inferred from page text, so it's
    # the most trustworthy signal when a gig also shows up elsewhere.
    all_events = merge_events(puistokonsertit_events, meteli_events, kohokohdat_events, keikat_org_events, kulttuuritoimitus_events)

    if not all_events:
        print("No events parsed from any source — leaving existing data.json untouched.", file=sys.stderr)
        sys.exit(1)

    all_events.sort(key=lambda e: (e["date"], e["time"] or "99:99"))
    raw = [[e["date"], e["time"], e["title"], e["venue"], e.get("genre", "rock"), e.get("free", 0), e.get("url", "")] for e in all_events]

    filtered = []
    # Only publish gigs happening today or later — past gigs aren't useful
    # to the site, so they're dropped from the final dataset every run.
    date_window_start = today.isoformat()
    date_window_end = (today + datetime.timedelta(days=200)).isoformat()
    print(f"[date filter] Keeping gigs from {date_window_start} through {date_window_end}", file=sys.stderr)
    for e in raw:
        date, time_s, title, venue, genre, free, url = e
        title_l = (title or "").lower()
        venue_l = (venue or "").lower()
        if not title or not venue:
            continue
        if not venue_looks_valid(venue):
            print(f"[filter] dropping garbled venue: {venue!r} title={title!r} url={url}", file=sys.stderr)
            continue
        if date < date_window_start:
            print(f"[filter] dropping past gig {date}: title={title!r} url={url}", file=sys.stderr)
            continue
        if date > date_window_end:
            print(f"[filter] dropping beyond date window {date}: title={title!r} url={url}", file=sys.stderr)
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
        "puistokonsertit": len(puistokonsertit_events),
        "kulttuuritoimitus": len(kulttuuritoimitus_events),
    }
    output = {
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "source_note": "Auto-scraped from 5 sources. Music gigs only — theatre/comedy filtered out.",
        "errors": errors,
        "source_status": source_status,
        "events": filtered,
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=1)

    print("\n========================================", file=sys.stderr)
    print("SCRAPE SUMMARY", file=sys.stderr)
    print("========================================", file=sys.stderr)
    for name, count in counts.items():
        print(f"  {name:22} {count:4d}", file=sys.stderr)
    print(f"  Parsed before final filter: {len(all_events):4d}", file=sys.stderr)
    print(f"  Final events in JSON:       {len(filtered):4d}", file=sys.stderr)
    print("  Source status:", file=sys.stderr)
    for name in counts:
        print(f"    {name:20} {source_status.get(name, 'UNKNOWN')}", file=sys.stderr)
    print(f"  Output: data.json", file=sys.stderr)
    _print_final_events(filtered)


if __name__ == "__main__":
    main()
