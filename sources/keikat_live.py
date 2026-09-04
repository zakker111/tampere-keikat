"""
keikat.live source scraper — independent Tampere calendar source.

UPDATED: Site structure changed! Now uses single <a class="gig"> tags containing
all event info (genre, title, venue, date/time) in one text blob.
Pattern: "GENRE TITLE VENUE DATE klo TIME" or "TITLE GENRE TITLE VENUE Today/Tomorrow klo TIME"
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

KEIKAT_LIVE_GENRE_MAP = {
    "rock": "rock",
    "metal": "metal",
    "punk": "punk",
    "pop": "pop",
    "hip hop": "hiphop",
    "jazz": "jazz",
    "blues": "jazz",
    "iskelmä": "folk",
    "folk": "folk",
    "reggae": "folk",
    "elektro / dj": "electronic",
    "klassinen": "classical",
    "festari": "festival",
}


def parse_keikat_live_page(html, year=None):
    """Parses keikat.live with new single-anchor format.
    Each event is in one <a class="gig"> tag with text like:
    "Musta Juhla Festari Musta Juhla Vastavirta-Klubi Tänään klo 19.00"
    or "Tuomiofest Klassinen Festari Tuomiofest Hatanpään valtatie 40 Huomenna klo 15.00"
    or "Band Name Genre Band Name Venue 5.9. klo 21.00"
    """
    year = year or datetime.date.today().year
    today = datetime.date.today()
    soup = BeautifulSoup(html, "html.parser")
    events = []
    seen_urls = set()
    
    # Find all gig anchors (new format)
    gig_anchors = soup.find_all("a", class_="gig")
    
    for a in gig_anchors:
        href = a.get("href")
        if not href:
            continue
            
        text = a.get_text(" ", strip=True)
        if not text or len(text) < 10:
            continue
            
        full_url = urljoin(KEIKAT_LIVE_URL, href)
        if full_url in seen_urls:
            continue
        
        # Extract date/time - supports "Tänään", "Huomenna", or specific date
        date_obj = None
        time_str = None
        
        # Check for "Tänään klo HH.MM"
        today_match = re.search(r'Tänään klo (\d{1,2}\.\d{2})', text)
        if today_match:
            date_obj = today
            time_str = today_match.group(1)
        else:
            # Check for "Huomenna klo HH.MM"
            tomorrow_match = re.search(r'Huomenna klo (\d{1,2}\.\d{2})', text)
            if tomorrow_match:
                date_obj = today + datetime.timedelta(days=1)
                time_str = tomorrow_match.group(1)
            else:
                # Check for specific date "DD.DD. klo HH.MM"
                date_match = re.search(r'(\d{1,2}\.\d{1,2}\.)\s+klo\s+(\d{1,2}\.\d{2})', text)
                if date_match:
                    day_month = date_match.group(1).rstrip('.')
                    day, month = map(int, day_month.split('.'))
                    time_str = date_match.group(2)
                    
                    try:
                        candidate = datetime.date(year, month, day)
                        # Handle year rollover
                        if candidate < today - datetime.timedelta(days=3):
                            candidate = datetime.date(year + 1, month, day)
                        date_obj = candidate
                    except ValueError:
                        continue
        
        if not date_obj or not time_str:
            continue
        
        # Extract genre (usually first word or phrase before title)
        # Look for known genre keywords in text
        genre = None
        text_lower = text.lower()
        for genre_key, genre_val in KEIKAT_LIVE_GENRE_MAP.items():
            if genre_key in text_lower:
                genre = genre_val
                break
        
        if not genre:
            genre = guess_genre(text, "")
        
        # Clean up title and venue
        # Remove date/time parts from text for title extraction
        clean_text = re.sub(r'(Tänään|Huomenna|\d{1,2}\.\d{1,2}\.)\s+klo\s+\d{1,2}\.\d{2}', '', text).strip()
        
        # Try to split into title and venue
        # Usually pattern is: "Genre Title Venue" or "Title Genre Title Venue"
        # Take first part as title, last part before date as venue
        parts = clean_text.split()
        if len(parts) >= 3:
            # Heuristic: title is first 2-4 words, venue is last 1-3 words
            venue = parts[-1] if len(parts) > 2 else clean_text
            title = ' '.join(parts[:-1]) if len(parts) > 2 else clean_text
        else:
            title = clean_text
            venue = "Tampere"
        
        # Skip excluded content
        if any(kw in f"{title}".casefold() for kw in EXCLUDE_KEYWORDS):
            continue
        
        # Limit lengths
        if len(title) > 180 or len(venue) > 80:
            continue
            
        seen_urls.add(full_url)
        
        events.append({
            "date": date_obj.isoformat(),
            "time": time_str.replace('.', ':'),
            "title": title.strip(),
            "venue": venue.strip(),
            "genre": genre,
            "free": 0,
            "url": full_url,
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
        if events:
            print(f"[keikat_live] SOURCE OK — {len(events)} events parsed", file=sys.stderr)
        else:
            print("[keikat_live] SOURCE OK — page fetched, but 0 events parsed", file=sys.stderr)
        return events
    except Exception as exc:
        log_http_error("keikat_live", exc)
        return []
