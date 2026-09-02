"""
keikat.live source scraper — independent Tampere calendar source.

Rewritten against a REAL sample of the live page (confirmed via a real
production run + a pasted snapshot), replacing an earlier version that
was built entirely blind and, as expected, returned 0 events in practice.

Real structure: each event renders as FOUR consecutive <a> tags all
sharing the same href — genre tag, title, venue, then date/time, e.g.:
    [Festari](url)
    [Nousevan komiikan festivaali - Perjantaiklubi 18.00](url)
    [Onda Music & Arts Café, Tampere](url)
    [28.8.klo 18.00](url)
Nothing like the "TITLE [GENRE] TITLE VENUE duplicated in one link" shape
the old parser assumed — that guess was simply wrong, which is exactly
why it silently returned zero rather than erroring.
"""
import re
import sys
import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .common import (
    EXCLUDE_KEYWORDS, HEADERS,
    guess_genre, fetch_with_retries, _looks_like_block_page, log_http_error,
)

KEIKAT_LIVE_URL = "https://keikat.live/kaupunki/tampere"

# keikat.live tags each event with its own genre label — trust that over
# guessing from keywords in the title, since it's the site's own
# classification. Falls back to guess_genre() for any label not listed here.
KEIKAT_LIVE_GENRE_MAP = {
    "rock": "rock",
    "metal": "metal",
    "punk": "metal",
    "pop": "hiphop",
    "hip hop": "hiphop",
    "jazz": "jazz",
    "blues": "jazz",
    "iskelmä": "hiphop",
    "folk": "folk",
    "reggae": "folk",
    "elektro / dj": "electronic",
    "klassinen": "classical",
    "festari": "festival",
}

KEIKAT_LIVE_DATETIME_RE = re.compile(
    r"(\d{1,2})\.(\d{1,2})\.\s*klo\s*([01]?\d|2[0-3])\.([0-5]\d)", re.IGNORECASE
)


def parse_keikat_live_page(html, year=None):
    """Groups consecutive same-href anchors into 4-tuples: genre, title,
    venue, date/time. Confirmed against one real example while building
    this (a comedy-festival listing, correctly excluded below) — still
    worth checking the per-source count after the first real run, since
    one example isn't the same as broad coverage."""
    year = year or datetime.date.today().year
    today = datetime.date.today()
    soup = BeautifulSoup(html, "html.parser")
    events = []
    seen_urls = set()

    anchors = soup.find_all("a", href=True)
    i, n = 0, len(anchors)
    while i < n:
        href = anchors[i]["href"]
        j = i + 1
        while j < n and anchors[j]["href"] == href:
            j += 1
        group = anchors[i:j]

        if len(group) >= 4:
            genre_tag, title, venue_raw, dt_text = (
                a.get_text(" ", strip=True) for a in group[:4]
            )
            dt_m = KEIKAT_LIVE_DATETIME_RE.match(dt_text)
            if dt_m and title and venue_raw:
                day, month = int(dt_m.group(1)), int(dt_m.group(2))
                hh, mm = int(dt_m.group(3)), dt_m.group(4)
                try:
                    candidate = datetime.date(year, month, day)
                    ev_year = year
                    if candidate < today - datetime.timedelta(days=3):
                        ev_year += 1
                        candidate = datetime.date(ev_year, month, day)

                    if not any(kw in f"{title} {genre_tag}".casefold() for kw in EXCLUDE_KEYWORDS):
                        venue = re.sub(r",\s*Tampere\s*$", "", venue_raw, flags=re.IGNORECASE).strip() or venue_raw
                        full_url = urljoin(KEIKAT_LIVE_URL, href)
                        if full_url not in seen_urls and len(title) <= 180 and len(venue) <= 80:
                            seen_urls.add(full_url)
                            genre = KEIKAT_LIVE_GENRE_MAP.get(genre_tag.strip().casefold())
                            if not genre:
                                genre = guess_genre(title, venue)
                            events.append({
                                "date": candidate.isoformat(),
                                "time": f"{hh:02d}:{mm}",
                                "title": title,
                                "venue": venue,
                                "genre": genre,
                                "free": 0,
                                "url": full_url,
                            })
                except ValueError:
                    pass

        i = j if j > i else i + 1

    return events


def fetch_keikat_live(url=KEIKAT_LIVE_URL):
    """Fetch Keikat.live directly; no Playwright fallback is used."""
    try:
        resp = fetch_with_retries("GET", url, headers=HEADERS, timeout=20, retries=2, backoff=1)
        if _looks_like_block_page(resp.text, getattr(resp, "status_code", None)):
            print("[keikat_live] BLOCKED/challenge page detected — not parsing it", file=sys.stderr)
            return []
        events = parse_keikat_live_page(resp.text, datetime.date.today().year)
        if events:
            print(f"[keikat_live] SOURCE OK — {len(events)} events parsed", file=sys.stderr)
        else:
            print("[keikat_live] SOURCE OK — page fetched, but 0 events parsed", file=sys.stderr)
        return events
    except Exception as exc:
        log_http_error("keikat_live", exc)
        return []
