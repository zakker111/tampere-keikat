#!/usr/bin/env python3
"""
Tampere Keikat — data pipeline orchestrator.

This file no longer contains any site-specific parsing logic itself — each
source lives in its own module under sources/ (kohokohdat.py, meteli.py,
keikat_org.py, puistokonsertit.py, keikat_live.py), all built on the shared
helpers in sources/common.py. This file's only job is to run those five
scrapers (concurrently, since they're independent of each other), merge
and de-duplicate the results, filter out anything stale/invalid, and write
data.json.

Splitting it this way means a bug in one source's parser can't leak into
another's, and adding a 6th source later is just: write sources/newsite.py,
add one line to the `jobs` dict below.
"""
import json
import re
import sys
import time
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from sources.common import merge_events, venue_looks_valid, _print_final_events, log_possible_duplicates, sanitize_events
from sources.kohokohdat import fetch_month
from sources.meteli import fetch_meteli
from sources.keikat_org import fetch_keikat_org
from sources.puistokonsertit import fetch_puistokonsertit
from sources.keikat_live import fetch_keikat_live
from sources.tamperefilharmonia import fetch_tampere_filharmonia


def _run_source(name, func):
    """Run one independent source and always return a structured result."""
    started = time.monotonic()
    try:
        events = func()
        elapsed = time.monotonic() - started
        status = "OK" if events else "NO_EVENTS_OR_PARSE_FAILURE"
        print(f"[{name}] COMPLETE — {len(events)} events in {elapsed:.1f}s — {status}", file=sys.stderr)
        return name, events, status, None
    except Exception as exc:
        elapsed = time.monotonic() - started
        print(f"[{name}] FAILED after {elapsed:.1f}s: {exc}", file=sys.stderr)
        return name, [], "FAILED", f"{name}: {exc}"


def _fetch_kohokohdat_months(months):
    """Fetch the current and next Kohokohdat month concurrently."""
    events = []
    if len(months) == 1:
        year, month = months[0]
        return fetch_month(year, month)

    with ThreadPoolExecutor(max_workers=len(months), thread_name_prefix="kohokohdat") as pool:
        futures = {
            pool.submit(fetch_month, year, month): (year, month)
            for year, month in months
        }
        for future in as_completed(futures):
            year, month = futures[future]
            try:
                month_events = future.result()
                print(f"[kohokohdat {year}-{month:02d}] parsed {len(month_events)} events", file=sys.stderr)
                events.extend(month_events)
            except Exception as exc:
                print(f"[kohokohdat {year}-{month:02d}] FAILED: {exc}", file=sys.stderr)
                raise
    return events


def main():
    started_total = time.monotonic()
    today = datetime.date.today()
    errors = []
    source_status = {}
    source_events = {
        "meteli": [],
        "kohokohdat": [],
        "keikat_org": [],
        "puistokonsertit": [],
        "keikat_live": [],
        "tampere_filharmonia": [],
    }

    months_to_fetch = [(today.year, today.month)]
    nm = today.month + 1
    ny = today.year
    if nm > 12:
        nm = 1
        ny += 1
    months_to_fetch.append((ny, nm))

    print("\n========================================", file=sys.stderr)
    print("SCRAPE START", file=sys.stderr)
    print("========================================", file=sys.stderr)
    print("Running independent sources in parallel.", file=sys.stderr)
    print(f"Date range: {today.isoformat()} -> {(today + datetime.timedelta(days=200)).isoformat()}", file=sys.stderr)

    # These sources are independent. Run them concurrently so a slow/blocked
    # source cannot make the whole scraper wait behind it.
    jobs = {
        "kohokohdat": lambda: _fetch_kohokohdat_months(months_to_fetch),
        "meteli": fetch_meteli,
        "keikat_org": fetch_keikat_org,
        "puistokonsertit": fetch_puistokonsertit,
        "keikat_live": fetch_keikat_live,
        "tampere_filharmonia": fetch_tampere_filharmonia,
    }

    with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="source") as pool:
        futures = {pool.submit(_run_source, name, func): name for name, func in jobs.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                result_name, events, status, error = future.result()
            except Exception as exc:
                result_name, events, status, error = name, [], "FAILED", f"{name}: {exc}"
            source_events[result_name] = events
            source_status[result_name] = status
            if error:
                errors.append(error)

    kohokohdat_events = source_events["kohokohdat"]
    meteli_events = source_events["meteli"]
    keikat_org_events = source_events["keikat_org"]
    puistokonsertit_events = source_events["puistokonsertit"]
    keikat_live_events = source_events["keikat_live"]
    tampere_filharmonia_events = source_events["tampere_filharmonia"]

    # First source wins when the same gig appears on multiple sites.
    # puistokonsertit and tampere_filharmonia go first: both have date/time
    # sourced directly (URL params or a verified, consistent page format)
    # rather than inferred from loosely-structured page text, so they're
    # the most trustworthy signal when a gig also shows up elsewhere.
    all_events = merge_events(
        puistokonsertit_events,
        tampere_filharmonia_events,
        meteli_events,
        kohokohdat_events,
        keikat_org_events,
        keikat_live_events,
    )

    if not all_events:
        print("No events parsed from any source — leaving existing data.json untouched.", file=sys.stderr)
        sys.exit(1)

    all_events = sanitize_events(all_events)
    if not all_events:
        print("All events were malformed after sanitization — leaving existing data.json untouched.", file=sys.stderr)
        sys.exit(1)

    log_possible_duplicates(all_events)

    all_events.sort(key=lambda e: (e["date"], e["time"] or "99:99"))
    raw = [[e["date"], e["time"], e["title"], e["venue"], e.get("genre", "rock"), e.get("free", 0), e.get("url", "")] for e in all_events]

    filtered = []
    # Only publish gigs happening today or later — past gigs aren't useful
    # to the site, so they're dropped from the final dataset every run.
    date_window_start = today.isoformat()
    date_window_end = (today + datetime.timedelta(days=200)).isoformat()
    print(f"[date filter] Keeping gigs from {date_window_start} through {date_window_end}", file=sys.stderr)
    for e in raw:
        date, time_s, title, venue, genre, free, url = e
        title_l = (title or "").lower()
        venue_l = (venue or "").lower()
        if not title or not venue:
            continue
        if not venue_looks_valid(venue):
            print(f"[filter] dropping garbled venue: {venue!r} title={title!r} url={url}", file=sys.stderr)
            continue
        if date < date_window_start:
            print(f"[filter] dropping past gig {date}: title={title!r} url={url}", file=sys.stderr)
            continue
        if date > date_window_end:
            print(f"[filter] dropping beyond date window {date}: title={title!r} url={url}", file=sys.stderr)
            continue
        if len(venue) > 60:
            print(f"[filter] dropping because venue too long: {venue!r} title={title!r} url={url}", file=sys.stderr)
            continue
        if title_l == venue_l:
            print(f"[filter] dropping because title equals venue: {title!r}", file=sys.stderr)
            continue
        if re.search(r'\b(home|info|article|artikkeli|kesä|kesällä|dam|articles|events in tampere)\b', title_l, re.IGNORECASE):
            print(f"[filter] dropping heading-like title: {title!r}", file=sys.stderr)
            continue
        filtered.append(e)

    counts = {name: len(source_events[name]) for name in jobs}
    output = {
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "source_note": "Auto-scraped from 6 sources. Music gigs only — theatre/comedy filtered out.",
        "errors": errors,
        "source_status": source_status,
        "events": filtered,
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=1)

    total_elapsed = time.monotonic() - started_total
    print("\n========================================", file=sys.stderr)
    print("SCRAPE SUMMARY", file=sys.stderr)
    print("========================================", file=sys.stderr)
    for name, count in counts.items():
        print(f"  {name:22} {count:4d}  {source_status.get(name, 'UNKNOWN')}", file=sys.stderr)
    print(f"  Parsed before final filter: {len(all_events):4d}", file=sys.stderr)
    print(f"  Final events in JSON:       {len(filtered):4d}", file=sys.stderr)
    print(f"  Sources parsed: {sum(1 for name in jobs if counts[name] > 0)}/{len(jobs)}", file=sys.stderr)
    print(f"  Total runtime: {total_elapsed:.1f}s", file=sys.stderr)
    print("  Output: data.json", file=sys.stderr)
    _print_final_events(filtered)


if __name__ == "__main__":
    main()
