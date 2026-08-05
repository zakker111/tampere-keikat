#!/usr/bin/env python3
"""
Scrapes Tampere *live music* gig listings from four sources and merges them
into data.json for the Tampere Keikat site. Confidence varies a lot between
them — read this before trusting the output blindly:

  1. meteli.net (fetch_meteli / parse_meteli_anchor_text) — HIGHEST
     CONFIDENCE. Parser was unit-tested against 7 real sample listings
     pulled from the live page before shipping (all passed).

  2. kohokohdat.fi (fetch_month / parse_month_page) — MEDIUM. A generic
     heuristic, not tested against the live site (no internet access in
     the sandbox that built it). Broadest existing coverage.

  3. keikat.org (fetch_keikat_org / parse_keikat_org_anchor_text) — LOW.
     Built from search-snippet text, not inspected real HTML. Could easily
     mis-parse until someone checks it against the live markup.

  4. linkedevents.tampere.fi (fetch_linkedevents) — LOW, but for a
     different reason: it's a real structured JSON API (not scraping), so
     if the query params are right it should be very reliable — but I
     could not get a single successful test call through in this sandbox
     (network there is domain-allowlisted and blocked it outright), so the
     query params are unverified against Tampere's actual instance. It's
     also the only source covering *all* city events, not just gigs, so it
     gets an extra positive-match filter (looks_like_music()) on top of the
     usual exclude-list, to avoid mislabeling random civic events as music.

merge_events() de-duplicates by (date, normalized title) across all four,
earlier-listed sources winning conflicts. data.json's `source_note` records
the raw per-source counts on every run — if one source suddenly returns 0,
that's your signal it broke and needs a look (see README).

Kohokohdat's "keikat" (gig) pages sometimes carry non-music programming from
the same venues — theatre-festival shows, stand-up nights, film screenings —
because those venues also host gigs. EXCLUDE_KEYWORDS below filters those
out so the site only shows things that are actually music playing. If you
notice a non-music item slipping through, add a distinctive phrase from its
title/programme name to EXCLUDE_KEYWORDS (not a generic word that could also
appear in a real band/song name).

IMPORTANT — read this before relying on it:
This was written without being able to inspect the site's live HTML/CSS
from the sandbox that built it (no internet access there). It targets the
*text structure* Kohokohdat's listing pages use rather than brittle CSS
class names, which tends to survive redesigns better, but it has not been
run against the real site yet. The first run should be checked by hand
(the GitHub Action opens a PR instead of pushing straight to main for
exactly this reason — see the workflow file).

The GitHub Action commits straight to main and redeploys, so check the
commit diff on data.json after the first couple of scheduled runs (Actions
tab -> workflow run -> "scrape" job -> git commit) to confirm the output
looks sane before trusting it unattended.

If it breaks: open month_url(year, month) in a browser, view source, and
adjust `parse_month_page()` to match what you see. The genre-guessing in
`guess_genre()` is intentionally simple keyword matching — tune the
GENRE_KEYWORDS dict as you notice misclassifications.
"""
import json
import re
import sys
import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://kohokohdat.fi"
MONTH_SLUGS = {
    1: "tammikuu", 2: "helmikuu", 3: "maaliskuu", 4: "huhtikuu",
    5: "toukokuu", 6: "kesakuu", 7: "heinakuu", 8: "elokuu",
    9: "syyskuu", 10: "lokakuu", 11: "marraskuu", 12: "joulukuu",
}
FI_MONTH_NUM = {
    "tammi": 1, "helmi": 2, "maalis": 3, "huhti": 4, "touko": 5, "kesä": 6,
    "heinä": 7, "elo": 8, "syys": 9, "loka": 10, "marras": 11, "joulu": 12,
}
FI_DOW = {"ma": 0, "ti": 1, "ke": 2, "to": 3, "pe": 4, "la": 5, "su": 6}

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

# Kohokohdat's "keikat" (gig) pages occasionally include non-music programming
# from the same venues (theatre festivals, stand-up, film screenings etc).
# Anything whose title/venue text matches one of these gets dropped entirely
# rather than mis-tagged into a music genre. Keep this list to *whole-programme*
# markers, not words that could plausibly appear in a real song/band title.
EXCLUDE_KEYWORDS = [
    "teatterikesä", "telttalab", "näytelmä", "teatteriesitys",
    "komedian superilta", "stand up", "stand-up", "improvisaatioteatteri",
    "elokuvanäytös", "leffailta", "kirjailijavierailu", "kirjamessu",
    "taidenäyttely", "luento", "urheiluottelu", "jalkapallo-ottelu",
]


def is_music_event(title, venue):
    text = f"{title} {venue}".lower()
    return not any(kw in text for kw in EXCLUDE_KEYWORDS)

HEADERS = {"User-Agent": "TampereKeikatBot/1.0 (personal hobby project; contact via github issues)"}


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
    """Looks for a Finnish date like 'la 1.8.' or '1.8.2026' and returns YYYY-MM-DD."""
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
    """
    Best-effort extraction. Kohokohdat's listing pages group events under
    date headings followed by venue/event blocks with a link to a detail
    page. We walk block-level elements in order, track the most recent
    date heading we've seen, and treat links into /tampere/tapahtuma/...
    as individual events.
    """
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
            if not is_music_event(title, venue):
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

    # de-dupe by url
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


def fetch_month(year, month):
    url = month_url(year, month)
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return parse_month_page(resp.text, year, month)


# ---------------------------------------------------------------------------
# SOURCE 2: meteli.net — a dedicated Finnish gig-listing site with a citywide
# Tampere page. Its link text follows a very consistent, verified pattern
# (confirmed against the real live page while building this, unlike the
# kohokohdat parser above which is best-effort):
#   "TI 04.08. [meteli dummy ]Title Venue, Tampere - alk. 52 € Löydä liput"
# This makes it a good second, independent source: if kohokohdat's markup
# drifts and breaks parse_month_page(), this one can keep working, and vice
# versa. Events found in both get deduped (see merge_events()).
# ---------------------------------------------------------------------------
METELI_BASE = "https://www.meteli.net"
METELI_TAMPERE_URL = "https://www.meteli.net/kaupunki/tampere"
METELI_LINK_RE = re.compile(
    r"^[A-ZÅÄÖ]{2}\s+(\d{1,2})\.(\d{1,2})\.\s*(?:meteli dummy\s*)?(.+)$",
    re.IGNORECASE,
)

# Splitting "Title Venue, Tampere" purely by character class is ambiguous
# when a title itself is several capitalized words (e.g. "Red Nose Company
# & Meta4 Tampere-talo, Tampere" — is "Tampere-talo" the venue, or part of
# the title? both look identical to a generic regex). A whitelist of known
# Tampere venue names, matched as a literal suffix, sidesteps that: this
# list was tested against real samples from the live page while building
# this. Extend it if you notice `venue` coming back empty/wrong for a venue
# not yet listed here.
KNOWN_VENUES = [
    "G Livelab Tampere", "Vastavirta-Klubi", "Vastavirta-klubi", "Paapan Kapakka",
    "Telakka", "Tavara-asema", "Bar Kotelo", "Pethaus", "Ruby & Fellas",
    "John Scott's Ratina", "Tampere-talo", "Tampereen stadion", "Tähti Areena",
    "Liikelaituri", "Kalevan kirkko", "Pub Simon", "O'Connell's Irish Bar",
    "O'Connell's", "Ilona", "Bar Ihku", "Axu Katubaari", "Paja Bar", "Armo",
    "Maanalainen", "Varjobaari", "Moro Sky Bar", "Gastropub Soho",
    "Winebistro 1910", "Ravintola Muusa", "Atmos Taproom",
    "Kulttuuriravintola Railo", "Tallipiha", "Tammelan Stadion",
    "Järvensivunpuisto", "Uittotunneli", "Laikunlava", "Tahmelan Huvila",
    "Pub Kujakolli", "Panimoravintola Plevna", "Saivo Kitchen & Bar",
    "Sisko ja sen Veli", "Mustalahden satama", "Ratinan stadion",
    "Ratinanniemen festivaalipuisto", "Sorsapuisto", "Finlaysonin Palatsi",
    "Väinö Linnan aukio", "Tyrvään Pappila", "Kuudes Linja", "Semifinal",
    "Kulttuuritehdas Korjaamo", "Tavastia-klubi", "Nekala", "Ylöjärven kaupungintalo",
]
KNOWN_VENUES_SORTED = sorted(KNOWN_VENUES, key=len, reverse=True)


def split_title_venue(rest):
    for v in KNOWN_VENUES_SORTED:
        suffix = f"{v}, Tampere"
        if rest.lower().endswith(suffix.lower()):
            return rest[: -len(suffix)].strip(" -–:"), v
    # Fallback for venues not in the whitelist yet: best-effort generic match.
    # Less reliable for titles with multiple capitalized words — see note above.
    m = re.search(r"(?P<venue>[A-ZÅÄÖ0-9][\w .'&\-]*?)\s*,\s*Tampere\s*$", rest)
    if m:
        return rest[:m.start()].strip(" -–:"), m.group("venue").strip()
    return None, None


def parse_meteli_anchor_text(text, year_hint, today):
    m = METELI_LINK_RE.match(text.strip())
    if not m:
        return None
    day, month, rest = int(m.group(1)), int(m.group(2)), m.group(3)

    # meteli doesn't repeat the year; roll over to next year if the date
    # would otherwise be far in the past (handles a Dec->Jan scrape).
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

    venue, title = None, None
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


def fetch_meteli(max_pages=4):
    events = []
    today = datetime.date.today()
    for page_num in range(1, max_pages + 1):
        url = METELI_TAMPERE_URL if page_num == 1 else f"{METELI_TAMPERE_URL}/page/{page_num}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            if resp.status_code == 404:
                break
            resp.raise_for_status()
        except Exception as exc:
            print(f"[meteli page {page_num}] FAILED: {exc}", file=sys.stderr)
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        found_this_page = 0
        for a in soup.find_all("a", href=True):
            if "/tapahtuma/" not in a["href"]:
                continue
            text = a.get_text(" ", strip=True)
            parsed = parse_meteli_anchor_text(text, today.year, today)
            if not parsed:
                continue
            if not is_music_event(parsed["title"], parsed["venue"]):
                continue
            parsed["genre"] = guess_genre(parsed["title"], parsed["venue"])
            parsed["url"] = urljoin(METELI_BASE, a["href"])
            events.append(parsed)
            found_this_page += 1
        print(f"[meteli page {page_num}] parsed {found_this_page} events", file=sys.stderr)
        if found_this_page == 0:
            break
    return events


def normalize_title(title):
    return re.sub(r"[^a-z0-9]", "", title.lower())[:40]


def merge_events(*event_lists):
    """Combines multiple sources, de-duplicating by (date, normalized title).
    Earlier lists in the argument order win on conflicts."""
    seen = {}
    merged = []
    for events in event_lists:
        for e in events:
            key = (e["date"], normalize_title(e["title"]))
            if key in seen:
                continue
            seen[key] = True
            merged.append(e)
    return merged


# ---------------------------------------------------------------------------
# SOURCE 3: keikat.org — LOW CONFIDENCE, unverified.
# Same situation as kohokohdat: built from search-snippet text, not real
# inspected HTML, so the anchor-boundary assumptions below may not match
# the live markup. Wrapped so a total failure here just yields zero events
# from this source rather than crashing the whole run.
# ---------------------------------------------------------------------------
KEIKAT_ORG_URL = "https://keikat.org/tampere"
KEIKAT_ORG_DATE_RE = re.compile(r"\d{1,2}\.\d{1,2}\.(\d{4})")


def parse_keikat_org_anchor_text(text):
    m = KEIKAT_ORG_DATE_RE.search(text)
    if not m:
        return None
    date_part = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    day, month, year = int(date_part.group(1)), int(date_part.group(2)), int(date_part.group(3))
    rest = text[date_part.end():].strip(" ·-")

    time_str = parse_time(rest[:10])
    rest = re.sub(r"^\d{1,2}[:.]\d{2}\s*·?\s*", "", rest)
    rest = re.sub(r"\s*Liput\s*$", "", rest).strip()
    rest = re.sub(r"[\d,\.]+\s*€\s*$", "", rest).strip()

    # this site's listing text sometimes repeats the title verbatim twice
    # in a row (title + venue duplicated for mobile layout) — collapse that.
    half = len(rest) // 2
    if half > 3 and rest[:half].strip() == rest[half:].strip():
        rest = rest[:half].strip()

    title, venue = split_title_venue(rest)
    if not title or not venue:
        return None
    return {"date": f"{year:04d}-{month:02d}-{day:02d}", "time": time_str, "title": title, "venue": venue, "free": 0}


def fetch_keikat_org(url=KEIKAT_ORG_URL):
    events = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as exc:
        print(f"[keikat.org] FAILED (non-fatal, source skipped): {exc}", file=sys.stderr)
        return events

    soup = BeautifulSoup(resp.text, "html.parser")
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if not KEIKAT_ORG_DATE_RE.search(text):
            continue
        parsed = parse_keikat_org_anchor_text(text)
        if not parsed or not is_music_event(parsed["title"], parsed["venue"]):
            continue
        parsed["genre"] = guess_genre(parsed["title"], parsed["venue"])
        parsed["url"] = urljoin(url, a["href"])
        events.append(parsed)
    print(f"[keikat.org] parsed {len(events)} events", file=sys.stderr)
    return events


# ---------------------------------------------------------------------------
# SOURCE 4: linkedevents.tampere.fi — a real JSON API (PirkanmaaEvents /
# LinkedEvents platform), not HTML scraping. In principle the most reliable
# possible source, BUT: I could not get a successful test call through in
# the sandbox that built this (network there is allowlisted and blocked the
# domain entirely with a 403 — a sandbox limitation, not evidence the API
# itself doesn't work). The query params below follow standard LinkedEvents
# conventions but are UNVERIFIED against Tampere's specific instance.
# This is a citywide feed covering all event types, not just gigs, so it
# gets an extra positive music-match filter (looks_like_music) rather than
# relying only on the exclude-list, to avoid mislabeling random civic
# events as "rock" via guess_genre()'s default.
# ---------------------------------------------------------------------------
LINKEDEVENTS_URL = "http://linkedevents.tampere.fi/v1/event/"


def looks_like_music(title, venue):
    text = f"{title} {venue}".lower()
    if any(kw in text for kws in GENRE_KEYWORDS.values() for kw in kws):
        return True
    return any(v.lower() in venue.lower() for v in KNOWN_VENUES)


def fetch_linkedevents(days_ahead=45):
    events = []
    today = datetime.date.today()
    end = today + datetime.timedelta(days=days_ahead)
    params = {
        "start": today.isoformat(),
        "end": end.isoformat(),
        "sort": "start_time",
        "page_size": 100,
    }
    try:
        resp = requests.get(LINKEDEVENTS_URL, headers=HEADERS, params=params, timeout=20)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        print(f"[linkedevents] FAILED (non-fatal, source skipped): {exc}", file=sys.stderr)
        return events

    for item in payload.get("data", []):
        try:
            name_field = item.get("name") or {}
            name = name_field.get("fi") or name_field.get("en") or ""
            loc_field = (item.get("location") or {}).get("name") or {}
            venue = loc_field.get("fi") or loc_field.get("en") or ""
            start_time = item.get("start_time") or ""
            if not name or len(start_time) < 10:
                continue
            date_str = start_time[:10]
            time_str = start_time[11:16] if len(start_time) >= 16 else ""
            if not is_music_event(name, venue) or not looks_like_music(name, venue):
                continue
            events.append({
                "date": date_str,
                "time": time_str,
                "title": name.strip(),
                "venue": (venue or "Tampere").strip(),
                "genre": guess_genre(name, venue),
                "free": 0,
                "url": item.get("info_url") or item.get("@id") or LINKEDEVENTS_URL,
            })
        except Exception:
            continue
    print(f"[linkedevents] parsed {len(events)} events", file=sys.stderr)
    return events


def main():
    today = datetime.date.today()
    kohokohdat_events = []
    meteli_events = []
    keikat_org_events = []
    linkedevents_events = []
    errors = []

    # Source 1: kohokohdat.fi, current month + next month
    months_to_fetch = [(today.year, today.month)]
    nm = today.month + 1
    ny = today.year
    if nm > 12:
        nm = 1
        ny += 1
    months_to_fetch.append((ny, nm))

    for year, month in months_to_fetch:
        try:
            events = fetch_month(year, month)
            print(f"[kohokohdat {year}-{month:02d}] parsed {len(events)} events", file=sys.stderr)
            kohokohdat_events.extend(events)
        except Exception as exc:
            errors.append(f"kohokohdat {year}-{month:02d}: {exc}")
            print(f"[kohokohdat {year}-{month:02d}] FAILED: {exc}", file=sys.stderr)

    # Source 2: meteli.net, citywide Tampere page (already sorted by date, paginated)
    try:
        meteli_events = fetch_meteli(max_pages=4)
    except Exception as exc:
        errors.append(f"meteli: {exc}")
        print(f"[meteli] FAILED: {exc}", file=sys.stderr)

    # Source 3: keikat.org — best-effort, see module docstring for confidence level
    try:
        keikat_org_events = fetch_keikat_org()
    except Exception as exc:
        errors.append(f"keikat.org: {exc}")
        print(f"[keikat.org] FAILED: {exc}", file=sys.stderr)

    # Source 4: linkedevents.tampere.fi API — best-effort, unverified query params
    try:
        linkedevents_events = fetch_linkedevents()
    except Exception as exc:
        errors.append(f"linkedevents: {exc}")
        print(f"[linkedevents] FAILED: {exc}", file=sys.stderr)

    # Order matters for merge_events(): earlier = wins on duplicate (date, title).
    # meteli first since its parser is the most rigorously tested; kohokohdat
    # second as the broadest-coverage existing source; the two newest/least
    # certain sources fill in gaps last.
    all_events = merge_events(meteli_events, kohokohdat_events, keikat_org_events, linkedevents_events)

    if not all_events:
        print("No events parsed from any source — leaving existing data.json untouched.", file=sys.stderr)
        sys.exit(1)

    all_events.sort(key=lambda e: (e["date"], e["time"] or "99:99"))
    raw = [[e["date"], e["time"], e["title"], e["venue"], e["genre"], e["free"], e["url"]] for e in all_events]

    counts = {
        "meteli": len(meteli_events),
        "kohokohdat": len(kohokohdat_events),
        "keikat_org": len(keikat_org_events),
        "linkedevents": len(linkedevents_events),
    }
    output = {
        "generated": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "source_note": (
            f"Auto-scraped from 4 sources (raw counts: {counts}), merged to "
            f"{len(raw)} events after de-duplication. Music gigs only \u2014 "
            f"theatre/comedy filtered out. Confidence varies by source: "
            f"meteli.net was tested against real samples before shipping; "
            f"kohokohdat.fi, keikat.org and the linkedevents.tampere.fi API "
            f"integration are best-effort and unverified \u2014 check the "
            f"per-source counts above if the total looks off."
        ),
        "errors": errors,
        "events": raw,
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=1)

    print(f"Wrote {len(raw)} events to data.json. Per-source raw counts: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
