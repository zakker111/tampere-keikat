"""
vastavirta.net source scraper.

Vastavirta-Klubi is one of Tampere's oldest rock clubs, hosting rock,
metal, punk, and alternative music events. The site uses a simple WordPress
structure with events in div.vv-custom-events containers.
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

VASTAVIRTA_BASE = "https://www.vastavirta.net"
VASTAVIRTA_URL = "https://www.vastavirta.net"

# Pattern to match event heading: "DD.MM.YYYY HH:MM - Artist/Event Name"
EVENT_HEADING_RE = re.compile(
    r"^(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})\s*-\s*(.+)$"
)


def parse_vastavirta_event(div, year_hint, today):
    """Parse a single event from a vv-custom-events div."""
    heading_elem = div.find("h3", class_="vv-events-heading")
    if not heading_elem:
        return None
    
    heading_text = heading_elem.get_text(strip=True)
    m = EVENT_HEADING_RE.match(heading_text)
    if not m:
        return None
    
    day, month, year, hour, minute, title_part = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)), m.group(6)
    
    try:
        event_date = datetime.date(year, month, day)
        # Skip past events (older than 2 days)
        if event_date < today - datetime.timedelta(days=2):
            return None
    except ValueError:
        return None
    
    # Extract venue and price from paragraph elements
    venue = ""
    free = 0
    price_str = ""
    
    for p in div.find_all("p"):
        text = p.get_text(strip=True)
        if text.startswith("Tapahtumapaikka:"):
            venue_raw = text.replace("Tapahtumapaikka:", "").strip()
            # Normalize venue names - strip location qualifiers
            venue_lower = venue_raw.lower()
            if "vastavirta-klubi" in venue_lower or "alakerta" in venue_lower:
                venue = "Vastavirta-Klubi"
            elif "yläkerta" in venue_lower or "terassi" in venue_raw:
                venue = "Terassi Pub Yläkerta"
            else:
                venue = venue_raw
        elif text.startswith("Liput:"):
            price_str = text.replace("Liput:", "").strip()
            if "ilmainen" in price_str.lower() or price_str.strip() == "0 €":
                free = 1
        elif "Ilmainen sisäänpääsy" in text:
            free = 1
    
    if not venue:
        venue = "Vastavirta-Klubi"
    
    # Clean up title - remove extra venue info if present
    title = title_part.strip()
    
    # Check exclude keywords
    if any(kw in f"{title} {venue}".lower() for kw in EXCLUDE_KEYWORDS):
        return None
    
    # Get Facebook link if available
    url = VASTAVIRTA_BASE
    fb_link = div.find("a", href=True)
    if fb_link and "facebook.com" in fb_link["href"]:
        url = fb_link["href"]
    
    return {
        "date": f"{year:04d}-{month:02d}-{day:02d}",
        "time": f"{hour:02d}:{minute:02d}",
        "title": title,
        "venue": venue,
        "free": free,
        "genre": guess_genre(title, venue),
        "url": url,
    }


def fetch_vastavirta():
    """Fetch and parse events from Vastavirta-Klubi website."""
    events = []
    today = datetime.date.today()
    use_scraper = cloudscraper is not None
    
    try:
        resp = fetch_with_retries(
            "GET", VASTAVIRTA_URL, headers=HEADERS, 
            timeout=20, retries=4, backoff=1, use_scraper=use_scraper
        )
        html = resp.text
        
        # Check for actual Cloudflare challenge pages, not just any meta tags
        html_lower = html.lower()
        if "just a moment" in html_lower or "cf-chl-" in html_lower or "challenge-platform" in html_lower:
            print("[vastavirta] BLOCKED/challenge page detected — not parsing", file=sys.stderr)
            return events
            
    except Exception as exc:
        log_http_error("vastavirta", exc)
        if PLAYWRIGHT_AVAILABLE:
            try:
                html = fetch_with_playwright_retries(VASTAVIRTA_URL)
                html_lower = html.lower()
                if "just a moment" in html_lower or "cf-chl-" in html_lower or "challenge-platform" in html_lower:
                    print("[vastavirta] BLOCKED/challenge page detected in Playwright fallback — not parsing", file=sys.stderr)
                    return events
            except Exception as exc2:
                log_http_error("vastavirta playwright", exc2)
                return events
        else:
            return events
    
    soup = BeautifulSoup(html, "html.parser")
    
    # Find all event divs
    event_divs = soup.find_all("div", class_="vv-custom-events")
    
    for div in event_divs:
        event = parse_vastavirta_event(div, today.year, today)
        if event:
            events.append(event)
    
    _print_event_lines("vastavirta", events)
    return events
