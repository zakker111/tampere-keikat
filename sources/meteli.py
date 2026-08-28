"""
meteli.net source scraper.

Highest-confidence source of the five — its parser was unit-tested against
real sample listings before shipping. Sits behind Cloudflare, so every
fetch tries plain requests first and falls back to Playwright.
"""
import re
import sys
import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .common import (
    EXCLUDE_KEYWORDS, HEADERS, PLAYWRIGHT_AVAILABLE, cloudscraper,
    guess_genre, split_title_venue, fetch_with_retries,
    fetch_with_playwright_retries, _looks_like_block_page,
    log_http_error, _print_event_lines,
)

METELI_BASE = "https://www.meteli.net"
METELI_TAMPERE_URL = "https://www.meteli.net/kaupunki/tampere"
METELI_LINK_RE = re.compile(
    r"^[A-ZÅÄÖ]{2}\s+(\d{1,2})\.(\d{1,2})\.\s*(?:meteli dummy\s*)?(.+)$",
    re.IGNORECASE,
)


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
