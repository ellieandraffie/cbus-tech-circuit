# RUNBOOK — CBus Tech Calendar scraper

Operational notes for `scrape.py` / `build.py`: what's installed, how each source
is pulled, current per-source status, and the schedule spec to wire up as a
recurring task. Last verified: **2026-09-19**.

## One-time setup (already done on this machine — repeat if scraping elsewhere)

Most sources are plain HTTP (Python stdlib only, nothing to install). Two sources
need a headless browser (Playwright + Chromium) because their event markup only
exists after JS runs client-side:

```bash
pip3 install playwright
python3 -m playwright install chromium
```

If Playwright/Chromium aren't installed, `scrape.py` still runs fine — the
sources that need it (currently just Women in Product) report a clean `SKIP`
with an actionable message instead of crashing the run. Every other source is
unaffected.

## How the two fetch paths work

1. **Plain HTTP** (`fetch()` in scrape.py, stdlib `urllib` only) — used
   wherever a site's markup, embedded JSON, or a public feed is available
   without executing JS. This turned out to cover almost everything once you
   find the right endpoint:
   - Squarespace (OhioX) server-renders its event list — no JS needed.
   - Dublin Chamber's real event data lives behind an infinite-scroll AJAX
     partial (`/events/searchscroll`) that returns plain HTML — no JS needed
     once you hit that endpoint directly instead of the page shell.
   - TechLife Columbus just embeds a public Google Calendar iframe — we decode
     the calendar ID out of the iframe `src` and read its public **ICS feed**
     directly. This is more robust than scraping markup at all.
   - Meetup group pages embed the full event list (title, time, venue, RSVP
     status) in a Next.js `__NEXT_DATA__` JSON blob that's present even on a
     plain fetch — no JS needed.
   - getWITit (Webflow) server-renders its chapter page — no JS needed.

2. **Headless-browser rendering** (`render_html()` / `RenderSession` in
   scrape.py, needs Playwright) — used only where a site genuinely requires JS
   execution to produce the event markup:
   - **Women in Product** (`community.womenpm.org`, a Circle-based community
     site) — fully client-rendered. We also render each in-window candidate
     event's own detail page (not just the list) because the list page shows
     every WIP chapter nationally with no per-chapter label — the *detail*
     page is the only place the real venue address (which tells us it's the
     Columbus chapter) shows up.
   - `src_generic()` (the fallback JSON-LD extractor used by Eventbrite, Luma,
     AI Tinkerers, and any future JSON-LD source) tries a plain fetch first and
     **automatically retries via headless render** if that returns nothing —
     so if a site starts requiring JS to expose its JSON-LD, it degrades
     gracefully instead of silently going back to zero.

## Per-source status (as of 2026-09-19 test run)

| Source | Method | Status |
|---|---|---|
| OhioX | plain fetch, Squarespace eventlist markup | ✅ working (11 in-window) |
| TechLife Columbus | plain fetch, decode Google Calendar embed → ICS | ✅ working (5 in-window) |
| Dublin Chamber | plain fetch, `/events/searchscroll` AJAX partial | ✅ working (37 in-window) |
| Startup Grind Columbus / Rev1 | Meetup adapter | ❌ **known limitation** — their Meetup group (`startup-grind-columbus`) now 404s ("Group not found"); Rev1 Ventures' own site (`rev1ventures.com/events`) is behind Cloudflare bot-detection. **Fallback: manual quarterly check** of both, or watch for a new canonical events link from startupgrind.com/columbus. |
| Eventbrite Columbus | plain fetch → JSON-LD, headless fallback | ✅ working (18 in-window) — was already wired, now actually returns events thanks to the ItemList-recursion fix |
| Luma — Ohio Startup Network | plain fetch → JSON-LD, headless fallback | ✅ working (20 in-window) — same fix |
| dev.events | plain fetch → JSON-LD | ✅ working (32 in-window, unchanged — this one always worked) |
| Women in Product Columbus | headless render (list + per-event detail) | ✅ working (1 in-window) — **confirmed the Sep 21 "Delegate & Elevate" event is auto-discovered** and correctly matched to Lauren's existing curated entry (no duplicate created) |
| IxDA Columbus (Meetup) | Meetup adapter | ✅ working, 0 in-window — group is real but genuinely has nothing scheduled right now |
| ProductTank Columbus (Meetup) | Meetup adapter | ✅ working, 0 in-window — same, genuinely dormant right now |
| Columbus AI (Meetup) | Meetup adapter | ✅ working, 0 in-window — same |
| AI Tinkerers Columbus | plain fetch → JSON-LD (nested ItemList) | ✅ working (2 in-window) |
| TechLife (Meetup) | Meetup adapter | ✅ working (8 in-window) |
| **Columbus WIT (getWITit)** | plain fetch, Webflow CMS markup | ✅ working (2 in-window: Columbus WITCON 2026, Oct 14; GetWITit's 10-Year Anniversary Gala, Dec 12) — **fixed 2026-09-28:** the Gala is rendered in a separate `event-highlight-strip` promo-banner block (used for one-off flagship events), not the regular `upcoming-event_card` markup, and its date has no year (`"Dec 12th"`). The adapter now also parses that block and infers the year relative to `TODAY`. |
| **Worthington Chamber** | headless render attempted | ❌ **known limitation** — `worthingtonchamber.org` sits behind a Cloudflare Turnstile bot-check that blocks both plain fetch and headless Chromium (confirmed manually). **Fallback: manual quarterly check** of `https://www.worthingtonchamber.org/events`. Do not fight this with stealth/anti-detection tooling — not worth the brittleness for one chamber's calendar. |
| **DevOps Columbus (Meetup)** *(new, 2026-09-28)* | Meetup adapter | ✅ working — added after a community tip (983-member active group, previously missing from the calendar). |

**Net result of this run:** `events.json` went from 10 → 68 events. All 4
previously hand-curated events (Tucci's wine tasting, the WIP Sep 21 event,
Chamber Lunch Bunch, Taste of Dublin 2026) were correctly recognized as
already-present and left untouched — no duplicates were created.

## Smarter de-duplication (why this matters going forward)

`merge()` now matches a freshly-scraped event against what's already in
`events.json` in three passes, most confident first:

1. **Same numeric event ID embedded in both URLs** (e.g. Dublin Chamber's
   `.../register/12630` and `.../details/taste-of-dublin-2026-12630` both
   carry `12630`) — survives Lauren rewriting the title or the source
   changing its link slug.
2. **Same date + normalized first-6-words-of-title** (the original matching
   logic).
3. **Same date + loose word-overlap** between titles — catches cases like
   curated *"Tucci's California Wine Tasting"* matching the source's own *"A
   Taste of California: Tucci's September Wine Tasting."*

This was necessary because Dublin Chamber and OhioX previously returned 0
events (so this never came up) — now that they return real data, without this
the scraper would have created near-duplicate entries next to Lauren's
hand-curated ones.

## Schedule spec (for Alfred to wire into a scheduled task)

- **Command(s):**
  ```bash
  cd "/Users/laurenmartin/Documents/Vibe Coding Projects/Coworking Space/cbus-tech-circuit"
  python3 scrape.py
  python3 build.py
  ```
- **Working directory:** `/Users/laurenmartin/Documents/Vibe Coding Projects/Coworking Space/cbus-tech-circuit`
- **Cadence:** weekly (matches the README's "Weekly refresh" note and the
  existing `<changefreq>weekly</changefreq>` in `sitemap.xml`) — e.g. Monday
  morning.
- **Prerequisites on the runner:** Python 3 (stdlib only for most sources) +
  Playwright/Chromium installed once (`pip3 install playwright && python3 -m
  playwright install chromium`) for the Women-in-Product source to keep
  working. If Playwright isn't available on whatever machine/agent runs the
  schedule, everything except Women in Product still works — that source just
  logs a clean SKIP.
- **After running:** `git add events.json index.html sitemap.xml && git commit
  -m "weekly scrape" && git push` — commit/push is a Lauren/Alfred approval
  gate, not something the scrape script itself should do.
- **Failure handling:** already built in — each source fails independently
  (`scrape.py`'s per-source try/except) and a bad/blocked source never wipes
  existing `events.json` data. Worth a human glance at the printed report
  occasionally (SKIP lines) in case a *previously-working* source goes quiet,
  which would mean its markup/endpoint changed and the adapter needs an
  update.

## Known limitations summary (nothing forced, all documented above)

- **Startup Grind Columbus / Rev1 Ventures** — dead Meetup link + Rev1's own
  site is Cloudflare-protected. Manual quarterly check.
- **Worthington Chamber** — Cloudflare Turnstile blocks both fetch paths.
  Manual quarterly check of `worthingtonchamber.org/events`.

## Community-suggested sources evaluated 2026-09-28 (Keith Instone via Slack)

A community member suggested several additional sources. Outcomes:

- **Added as a scraper:** DevOps Columbus (Meetup) — see table above.
- **Fixed an existing scraper:** getWITit Gala was already on the page this
  scraper hits, just in markup the old regex didn't cover — see table above.
- **Added as one-off curated entries** (not scraped — either genuinely
  one-time or the domain is currently blocked, see below): Central Ohio
  Digital Inclusion Summit (Oct 7), DataConnect Conference 2026 (Oct 29–30,
  Women in Analytics), TEDxColumbus 2026 "MAKE ROOM" (Nov 20).
- **Blocked by this environment's outbound network policy** (proxy returns
  403 on the CONNECT tunnel — same failure mode as the existing Luma/Ohio
  Startup Network source above) as of 2026-09-28: `lu.ma` (would cover the
  suggested Toledo Codes calendar, `travel: columbus` tag — reportedly
  catches cybersecurity conferences we're missing), `techcc.org` (a links
  directory, not itself scrapable — DevOps Columbus was the one actionable
  lead pulled from it), `tedxcolumbus.com`, `dataconnectconf.com`. Lauren
  widened Network access on 2026-09-29 for `lu.ma` and
  `innovatenewalbany.org` specifically, but **`lu.ma` is still hitting the
  same proxy-level 403** after that change (tried `lu.ma`, `www.luma.com`,
  `luma.com`) — worth double-checking the allowed-domains entry / whether the
  environment needs a restart to pick it up, then re-test.
- **`innovatenewalbany.org`** — the network-access change *did* take effect
  (no more proxy 403), but the site itself sits behind a Cloudflare bot-check
  that returns its own 403 to a plain request — the identical situation
  already documented for Worthington Chamber below. A quick headless-render
  attempt hit an unrelated Chromium/TLS-trust issue in this container
  (separate from the site itself) before we could even find out whether
  headless gets past Cloudflare. Given the project's existing call on
  Worthington ("not worth the brittleness for one chamber's calendar"), the
  same likely applies here — **recommend treating it as a manual quarterly
  check** (`https://innovatenewalbany.org/new-events/`) rather than sinking
  more time into a live scraper, even though it looked like a genuinely good
  fit (16k sq ft city-run tech-startup incubator, recurring "TIGER"
  business/tech events since 2015).
- **Skipped, out of scope for this calendar:** IDSA Columbus (industrial
  design, LinkedIn-only presence, no scrapable page), CSCA (graphic design
  community — organizer's own words: "sometimes talk tech"), Fisher College
  of Business "AI in Business" page (per Lauren's call), an unresolved
  `luma.com/n9xqi4u5` short link (per Lauren's call).
