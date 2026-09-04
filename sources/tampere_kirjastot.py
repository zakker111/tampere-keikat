"""
tampere_kirjastot source scraper.

Scrapes music events from Tampere city library calendar via their Eventz API.
Source URL: https://www.tampere.fi/kirjastot/kirjastojen-tapahtumat
API Endpoint: https://www.tampere.fi/api/eventz-today/{paragraph_id}
Music events paragraph ID: 105505
"""
import sys
import datetime
import re
from urllib.parse import urljoin

from .common import (
    EXCLUDE_KEYWORDS, HEADERS, PLAYWRIGHT_AVAILABLE, cloudscraper,
    guess_genre, fetch_with_retries, log_http_error, _print_event_lines,
)

TAMPERE_KIRJASTOT_BASE = "https://www.tampere.fi"
TAMPERE_KIRJASTOT_API = "https://www.tampere.fi/api/eventz-today/105505"
MUSIIKKI_PARAGRAPH_ID = "105505"


def parse_library_event(event_data):
    """Parse a single event from the API response."""
    heading = event_data.get("heading", "")
    date_str = event_data.get("date", "")
    venue = event_data.get("tag", "")
    link_url = event_data.get("link_url", "")
    
    if not heading or not date_str:
        return None
    
    # Skip excluded keywords
    if any(kw in f"{heading} {venue}".lower() for kw in EXCLUDE_KEYWORDS):
        return None
    
    # Parse date - format can be "4.9.2026 12.00" or "3.10.2026–10.10.2026"
    parsed_date = ""
    parsed_time = ""
    
    # Check for date range first (e.g., "3.10.2026–10.10.2026")
    range_match = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{4})[–-](\d{1,2})\.(\d{1,2})\.(\d{4})', date_str)
    if range_match:
        # Use start date of range
        day, month, year = int(range_match.group(1)), int(range_match.group(2)), int(range_match.group(3))
        parsed_date = f"{year:04d}-{month:02d}-{day:02d}"
    else:
        # Single date with time (e.g., "4.9.2026 12.00")
        single_match = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2})\.(\d{2})', date_str)
        if single_match:
            day, month, year = int(single_match.group(1)), int(single_match.group(2)), int(single_match.group(3))
            hour, minute = int(single_match.group(4)), int(single_match.group(5))
            parsed_date = f"{year:04d}-{month:02d}-{day:02d}"
            parsed_time = f"{hour:02d}:{minute:02d}"
        else:
            # Date without time
            date_only_match = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{4})', date_str)
            if date_only_match:
                day, month, year = int(date_only_match.group(1)), int(date_only_match.group(2)), int(date_only_match.group(3))
                parsed_date = f"{year:04d}-{month:02d}-{day:02d}"
    
    if not parsed_date:
        return None
    
    # Filter out past events
    try:
        event_date = datetime.datetime.strptime(parsed_date, "%Y-%m-%d").date()
        if event_date < datetime.date.today():
            return None
    except ValueError:
        return None
    
    # Clean venue name
    venue_clean = venue.replace(", Tampere", "").strip() if venue else ""
    
    # Determine genre - libraries host diverse events
    genre = guess_genre(heading, venue_clean)
    if "karaoke" in heading.lower():
        genre = "pop"
    elif "konsertti" in heading.lower() or "luentokonsertti" in heading.lower():
        genre = "classical"
    elif "rock" in heading.lower():
        genre = "rock"
    
    return {
        "date": parsed_date,
        "time": parsed_time,
        "title": heading.strip(),
        "venue": venue_clean if venue_clean else "Tampereen kirjasto",
        "free": 1,  # Library events are typically free
        "genre": genre,
        "url": link_url,
    }


def fetch_tampere_kirjastot():
    """Fetch events from Tampere library music events API."""
    events = []
    
    try:
        resp = fetch_with_retries(
            "GET", 
            TAMPERE_KIRJASTOT_API, 
            headers=HEADERS, 
            timeout=20, 
            retries=3, 
            backoff=1,
            use_scraper=False  # API doesn't need cloudscraper
        )
        data = resp.json()
        
        if not data.get("success", False):
            print(f"[tampere_kirjastot] API returned success=false", file=sys.stderr)
            return events
        
        liftups = data.get("liftups", [])
        if not liftups:
            print(f"[tampere_kirjastot] No events in API response", file=sys.stderr)
            return events
        
        for event_data in liftups:
            parsed = parse_library_event(event_data)
            if parsed:
                events.append(parsed)
        
        _print_event_lines("tampere_kirjastot", events)
        
    except Exception as exc:
        log_http_error("tampere_kirjastot", exc)
    
    return events


def scrape():
    """Main entry point for tampere_kirjastot scraper."""
    return fetch_tampere_kirjastot()
