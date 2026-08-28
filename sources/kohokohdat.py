"""
kohokohdat.fi source scraper.

Broadest existing coverage of the five sources, but the trickiest to parse
reliably — see the comments on parse_month_page, _forward_adjacent_text,
and _looks_like_stuck_date_tracking below for the specific real bugs that
were found and fixed here (date tracking getting stuck on one day, venues
coming out off-by-one, etc).
"""
import re
import sys
from collections import Counter
from urllib.parse import urljoin

import datetime
from bs4 import BeautifulSoup

from .common import (
    EXCLUDE_KEYWORDS, KNOWN_VENUES_SORTED, HEADERS, PLAYWRIGHT_AVAILABLE,
    guess_genre, is_suspicious_heading, parse_date, parse_date_with_year,
    parse_time, fetch_with_retries, fetch_with_playwright_generic,
)

BASE = "https://kohokohdat.fi"
MONTH_SLUGS = {
    1: "tammikuu", 2: "helmikuu", 3: "maaliskuu", 4: "huhtikuu",
    5: "toukokuu", 6: "kesakuu", 7: "heinakuu", 8: "elokuu",
    9: "syyskuu", 10: "lokakuu", 11: "marraskuu", 12: "joulukuu",
}


def month_url(year, month):
    slug = MONTH_SLUGS[month]
    return f"{BASE}/tampere/tapahtumat-tampere/keikat-tampere-{slug}/"


def _is_event_anchor(node):
    return getattr(node, "name", None) == "a" and "/tampere/tapahtuma/" in (node.get("href") or "")


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
    # the next heading.
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


def _looks_like_stuck_date_tracking(events, year, month):
    """Detects the failure mode where current_date tracking finds one real
    date heading then silently fails to update for the rest of the page,
    piling every remaining event onto that single date. Can't inspect the
    real HTML to fix the root cause in every case, so this catches the
    *symptom* instead: if effectively all of a month's events share one
    date, that's not a real gig calendar, that's a bug."""
    if len(events) < 12:
        return False
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
