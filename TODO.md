# Tampere Keikat Scraper - TODO List

## ✅ Completed Tasks

- [x] Fixed genre corruption bug in `utils/dedup.py` (list-type genres now normalized to strings)
- [x] Added update timestamp display on website (`#updatedBadge` shows "Updated DD MMM YYYY, HH:MM UTC")
- [x] Implemented stuck-date detection (>85% events on single date are rejected)
- [x] Fixed date tracking for sources with section-level dates
- [x] Added comprehensive data validation layer
- [x] Verified September & October 2026 coverage (104 + 154 events across 40 unique dates)
- [x] Updated README.md with current source status and event counts
- [x] Documented known issues (meteli Cloudflare blocking, puistokonsertit seasonal)

## 🔄 Current Status

**Working Sources (6/8):**
- kohokohdat.fi: 208 events ✅
- tamperefilharmonia.fi: 21 events ✅
- vastavirta-klubi.fi: 21 events ✅
- keikat.org: 12 events ✅
- tampere.fi/kirjastot: 11 events ✅
- keikat.live: 1 event ✅

**Expected Non-Working (2/8):**
- puistokonsertit.tampere.fi: Seasonal (May-August only) ⚠️
- meteli.net: Cloudflare blocking (needs playwright browsers) ⚠️

## 📋 Remaining Tasks

- [ ] Install playwright browsers in GitHub Actions to fix meteli.net scraping
- [ ] Add more sources if needed (g-livelab.fi, tampere.pakkahuone.fi, tampere-talo.fi)
- [ ] Monitor source health and update parsers if HTML structures change
- [ ] Consider adding Instagram scraping for venues that only post there

## 🐛 Known Issues

1. **meteli.net**: Blocked by Cloudflare. Solution: Install playwright browsers in CI environment.
2. **puistokonsertit**: Only active May-August. This is expected behavior, not a bug.
3. **Browser caching**: Users may need to hard refresh (Ctrl+Shift+R) to see updates.

---

*Last updated: 2026-09-14 20:18 UTC | Total events: 274 | Corrupted genres: 0*
