"""
tamperefilharmonia.fi (Tampere Philharmonic Orchestra) source scraper.

Fetched and verified against the live page while building this — not a
blind guess like keikat_live was. https://www.tamperefilharmonia.fi/en/concerts/
returned clean, consistent server-rendered HTML with no robots.txt block.

Each event card is a heading (h2/h3) linking to the event, followed by a
short description, then plain-text date/time/venue lines:
    28.8.2026
    18.00 - 22.30
    Tampere-talo Oy, Yliopistonkatu 55, 33100 Tampere
Some recurring events (e.g. "Open Rehearsal") use the same
?event-id=...&date=D.M.YYYY&time=HH.MM+-+HH.MM URL pattern as
puistokonsertit.tampere.fi, suggesting a shared city CMS — but most events
here use a plain /dynamic-event/<slug>/ URL with the date/time only in the
surrounding text, so this parser reads the text rather than relying on the
URL like puistokonsertit's does.

Genre is hardcoded to "classical" rather than guessed from keywords: this
entire site is one orchestra's programme, and concert titles like
"Kullervo" or "Water Music" wouldn't match any of guess_genre()'s keyword
list, so guessing would just misclassify everything as the rock default.
"""
import re
import sys
import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .common import (
    HEADERS, KNOWN_VENUES_SORTED,
    is_suspicious_heading, fetch_with_retries, _looks_like_block_page,
    log_http_error,
)

TAMPERE_FILHARMONIA_URL = "https://www.tamperefilharmonia.fi/en/concerts/"


def parse_filharmonia_page(html):
    soup = BeautifulSoup(html, "html.parser")
    events = []
    seen = set()

    for h in soup.find_all(["h2", "h3"]):
        a = h.find("a", href=True)
        if not a:
            continue
        href = a["href"]
        if not any(marker in href for marker in ("/dynamic-event/", "/tapahtuma/", "/konsertti/")):
            continue
        title = a.get_text(" ", strip=True)
        if not title or href in seen:
            continue

        # Gather the surrounding card text (date/time/venue live here for
        # most events; the query-param URL style only covers a few).
        container = h.find_parent(["article", "div", "li"])
        ctext = container.get_text(" ", strip=True) if container else ""
        if not ctext:
            texts, node, hops = [], h.find_next_sibling(), 0
            while node is not None and hops < 6:
                if hasattr(node, "get_text"):
                    texts.append(node.get_text(" ", strip=True))
                node = node.find_next_sibling()
                hops += 1
            ctext = " ".join(texts)

        date_m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", ctext)
        if not date_m:
            continue
        d, mo, y = int(date_m.group(1)), int(date_m.group(2)), int(date_m.group(3))
        try:
            date_str = datetime.date(y, mo, d).isoformat()
        except ValueError:
            continue

        # Times here use "HH.MM - HH.MM" (periods, not colons) — different
        # convention from every other source in this project.
        time_m = re.search(r"\b([01]?\d|2[0-3])\.([0-5]\d)\s*-\s*(?:[01]?\d|2[0-3])\.[0-5]\d\b", ctext)
        time_str = f"{int(time_m.group(1)):02d}:{time_m.group(2)}" if time_m else ""
        if time_str == "00:00":
            time_str = ""  # "00.00 - 00.00" on the real page means unspecified, not midnight

        venue = "Tampere"
        for v in KNOWN_VENUES_SORTED:
            if v.lower() in ctext.lower():
                venue = v
                break
        if venue == "Tampere":
            addr_m = re.search(r"([A-ZÅÄÖ][\w .,'\-]{2,60}?),?\s*\d{5}\s+Tampere", ctext)
            if addr_m:
                candidate = addr_m.group(1).split(",")[0].strip()
                if not is_suspicious_heading(candidate):
                    venue = candidate

        free = 1 if re.search(r"\bFree\b", ctext) else 0

        seen.add(href)
        events.append({
            "date": date_str,
            "time": time_str,
            "title": title,
            "venue": venue,
            "genre": "classical",
            "free": free,
            "url": href if href.startswith("http") else urljoin(TAMPERE_FILHARMONIA_URL, href),
        })

    return events


def fetch_tampere_filharmonia(max_pages=3):
    events = []
    for page in range(1, max_pages + 1):
        url = TAMPERE_FILHARMONIA_URL if page == 1 else f"{TAMPERE_FILHARMONIA_URL}?paged={page}"
        try:
            resp = fetch_with_retries("GET", url, headers=HEADERS, timeout=20, retries=3, backoff=1)
            if _looks_like_block_page(resp.text, getattr(resp, "status_code", None)):
                print(f"[tamperefilharmonia page {page}] BLOCKED/challenge page detected — stopping", file=sys.stderr)
                break
            page_events = parse_filharmonia_page(resp.text)
        except Exception as exc:
            log_http_error(f"tamperefilharmonia page {page}", exc)
            break
        if not page_events:
            break
        events.extend(page_events)
        print(f"[tamperefilharmonia page {page}] parsed {len(page_events)} events", file=sys.stderr)
    print(f"[tamperefilharmonia] TOTAL parsed {len(events)} events", file=sys.stderr)
    return events
