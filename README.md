# Tampere Keikat

A one-page gig guide for Tampere: list + calendar views, genre filters, mobile-friendly.
`data.json` is meant to be regenerated daily by `scrape.py` via GitHub Actions.

## Code structure

```
scrape.py              orchestrator only — no site-specific parsing logic here
utils/
  dedup.py             event deduplication: hash-based duplicate detection
sources/
  common.py             shared helpers: HTTP/Playwright fetching, genre guessing,
                         venue whitelist, date/time parsing
  kohokohdat.py          kohokohdat.fi
  meteli.py              meteli.net
  keikat_org.py          keikat.org
  puistokonsertit.py     puistokonsertit.tampere.fi
  keikat_live.py         keikat.live
  tamperefilharmonia.py  tamperefilharmonia.fi
  g_livelab.fi           g-livelab.fi
  pakkahuone.py          tampere.pakkahuone.fi
  tampere_kirjastot.py   tampere.fi/kirjastot (library events)
  tampere_talo.py        tampere-talo.fi
  vastavirta.py          vastavirta-klubi.fi
```

Each source module is self-contained: it knows how to fetch and parse
exactly one website, and returns a plain list of event dicts. `scrape.py`
runs all sources concurrently, merges/de-duplicates the results, filters out
anything stale or invalid, and writes `data.json`. A bug in one source's
parser can't leak into another's, and adding a new source later is just:
write `sources/newsite.py` with a `fetch_x()` function, import it in
`scrape.py`, add one line to the `jobs` dict.

## What it scrapes

Eleven sources, merged and de-duplicated by (date, title, venue):

| Source | Confidence | Why |
|---|---|---|
| meteli.net | **High** | Parser unit-tested against 7 real listings before shipping |
| puistokonsertit.tampere.fi | **High** | Date/time come from the event URL's own query params, not inferred from page text |
| tamperefilharmonia.fi | **High** | Fetched and verified against the live page while building this — real dates/times/venues, no robots.txt block. Adds classical coverage the other sources barely touch. |
| kohokohdat.fi | Medium | Real bugs found and fixed against an actual page snapshot (date tracking, venue extraction) — see comments in `sources/kohokohdat.py` |
| keikat.org | Low | Built from search-snippet text, not fully inspected real HTML |
| keikat.live | Unverified | Built without a live-tested reference sample |
| g-livelab.fi | Unverified | New source, needs live testing |
| tampere.pakkahuone.fi | Unverified | New source, needs live testing |
| tampere.fi/kirjastot | Unverified | New source, needs live testing |
| tampere-talo.fi | Unverified | New source, needs live testing |
| vastavirta-klubi.fi | Unverified | New source, needs live testing |

None of the sources were reachable from the sandbox that built this (network
egress there is domain-allowlisted; every one came back `403
host_not_allowed`). GitHub Actions runners have normal internet access, so
this isn't expected to be a problem once deployed — but it does mean the
**first real run against the live sites is the actual test**, not something
I could confirm beforehand for most sources.

Every run's `data.json` records per-source raw counts in `source_note` —
check the Actions log or that field if the total event count looks off; a
source silently returning 0 is your signal something broke.

Coverage is still limited to whatever these eleven sites list — a venue that
only posts to its own website or Instagram won't show up regardless.

## Deploy it (about 5 minutes)

1. **Create a new repo on GitHub** (github.com → New repository). Public, no
   README/license needed — you already have these files. Name it whatever
   you like, e.g. `tampere-keikat`.

2. **Upload these files** to the repo. Easiest way if you don't use git
   from the command line: on the repo's page, "Add file" → "Upload files",
   drag in everything from this folder *including* the `.github` folder
   (GitHub's uploader keeps the folder structure). Commit to the repository's default branch.

3. **Turn on GitHub Pages**: repo → Settings → Pages → under "Build and
   deployment", set Source to **GitHub Actions** (not "Deploy from a
   branch" — the included workflow handles the build itself).

4. **Run the workflow once by hand (first-time run)** — this performs the
   initial scrape and creates the first Pages deployment. Steps:

   - Go to your repository on GitHub and click the "Actions" tab.
   - In the left-hand workflow list, find and click the workflow named "Update Tampere gig data and deploy".
   - On the workflow page, click the green "Run workflow" button (top-right). If prompted, select the branch to run on (use the default branch, usually `main`) and any inputs, then click "Run workflow".
   - Wait for the run to complete. You can open the run and inspect the `scrape` job logs and the `pages` deploy job.

   After this first manual run, the workflow will also run automatically every day at 05:00 UTC (configured in the workflow file).

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

Check `source_status` in `data.json` (or the Actions log) for per-source
status first — that tells you which one broke. Each source is now its own
file under `sources/`, independent of the others.

- **meteli**: `sources/meteli.py` — `parse_meteli_anchor_text()` /
  `split_title_venue()` (in `sources/common.py`). If a venue keeps coming
  back wrong, add its exact name to `KNOWN_VENUES` in `common.py`.
- **kohokohdat**: `sources/kohokohdat.py` — `parse_month_page()`. Broadest
  coverage but the trickiest site; read the comments on
  `_forward_adjacent_text()` and `_looks_like_stuck_date_tracking()` before
  changing anything, they document real bugs already found and fixed here.
- **keikat.org**: `sources/keikat_org.py` — `parse_keikat_org_anchor_text()`,
  built from search-snippet text rather than fully inspected HTML.
- **puistokonsertit**: `sources/puistokonsertit.py` — most reliable source,
  date/time come from the event URL itself.
- **keikat.live**: `sources/keikat_live.py` — built without a live-tested
  reference sample, most likely to need adjustment first.
- **g_livelab**: `sources/g_livelab.py` — new source, needs live testing.
- **pakkahuone**: `sources/pakkahuone.py` — new source, needs live testing.
- **tampere_kirjastot**: `sources/tampere_kirjastot.py` — new source, needs live testing.
- **tampere_talo**: `sources/tampere_talo.py` — new source, needs live testing.
- **vastavirta**: `sources/vastavirta.py` — new source, needs live testing.
- **tamperefilharmonia**: `sources/tamperefilharmonia.py` — verified against live page.

Genre guessing (shared by all sources) is `guess_genre()` in
`sources/common.py`. Everything's commented. You (or a future Claude
session with real internet access, e.g. via Claude Code) can iterate on
any of these against the live sites much more reliably than from a
sandbox without network access.

## Manual data updates without touching the scraper

You can always hand-edit `data.json` directly (it's just an array of
`[date, time, title, venue, genre, free, url]` rows plus a `generated`
timestamp) and push — no need to wait for or debug the scraper.

## Visitor analytics (cookie consent + Google Analytics)

The site has a working cookie-consent banner built in, but analytics
stays fully inactive until you plug in your own Google Analytics ID —
safe to deploy as-is with no tracking happening at all.

**To turn it on:**

1. Go to [analytics.google.com](https://analytics.google.com), sign in
   with a Google account, and create a new GA4 property for your site
   (free). It'll give you a Measurement ID that looks like `G-ABC1234567`.
2. Open `index.html`, find this line near the bottom (search for
   `GA_MEASUREMENT_ID`):
   ```js
   const GA_MEASUREMENT_ID = "G-XXXXXXXXXX";
   ```
   Replace the placeholder with your real ID.
3. Commit and push. That's it — no other changes needed.

**How the consent flow actually works:** on a visitor's first visit, a
banner asks Accept or Decline. The Google Analytics script is only ever
injected into the page *after* Accept is clicked — never before, and
never just because the banner was shown. That's the real legal
requirement (GDPR/ePrivacy), not just displaying a banner. Their choice
is remembered in `localStorage` (not a cookie) so they aren't asked again
on future visits, and a "Cookie preferences" link in the footer lets them
reopen the banner and change their mind at any time.

If you leave `GA_MEASUREMENT_ID` as the placeholder, the banner still
shows (so the UI is testable), but clicking Accept logs a console warning
and does not inject any tracking script — verified with an automated
browser test before shipping this.

