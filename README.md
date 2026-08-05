# Tampere Keikat

A one-page gig guide for Tampere: list + calendar views, genre filters, mobile-friendly.
`data.json` is meant to be regenerated daily by `scrape.py` via GitHub Actions.

## What it scrapes

Four sources, merged and de-duplicated by (date, title):

| Source | Confidence | Why |
|---|---|---|
| meteli.net | **High** | Parser unit-tested against 7 real listings before shipping |
| kohokohdat.fi | Medium | Generic heuristic, broadest coverage, unverified against live site |
| keikat.org | Low | Built from search-snippet text, not real inspected HTML |
| linkedevents.tampere.fi | Low (but a real API) | Official structured JSON API — should be the most reliable in principle, but I couldn't get a test query through this sandbox's network lock, so params are unverified |

None of the four were reachable from the sandbox that built this (network
egress there is domain-allowlisted; every one came back `403
host_not_allowed`). GitHub Actions runners have normal internet access, so
this isn't expected to be a problem once deployed — but it does mean the
**first real run against the live sites is the actual test**, not something
I could confirm beforehand for sources 2–4.

Every run's `data.json` records per-source raw counts in `source_note` —
check the Actions log or that field if the total event count looks off; a
source silently returning 0 is your signal something broke.

Coverage is still limited to whatever these four sites list — a venue that
only posts to its own website or Instagram won't show up regardless.

## Deploy it (about 5 minutes)

1. **Create a new repo on GitHub** (github.com → New repository). Public, no
   README/license needed — you already have these files. Name it whatever
   you like, e.g. `tampere-keikat`.

2. **Upload these files** to the repo. Easiest way if you don't use git
   from the command line: on the repo's page, "Add file" → "Upload files",
   drag in everything from this folder *including* the `.github` folder
   (GitHub's uploader keeps the folder structure). Commit to `main`.

3. **Turn on GitHub Pages**: repo → Settings → Pages → under "Build and
   deployment", set Source to **GitHub Actions** (not "Deploy from a
   branch" — the included workflow handles the build itself).

4. **Run the workflow once by hand**: repo → Actions tab → "Update Tampere
   gig data and deploy" → Run workflow. This does the first scrape + first
   Pages deploy. After this it also runs automatically every day at
   05:00 UTC.

5. Your live link will show up under Settings → Pages once the first
   deploy finishes — something like:
   `https://yourusername.github.io/tampere-keikat/`

## What to check after the first run

- Open the Action run's log for the `scrape` job and read the "parsed N
  events" lines — if N is 0 or suspiciously low, the site's HTML has
  probably drifted from what `scrape.py` expects.
- Look at the commit it made to `data.json` and skim a few entries for
  correct dates/venues/genres.
- If the page shows "Showing bundled snapshot (live data.json
  unavailable)" instead of "Auto-updated …", `data.json` failed to load —
  check the browser console on the deployed page.

## If a source needs fixing

Check `source_note` in `data.json` (or the Actions log) for per-source
counts first — that tells you which one broke.

- **meteli**: `parse_meteli_anchor_text()` / `split_title_venue()` — if a
  venue keeps coming back wrong, add its exact name to `KNOWN_VENUES`.
- **kohokohdat**: `parse_month_page()`.
- **keikat.org**: `parse_keikat_org_anchor_text()` — most likely to need a
  fix first, since it was built blind.
- **linkedevents**: `fetch_linkedevents()` — if this returns 0 every time,
  open `http://linkedevents.tampere.fi/v1/event/` in a browser, check the
  actual JSON field names and working query params, and adjust the `params`
  dict and field lookups (`item.get("name")` etc.) to match.

Genre guessing (shared by all sources) is in `guess_genre()`. Everything's
commented. You (or a future Claude session with real internet access, e.g.
via Claude Code) can iterate on any of these against the live sites much
more reliably than I could while building this, since I had no network
access in the sandbox I built it in.

## Manual data updates without touching the scraper

You can always hand-edit `data.json` directly (it's just an array of
`[date, time, title, venue, genre, free, url]` rows plus a `generated`
timestamp) and push — no need to wait for or debug the scraper.
