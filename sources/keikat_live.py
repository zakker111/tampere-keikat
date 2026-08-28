"""
keikat.live source scraper — independent Tampere calendar source.

Fetched directly with plain requests; no Playwright fallback needed for
this one. Keikat.live puts the date in a section heading and the event
details in the following links, and commonly renders each entry as
"TITLE [GENRE] TITLE VENUE" (the title duplicated with a genre label
sandwiched in between) — see _keikat_live_extract_title_venue for how
that gets untangled.
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

KEIKAT_LIVE_VENUES = [
    "Mustanlahden Tapahtumasatama / Ravintola Kaisla",
    "Tampereen Komediateatteri katettu ulkoilmakatsomo",
    "Pyynikin kesäteatteri",
    "Irish Bar O’Connell’s",
    "Irish Bar O'Connell's",
    "Kulttuurikeskus Maanalainen",
    "Kulttuuritalo Telakka",
    "G Livelab Tampere",
    "Tampereen konservatorio",
    "Tampere-talo",
    "Tampere-talo",
    "Tavara-asema",
    "Tahmelan Huvila",
    "Ravintola Suoma",
    "Katubaari Axu",
    "Tallipiha",
    "Viikinsaari",
    "Ratinanniemi",
    "Ratinan stadion",
    "Koskikatu 9",
    "Satakunnankatu 12",
    "Satakunnankatu 18",
    "Jokipohjantie 47",
    "Erkkilänkatu 11 B-rappu",
    "Finlaysoninkuja 9",
    "Hatanpään valtatie 40",
    "Nyyrikintie 4",
    "Olympia",
    "Varjobaari",
    "Vastavirta-Klubi",
    "Cafe Kartano",
    "Pub Sisko ja sen Veli",
    "Artturi 9",
    "TTT-klubi",
    "TTT-Klubi",
]
KEIKAT_LIVE_VENUES_SORTED = sorted(KEIKAT_LIVE_VENUES, key=len, reverse=True)
KEIKAT_LIVE_GENRE_LABELS = [
    "Elektro / DJ", "Hip hop", "Klassinen", "Iskelmä", "Metal",
    "Rock", "Pop", "Jazz", "Folk", "Punk", "Reggae", "Blues", "Festari",
]


def _keikat_live_heading_date(text, year):
    """Return ISO date from headings such as '11.8.TI 1 keikka'."""
    text = re.sub(r"\s+", " ", text).strip()
    m = re.match(
        r"^\s*(\d{1,2})\.(\d{1,2})\.[A-Za-zÄÖÅäöå]+\s+\d+\s+keikka(?:a)?\s*$",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None
    try:
        return datetime.date(year, int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        return None


def _keikat_live_clean_title(text):
    """Remove category labels and the duplicated title rendered by the site."""
    text = re.sub(r"\s+", " ", text).strip(" -–:")
    for label in sorted(KEIKAT_LIVE_GENRE_LABELS, key=len, reverse=True):
        text = re.sub(rf"\s+{re.escape(label)}(?=\s|$)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+K-18\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" -–:")

    words = text.split()
    for n in range(len(words) // 2, 0, -1):
        if (
            [w.casefold() for w in words[:n]]
            == [w.casefold() for w in words[n:2 * n]]
            and len(words) == 2 * n
        ):
            return " ".join(words[:n]).strip()

    return text


def _keikat_live_extract_title_venue(text_before_time):
    """
    Keikat.live commonly renders:
        TITLE [GENRE] TITLE VENUE
    Find the repeated title and treat the remainder as the venue.
    """
    cleaned = re.sub(r"\s+", " ", text_before_time).strip(" -–:")
    words = cleaned.split()
    if len(words) < 3:
        return "", ""

    labels = {label.casefold() for label in KEIKAT_LIVE_GENRE_LABELS}

    for title_len in range(min(len(words) // 2, 40), 0, -1):
        first = words[:title_len]

        for middle_len in range(0, min(3, len(words) - 2 * title_len) + 1):
            second_start = title_len + middle_len
            second_end = second_start + title_len
            if second_end >= len(words):
                continue

            middle = words[title_len:second_start]
            if any(word.casefold() not in labels and word.casefold() != "k-18" for word in middle):
                continue

            second = words[second_start:second_end]
            if [w.casefold() for w in first] != [w.casefold() for w in second]:
                continue

            venue = " ".join(words[second_end:]).strip(" -–:")
            if venue:
                return " ".join(first), venue

    for venue in KEIKAT_LIVE_VENUES_SORTED:
        if cleaned.casefold().endswith(venue.casefold()):
            return cleaned[: -len(venue)].strip(" -–:"), venue

    return "", ""


def parse_keikat_live_page(html, year=None):
    """Parse the public Tampere calendar without JavaScript or Playwright."""
    year = year or datetime.date.today().year
    soup = BeautifulSoup(html, "html.parser")
    events = []
    current_date = None
    seen_urls = set()

    for el in soup.find_all(["h2", "h3", "h4", "a"]):
        if el.name in {"h2", "h3", "h4"}:
            heading = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
            parsed_date = _keikat_live_heading_date(heading, year)
            if parsed_date:
                current_date = parsed_date
            continue

        if not current_date:
            continue

        text = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        if not text or "klo" not in text.casefold():
            continue

        tm = re.search(r"\bklo\s*([01]?\d|2[0-3])\.([0-5]\d)\b", text, re.IGNORECASE)
        if not tm:
            continue

        time_str = f"{int(tm.group(1)):02d}:{tm.group(2)}"
        before_time = text[:tm.start()].strip()
        free = 1 if re.search(r"\bIlmainen\b", text, re.IGNORECASE) else 0

        title_prefix, venue = _keikat_live_extract_title_venue(before_time)
        if not title_prefix or not venue:
            continue

        title = _keikat_live_clean_title(title_prefix)
        if not title or len(title) > 180 or len(venue) > 80:
            continue

        if any(kw in f"{title} {venue}".casefold() for kw in EXCLUDE_KEYWORDS):
            continue

        href = el.get("href", "").strip()
        if not href:
            continue

        url = urljoin(KEIKAT_LIVE_URL, href)
        if url in seen_urls:
            continue
        seen_urls.add(url)

        events.append({
            "date": current_date,
            "time": time_str,
            "title": title,
            "venue": venue,
            "genre": guess_genre(title, venue),
            "free": free,
            "url": url,
        })

    return events


def fetch_keikat_live(url=KEIKAT_LIVE_URL):
    """Fetch Keikat.live directly; no Playwright fallback is used."""
    try:
        resp = fetch_with_retries("GET", url, headers=HEADERS, timeout=20, retries=2, backoff=1)
        if _looks_like_block_page(resp.text, getattr(resp, "status_code", None)):
            print("[keikat_live] BLOCKED/challenge page detected — not parsing it", file=sys.stderr)
            return []
        events = parse_keikat_live_page(resp.text, datetime.date.today().year)
        print(
            f"[keikat_live] parsed {len(events)} events from Tampere calendar",
            file=sys.stderr,
        )
        if events:
            print(f"[keikat_live] SOURCE OK — {len(events)} events parsed", file=sys.stderr)
        else:
            print("[keikat_live] SOURCE OK — page fetched, but 0 events parsed", file=sys.stderr)
        return events
    except Exception as exc:
        log_http_error("keikat_live", exc)
        return []
