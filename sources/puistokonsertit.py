"""
puistokonsertit.tampere.fi (Tampere park concerts) source scraper.

Unusually reliable source: each event's own URL carries the date and time
as query params, e.g. ?event-id=...&date=08.08.2026&time=14.00+-+15.00
so the date/time don't need to be inferred from surrounding page text at
all — just parsed straight out of the link itself. Venue/price still come
from the page text near the link, same approach as the other sources.
"""
import sys
import re
from urllib.parse import urlparse, parse_qs

from bs4 import BeautifulSoup

from .common import (
    HEADERS, PLAYWRIGHT_AVAILABLE, KNOWN_VENUES_SORTED,
    guess_genre, is_suspicious_heading, fetch_with_retries,
    fetch_with_playwright_generic, _looks_like_block_page, log_http_error,
)

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
            print("[puistokonsertit] BLOCKED/challenge page detected — not parsing it", file=sys.stderr)
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
