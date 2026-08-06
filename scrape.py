#!/usr/bin/env python3
"""
Scraper with Playwright fallback for meteli.net and VisitTampere integration.
"""
import json
import re
import sys
import time
import datetime
import random
from urllib.parse import urljoin

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


def guess_genre(title, venue):
    text = f"{title} {venue}".lower()
    for genre, kws in GENRE_KEYWORDS.items():
        if any(kw in text for kw in kws):
            return genre
    return DEFAULT_GENRE


def month_url(year, month):
    slug = MONTH_SLUGS[month]
    return f"{BASE}/tampere/tapahtumat-tampere/keikat-tampere-{slug}/"


def parse_time(text):
    m = re.search(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", text)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return ""


def parse_date(text, year_hint):
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", text)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(?!\d)", text)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        return f"{year_hint:04d}-{mo:02d}-{d:02d}"
    return None


def parse_month_page(html, year, month):
    soup = BeautifulSoup(html, "html.parser")
    events = []
    current_date = None
    excluded_count = 0

    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "a", "li", "div"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue

        maybe_date = parse_date(text[:40], year)
        if maybe_date and len(text) < 60:
            current_date = maybe_date
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
            parent_text = el.find_parent(["li", "div", "article"])
            if parent_text:
                pt = parent_text.get_text(" ", strip=True)
                venue_match = re.search(r"Tampere\s+([A-ZÅÄÖ][\w &'\-]{2,40})", pt)
                if venue_match:
                    venue = venue_match.group(1).strip()
            if any(kw in f"{title} {venue}".lower() for kw in EXCLUDE_KEYWORDS):
                excluded_count += 1
                continue

            time_str = parse_time(text)
            date_str = current_date or f"{year:04d}-{month:02d}-01"
            events.append({
                "date": date_str,
                "time": time_str,
                "title": title,
                "venue": venue or "Tampere",
                "genre": guess_genre(title, venue),
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
