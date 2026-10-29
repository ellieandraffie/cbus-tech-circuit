#!/usr/bin/env python3
"""
scrape.py — weekly event scraper for CBus Tech Circuit.

Pulls upcoming (next ~3 months) Central-Ohio tech/startup/networking events from a
set of sources, normalizes them into the events.json schema, classifies each by
type, dedupes against what's already in events.json, and writes the merged result
back. PAST events are kept (the page greys them). It is deliberately conservative:

  * Each source is fetched in its own try/except. If a source errors, times out, or
    returns nothing parseable, it is SKIPPED and logged — we never let one bad
    source wipe the calendar. On ANY failure we fail toward keeping existing data.
  * Hand-curated fields already in events.json (desc, members, cost, time) are
    PRESERVED. A scrape only *fills gaps* (e.g. adds a missing registration URL);
    it does not clobber Lauren's edits.

Two fetch paths:
  1. Plain HTTP (`fetch()`, stdlib only) — used wherever a site serves useful
     markup/JSON/ICS without running JS (server-rendered HTML, embedded JSON-LD,
     a site's own JSON/JSONP/ICS endpoints, Meetup's Next.js data island, etc).
     Prefer this — it's faster and has nothing extra to install.
  2. Headless-browser rendering (`render_html()`, needs Playwright + Chromium —
     see RUNBOOK.md for the one-time install) — used only where a site truly
     requires JS execution to produce the event markup (currently: Women in
     Product's community site). `src_generic()` also retries via this path if a
     plain fetch turns up no JSON-LD, so aggregator sites that start requiring JS
     degrade gracefully instead of silently returning zero.

Where a source resists both (aggressive bot-detection / Cloudflare challenge /
login-gated), it's skipped with a clear reason — see RUNBOOK.md for the current
per-source status + fallbacks. We do not fight bot walls; that's a "check
manually" case, not a scraping case.

Usage:
    python3 scrape.py                 # fetch, merge, write events.json
    python3 scrape.py --dry-run       # fetch + report, write nothing
    python3 scrape.py --only ohiox    # run a single source (dev/testing)
"""
import datetime
import html
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
EVENTS_JSON = os.path.join(HERE, "events.json")

TODAY = datetime.date.today()
HORIZON = TODAY + datetime.timedelta(days=95)   # ~3 months forward
UA = "CBusTechCircuitBot/1.0 (+https://github.com/ellieandraffie/cbus-tech-circuit) weekly community calendar"
UA_BROWSER = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# Columbus-metro city/area names ~within 30 min of Dublin OH 43017. We can't geocode
# without an external service, so we keep events whose location text names one of
# these (or whose location is unknown — better to include and let Lauren cull).
METRO = [
    "dublin", "columbus", "worthington", "westerville", "hilliard", "powell",
    "new albany", "gahanna", "grove city", "upper arlington", "marysville",
    "delaware", "lewis center", "reynoldsburg", "bexley", "grandview", "polaris",
    "easton", "short north", "the peninsula", "rev1", "franklinton",
    "central ohio", "cbus", "metro place",
]

# ---------------------------------------------------------------------------
# type classification — keyword heuristics; default to networking
# ---------------------------------------------------------------------------
TYPE_RULES = [
    ("conf",   ["summit", "conference", "expo", "symposium", "convention", "conf "]),
    ("round",  ["roundtable", "round table", "panel", "forum", "fireside", "discussion", "office hours"]),
    ("work",   ["workshop", "hands-on", "hands on", "bootcamp", "training", "class",
                "lab", "tutorial", "lunch & learn", "lunch and learn", "webinar",
                "demo", "tinkerers", "build", "code", "seminar", "masterclass"]),
    ("social", ["happy hour", "mixer", "party", "social", "celebration", "wine",
                "tasting", "festival", "carnifall", "holiday", "cookout", "bbq",
                "bbq", "trivia", "game night"]),
    ("net",    ["networking", "network", "meetup", "breakfast", "coffee", "connect",
                "lunch bunch", "off the clock", "chamber"]),
]


def classify(title, desc=""):
    text = (title + " " + (desc or "")).lower()
    for typ, kws in TYPE_RULES:
        if any(k in text for k in kws):
            return typ
    return "net"


# Dublin Chamber's own calendar is a general small-business chamber calendar
# (wine tastings, grief-support workshops, ribbon cuttings, golf outings...) —
# only a handful of its events are actually tech/startup-relevant. Rather than
# pull in everything, src_dublin_chamber() keeps only events that match one of
# these (word-boundary so "ai" doesn't match "fair"/"said", etc).
TECH_KEYWORDS = [
    r"\btech\b", r"\btechnology\b", r"\bstartup", r"\bsoftware\b", r"\bsaas\b",
    r"\bcyber\b", r"\bcybersecurity\b", r"\bartificial intelligence\b", r"\bai\b",
    r"\bgenai\b", r"\bgen ai\b", r"\bllm\b", r"\bmachine learning\b",
    r"\bdata science\b", r"\bbig data\b", r"\bautomation\b", r"\brobotic",
    r"\bcoding\b", r"\bdeveloper", r"\bprogramming\b", r"\bcloud computing\b",
    r"\bblockchain\b", r"\bfintech\b", r"\biot\b", r"\bapi\b",
    r"\bdigital transformation\b", r"\bweb development\b", r"\bapp development\b",
]


def is_tech_relevant(title, desc=""):
    text = (title + " " + (desc or "")).lower()
    return any(re.search(kw, text) for kw in TECH_KEYWORDS)


def in_metro(loc):
    # Require a positive match against a metro city/area name. Empty/unknown is
    # rejected — aggregators (dev.events, Eventbrite, Luma) list global events, so
    # "keep on unknown" would leak non-local events onto the calendar. Inherently
    # local single-org sources stamp a known city before this check (see src_generic).
    if not loc:
        return False
    l = loc.lower()
    return any(city in l for city in METRO)


def norm_key(title, date):
    t = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    t = " ".join(t.split()[:6])            # first ~6 words is enough to match
    return (date or "") + "|" + t


def url_id_key(url):
    """Pull a trailing numeric event ID out of a URL, e.g. .../register/12630 or
    .../details/taste-of-dublin-2026-12630 both -> '12630'. Several of our sources
    (Dublin Chamber/GrowthZone, Meetup) use stable numeric IDs even when the slug
    or link shape changes — a far more reliable dedupe key than title text, which
    Lauren often hand-rewrites when curating (see merge())."""
    if not url:
        return None
    m = re.search(r"(\d{4,})(?:[/?#]|$)", url.rstrip("/"))
    return m.group(1) if m else None


STOPWORDS = {"the", "a", "an", "at", "in", "on", "for", "with", "and", "of", "to",
             "2026", "2027", "2025", "columbus", "dublin", "event", "events"}


def title_overlap(a, b):
    """Loose word-overlap match for titles Lauren may have hand-rewritten while
    curating (e.g. curated 'Tucci's California Wine Tasting' vs. the source's own
    'A Taste of California: Tucci's September Wine Tasting'). Used only as a last
    resort after exact/URL-ID matching fails."""
    wa = set(re.sub(r"[^a-z0-9 ]", " ", (a or "").lower()).split()) - STOPWORDS
    wb = set(re.sub(r"[^a-z0-9 ]", " ", (b or "").lower()).split()) - STOPWORDS
    wa = {w for w in wa if len(w) > 2}
    wb = {w for w in wb if len(w) > 2}
    if not wa or not wb:
        return False
    overlap = wa & wb
    return len(overlap) >= 2 and len(overlap) >= 0.5 * min(len(wa), len(wb))


# ---------------------------------------------------------------------------
# fetch helper — polite, guarded, times out fast (plain HTTP, no JS)
# ---------------------------------------------------------------------------
def fetch(url, timeout=20, accept="text/html,application/json,application/xhtml+xml,*/*"):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        raw = r.read()
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# headless-browser rendering — only for sites that need JS to produce markup.
# Playwright is an optional dependency; if it isn't installed we raise a clear,
# actionable error that shows up as a SKIP line in the report rather than
# crashing the whole run. See RUNBOOK.md for the install step.
# ---------------------------------------------------------------------------
try:
    from playwright.sync_api import sync_playwright
    _HAVE_PLAYWRIGHT = True
except ImportError:
    _HAVE_PLAYWRIGHT = False

# Some environments (e.g. Claude's cloud sessions) ship a pre-installed Chromium
# at a fixed path but can't download the exact revision a freshly-pip-installed
# Playwright expects (network policy blocks cdn.playwright.dev) — launch()
# with no args then fails with "Executable doesn't exist at ...". If that path
# exists, point launch() at it explicitly instead of Playwright's own managed
# browser; otherwise fall back to default (normal `playwright install chromium`
# setups, e.g. Lauren's own machine per RUNBOOK.md).
_LOCAL_CHROMIUM = "/opt/pw-browsers/chromium"
_CHROMIUM_LAUNCH_KWARGS = (
    {"executable_path": _LOCAL_CHROMIUM} if os.path.exists(_LOCAL_CHROMIUM) else {}
)


def render_html(url, wait_ms=4000, wait_until="domcontentloaded", timeout=30000):
    """Load `url` in headless Chromium and return the fully-rendered HTML.
    Raises RuntimeError with an actionable message if Playwright/Chromium isn't
    installed, so the caller's try/except turns that into a clean SKIP."""
    if not _HAVE_PLAYWRIGHT:
        raise RuntimeError(
            "Playwright not installed — run: pip3 install playwright && "
            "python3 -m playwright install chromium (see RUNBOOK.md)"
        )
    with sync_playwright() as p:
        browser = p.chromium.launch(**_CHROMIUM_LAUNCH_KWARGS)
        try:
            page = browser.new_page(user_agent=UA_BROWSER)
            page.goto(url, timeout=timeout, wait_until=wait_until)
            page.wait_for_timeout(wait_ms)
            return page.content()
        finally:
            browser.close()


class RenderSession:
    """Reuse one headless browser across several page loads (e.g. a list page
    plus N event-detail pages) instead of paying browser-launch cost per page."""

    def __init__(self):
        if not _HAVE_PLAYWRIGHT:
            raise RuntimeError(
                "Playwright not installed — run: pip3 install playwright && "
                "python3 -m playwright install chromium (see RUNBOOK.md)"
            )
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(**_CHROMIUM_LAUNCH_KWARGS)
        self.page = self.browser.new_page(user_agent=UA_BROWSER)

    def get(self, url, wait_ms=3500, wait_until="domcontentloaded", timeout=30000):
        self.page.goto(url, timeout=timeout, wait_until=wait_until)
        self.page.wait_for_timeout(wait_ms)
        return self.page.content()

    def close(self):
        try:
            self.browser.close()
        finally:
            self._pw.stop()


# ---------------------------------------------------------------------------
# generic JSON-LD schema.org/Event extractor
# ---------------------------------------------------------------------------
def jsonld_events(page_html, default_url=None):
    """Pull schema.org Event objects out of <script type=application/ld+json> blocks.
    Recurses into ANY nested dict/list (not just @graph) so patterns like
    ItemList -> itemListElement -> item -> Event (common on Eventbrite, Luma, AI
    Tinkerers) are found regardless of the wrapping key name."""
    out = []
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                         page_html, re.S | re.I):
        blob = m.group(1).strip()
        try:
            data = json.loads(blob)
        except Exception:
            continue
        stack = [data]
        seen = 0
        while stack and seen < 5000:   # generous but bounded — jsonld blobs are small
            seen += 1
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            t = node.get("@type", "")
            t = t if isinstance(t, str) else (t[0] if isinstance(t, list) and t else "")
            if "event" in str(t).lower():
                ev = _event_from_jsonld(node, default_url)
                if ev:
                    out.append(ev)
            # recurse into every nested value — covers @graph, itemListElement,
            # item, or any other wrapper a site happens to use.
            for v in node.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
    return out


def _event_from_jsonld(node, default_url):
    start = node.get("startDate") or node.get("startdate")
    if not start:
        return None
    date = str(start)[:10]
    if not re.match(r"\d{4}-\d{2}-\d{2}", date):
        return None
    title = html.unescape(str(node.get("name", "")).strip())
    if not title:
        return None
    loc = node.get("location")
    loc_str = ""
    if isinstance(loc, dict):
        loc_str = loc.get("name", "")
        addr = loc.get("address")
        if isinstance(addr, dict):
            loc_str = (loc_str + " " + addr.get("addressLocality", "")).strip()
        elif isinstance(addr, str):
            loc_str = (loc_str + " " + addr).strip()
    elif isinstance(loc, str):
        loc_str = loc
    loc_str = html.unescape(loc_str).strip()
    url = node.get("url") or default_url or ""
    if isinstance(url, list):
        url = url[0] if url else ""
    desc = html.unescape(re.sub(r"<[^>]+>", "", str(node.get("description", "")))).strip()
    # offers -> cost / free
    cost = None
    free = None
    offers = node.get("offers")
    if isinstance(offers, dict):
        offers = [offers]
    if isinstance(offers, list):
        prices = []
        for o in offers:
            if isinstance(o, dict) and o.get("price") is not None:
                try:
                    prices.append(float(o["price"]))
                except (TypeError, ValueError):
                    pass
        if prices:
            if max(prices) == 0:
                free = True
            else:
                cost = "$%d" % min(p for p in prices if p > 0) if any(p > 0 for p in prices) else None
    ev = {"date": date, "title": title, "type": classify(title, desc),
          "time": "See event page", "loc": loc_str,
          "desc": desc[:280] or title, "url": url}
    if cost:
        ev["cost"] = cost
    if free:
        ev["free"] = True
    return ev


def fmt_time(dt):
    try:
        s = dt.strftime("%I:%M %p").lstrip("0")
        return s
    except Exception:
        return "See event page"


# ---------------------------------------------------------------------------
# per-source adapters
# Each returns a list of normalized event dicts, or raises (caught by caller).
# ---------------------------------------------------------------------------
def src_generic(url, local_default=None):
    """Fetch a page and pull schema.org Events from its JSON-LD. If a plain fetch
    turns up nothing, retry once via headless render — some sites only inject
    their JSON-LD after JS runs. Returns [] cleanly (not an error) if truly
    nothing structured is present either way.

    `local_default` stamps a city on events whose location the page didn't expose.
    Use it ONLY for inherently-local single-org sources — never for aggregators,
    whose unknown-location events could be anywhere."""
    page = fetch(url)
    evs = jsonld_events(page, default_url=url)
    if not evs:
        try:
            page = render_html(url)
            evs = jsonld_events(page, default_url=url)
        except RuntimeError:
            pass  # no Playwright available — plain-fetch result (possibly empty) stands
    if local_default:
        for e in evs:
            if not e.get("loc"):
                e["loc"] = local_default
    return evs


def src_ohiox():
    """OhioX runs on Squarespace's events collection (server-rendered — no JS
    needed). It's a statewide org (Toledo/Cleveland/Cincinnati/Columbus etc.), so
    we do NOT stamp a default city: the event title reliably names the city
    (e.g. 'OhioX Morning Tech: Toledo'), so we hand that text to in_metro() via
    the `loc` field and let the metro filter do its job. Events with no city
    named in the title are excluded rather than guessed at — safer than leaking
    a Toledo event onto a Columbus calendar."""
    page = fetch("https://www.ohiox.org/events")
    out = []
    for m in re.finditer(r'<article class="eventlist-event.*?</article>', page, re.S):
        block = m.group(0)
        title_m = re.search(r'eventlist-title-link[^>]*>([^<]+)</a>', block)
        date_m = re.search(r'class="event-date" datetime="(\d{4}-\d{2}-\d{2})"', block)
        href_m = re.search(r'<h1 class="eventlist-title"><a href="([^"]+)"', block)
        time_m = re.search(r'event-time-localized-start" datetime="[^"]*">([^<]+)</time>', block)
        excerpt_m = re.search(r'eventlist-excerpt">\s*(?:<p[^>]*>)?(.*?)(?:</p>)?\s*</div>', block, re.S)
        if not (title_m and date_m):
            continue
        title = html.unescape(title_m.group(1).strip())
        date = date_m.group(1)
        href = href_m.group(1) if href_m else ""
        url = ("https://www.ohiox.org" + href) if href.startswith("/") else (href or "https://www.ohiox.org/events")
        time_str = time_m.group(1).strip().replace(" ", " ") if time_m else "See event page"
        desc = html.unescape(re.sub(r"<[^>]+>", "", excerpt_m.group(1))).strip() if excerpt_m else title
        out.append({
            "date": date, "title": title, "type": classify(title, desc),
            "time": time_str, "loc": title,   # loc=title so in_metro() reads the city name
            "desc": desc[:280] or title, "url": url,
        })
    return out


def src_dublin_chamber():
    """Dublin Chamber (GrowthZone/ChamberMaster) loads its event list via an
    infinite-scroll partial (`/events/searchscroll`) — plain HTML, no JS needed,
    once you hit the right endpoint+params directly. We page through it until an
    empty page comes back.

    This is a general chamber-of-commerce calendar, not a tech one — we only
    keep events that look tech/startup-relevant (see TECH_KEYWORDS /
    is_tech_relevant), rather than pulling in the whole thing."""
    base = "https://www.dublinchamber.org"
    frm = TODAY.strftime("%-m/%-d/%Y") if os.name != "nt" else TODAY.strftime("%#m/%#d/%Y")
    to = HORIZON.strftime("%-m/%-d/%Y") if os.name != "nt" else HORIZON.strftime("%#m/%#d/%Y")
    events = []
    for page_num in range(1, 16):
        url = (base + "/events/searchscroll?page=%d&rendermode=partial&q=&c=&l=0&lookahead=&from=%s&to=%s"
               % (page_num, urllib.request.quote(frm), urllib.request.quote(to)))
        html_frag = fetch(url)
        if "gz-list-card-wrapper" not in html_frag:
            break
        found_this_page = 0
        for m in re.finditer(r'<div class="gz-list-card-wrapper.*?<!-- end of card-->', html_frag, re.S):
            block = m.group(0)
            title_m = re.search(r'gz-card-title">\s*<a href="([^"]+)">([^<]+)</a>', block)
            date_m = re.search(r'gz-card-date">.*?<span content="(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})"', block, re.S)
            desc_m = re.search(r'gz-events-description">([^<]*)</p>', block)
            reg_m = re.search(r'href="(https://www\.dublinchamber\.org/events/register/\d+)"', block)
            if not (title_m and date_m):
                continue
            found_this_page += 1
            url_ = reg_m.group(1) if reg_m else title_m.group(1)
            title = html.unescape(title_m.group(2).strip())
            date = date_m.group(1)
            desc = html.unescape(desc_m.group(1).strip()) if desc_m else title
            if not is_tech_relevant(title, desc):
                continue
            events.append({
                "date": date, "title": title, "type": classify(title, desc),
                "time": "See event page", "loc": "Dublin",
                "members": "Dublin Chamber event — member pricing may apply.",
                "desc": desc[:280] or title, "url": url_,
            })
        if found_this_page == 0:
            break
    return events


def src_meetup(slug, local_default=None):
    """Meetup group event pages embed full event data (title, time, venue, RSVP
    status) in a Next.js `__NEXT_DATA__` Apollo-cache blob — present even on a
    plain (non-JS) fetch. `status: "ACTIVE"` = upcoming; `"PAST"` = already
    happened. If the group has been renamed/removed, Meetup still returns 200
    with a client-rendered "Group not found" page — we detect that and raise a
    clear error instead of silently reporting zero events."""
    page = fetch("https://www.meetup.com/%s/events/" % slug)
    if "groupNotFoundTitle" in page or "Group not found" in page:
        raise RuntimeError("Meetup group '%s' not found (renamed or removed)" % slug)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not m:
        raise RuntimeError("no __NEXT_DATA__ found on Meetup page (layout changed?)")
    data = json.loads(m.group(1))
    state = data.get("props", {}).get("pageProps", {}).get("__APOLLO_STATE__", {}) or {}
    venues = {k: v for k, v in state.items() if k.startswith("Venue:")}
    out = []
    for k, ev in state.items():
        if not k.startswith("Event:") or ev.get("status") != "ACTIVE":
            continue
        dt = ev.get("dateTime", "")
        try:
            d = datetime.datetime.fromisoformat(dt)
        except Exception:
            continue
        loc = local_default or ""
        venue_ref = (ev.get("venue") or {}).get("__ref")
        if venue_ref and venue_ref in venues:
            v = venues[venue_ref]
            loc = ", ".join(x for x in [v.get("name", ""), v.get("city", "")] if x) or loc
        elif ev.get("isOnline"):
            loc = "Online"
        title = html.unescape(ev.get("title", "").strip())
        desc = html.unescape(re.sub(r"\s+", " ", ev.get("description", "") or "")).strip()
        out.append({
            "date": d.date().isoformat(), "title": title,
            "type": classify(title, desc), "time": fmt_time(d),
            "loc": loc, "desc": desc[:280] or title,
            "url": ev.get("eventUrl", "https://www.meetup.com/%s/events/" % slug),
        })
    return out


def src_witit_columbus():
    """getWITit's Columbus chapter page is a Webflow CMS collection — server-
    rendered, no JS needed. One upcoming-event card per regular event, plus an
    optional 'event-highlight-strip' promo banner used for one-off flagship
    events (e.g. an anniversary gala). The banner uses different markup and a
    year-less date ('Dec 12th') that we infer relative to TODAY."""
    page = fetch("https://getwitit.org/chapters/columbus")
    out = []
    for m in re.finditer(r'<a href="([^"]+)" class="upcoming-event_card[^"]*">(.*?)</a>', page, re.S):
        href, block = m.groups()
        title_m = re.search(r'event-card_title">([^<]+)</div>', block)
        date_m = re.search(r'text-block-2">([^<]+)</div>', block)
        time_m = re.search(r'text-block">([^<]+)</div>', block)
        city_m = re.search(r'text-block-3">([^<]+)</div>', block)
        if not (title_m and date_m):
            continue
        title = html.unescape(title_m.group(1).strip())
        try:
            d = datetime.datetime.strptime(date_m.group(1).strip(), "%b %d, %Y").date()
        except ValueError:
            continue
        city = html.unescape(city_m.group(1).strip()) if city_m else "Columbus"
        time_str = html.unescape(time_m.group(1).strip()) if time_m else "See event page"
        out.append({
            "date": d.isoformat(), "title": title, "type": classify(title),
            "time": time_str, "loc": city or "Columbus",
            "desc": "getWITit Columbus — " + title, "url": href,
        })

    months = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
    for m in re.finditer(
        r'event-highlight-strip[^"]*"><div><h3[^>]*>([^<]+)</h3>'
        r'<h4[^>]*>([^<]+)</h4><a href="([^"]+)"',
        page, re.S,
    ):
        title, subtitle, href = m.groups()
        title = html.unescape(title.strip())
        date_txt, _, loc_txt = html.unescape(subtitle).partition("●")
        date_m = re.search(r'([A-Za-z]+)\s+(\d{1,2})', date_txt)
        mon = months.get(date_m.group(1)[:3].lower()) if date_m else None
        if not mon:
            continue
        day = int(date_m.group(2))
        try:
            d = datetime.date(TODAY.year, mon, day)
        except ValueError:
            continue
        if d < TODAY:
            d = datetime.date(TODAY.year + 1, mon, day)
        out.append({
            "date": d.isoformat(), "title": title, "type": classify(title),
            "time": "See event page", "loc": loc_txt.strip() or "Columbus",
            "desc": "getWITit Columbus — " + title, "url": href,
        })
    return out


def src_worthington_chamber():
    """Worthington Chamber's site sits behind a Cloudflare Turnstile bot-check —
    the same challenge shows up whether we hit it with a plain fetch or headless
    Chromium (confirmed via manual check 2026-09-19). We surface that clearly
    rather than pretending 0 events means 'nothing scheduled'. See RUNBOOK.md for
    the documented fallback (manual quarterly check)."""
    html_out = render_html("https://www.worthingtonchamber.org/events", wait_ms=6000)
    if "Performing security verification" in html_out or "cf-chl" in html_out or "Turnstile" in html_out:
        raise RuntimeError("blocked by Cloudflare bot-check — see RUNBOOK.md known limitation")
    # If Cloudflare ever stands down for our UA, fall back to a generic JSON-LD pass.
    evs = jsonld_events(html_out, default_url="https://www.worthingtonchamber.org/events")
    for e in evs:
        e.setdefault("members", "Worthington Chamber event — member pricing may apply.")
        if not e.get("loc"):
            e["loc"] = "Worthington"
    return evs


def src_womenpm():
    """Women in Product's community site (Circle-based) lists ALL chapters'
    events on one national feed. The rendered page used to be our only option
    (hence the old per-event detail-page-render approach), but the site also
    exposes the exact same data as a plain, unauthenticated JSON API —
    `internal_api/spaces/<id>/posts` — which includes a already-geocoded
    `in_person_location.formatted_address` per event. That's both a more
    reliable metro signal than the rendered page's generic 'In person' text
    AND means this source needs no headless browser at all anymore.

    Space id 1322668 is 'All Local Events' — found via the page's own network
    requests (community.womenpm.org/c/all-local-events); it'll need updating
    if WiP ever migrates/re-IDs that space."""
    SPACE_ID = 1322668
    out = []
    page = 1
    while page <= 10:   # sane cap — current feed is ~1 page; leaves headroom
        url = ("https://community.womenpm.org/internal_api/spaces/%d/posts"
               "?past_events=false&used_on=posts&include_top_pinned_post=true&page=%d"
               % (SPACE_ID, page))
        data = json.loads(fetch(url, accept="application/json"))
        for r in data.get("records", []):
            esa = r.get("event_setting_attributes") or {}
            loc_raw = esa.get("in_person_location")
            loc = ""
            if loc_raw:
                try:
                    loc = json.loads(loc_raw).get("formatted_address", "") or ""
                except (TypeError, ValueError):
                    loc = ""
            elif esa.get("virtual_location_url"):
                loc = "Online"
            if not in_metro(loc):
                continue
            starts = esa.get("calendar_starts_at") or esa.get("starts_at")
            try:
                d = datetime.datetime.fromisoformat(starts)
            except (TypeError, ValueError):
                continue
            title = html.unescape((r.get("name") or "").strip())
            if not title:
                continue
            slug = r.get("slug", "")
            out.append({
                "date": d.date().isoformat(), "title": title,
                "type": classify(title), "time": fmt_time(d),
                "loc": loc, "desc": "Women in Product Columbus — " + title,
                "url": "https://community.womenpm.org/c/all-local-events/%s" % slug,
            })
        if not data.get("has_next_page"):
            break
        page += 1
    return out


# Nicer, public-facing host names for the calendar UI ("hosted by X"), keyed by
# SOURCES key. Falls back to the SOURCES label itself when a key isn't listed here.
HOST_LABELS = {
    "ohiox": "OhioX",
    "dublinchamber": "Dublin Chamber of Commerce",
    "startupgrind": "Startup Grind Columbus",
    "eventbrite": "Eventbrite",
    "luma_osn": "Luma",
    "devevents": "dev.events",
    "womenpm": "Women in Product — Columbus",
    "ixda": "IxDA Columbus",
    "producttank": "ProductTank Columbus",
    "columbusai": "Columbus AI",
    "aitinkerers": "AI Tinkerers Columbus",
    "techlife_mu": "TechLife Columbus",
    "witit": "Columbus WIT",
    "worthington": "Worthington Area Chamber of Commerce",
    "devops": "DevOps Columbus",
    "sparkworks": "SparkWorks Innovation",
}

SOURCES = {
    # key: (label, callable)
    "ohiox":        ("OhioX", src_ohiox),
    "dublinchamber":("Dublin Chamber", src_dublin_chamber),
    "startupgrind": ("Startup Grind / Rev1", lambda: src_meetup("startup-grind-columbus", local_default="Columbus")),
    "eventbrite":   ("Eventbrite Columbus", lambda: src_generic("https://www.eventbrite.com/d/oh--columbus/technology--events/")),
    "luma_osn":     ("Luma — Ohio Startup Network", lambda: src_generic("https://lu.ma/ohiostartupnetwork")),
    "devevents":    ("dev.events", lambda: src_generic("https://dev.events/north-america/us/oh")),
    "womenpm":      ("Women in Product Columbus (community.womenpm.org)", src_womenpm),
    "ixda":         ("IxDA Columbus (Meetup)", lambda: src_meetup("columbus-ixda-group", local_default="Columbus")),
    "producttank":  ("ProductTank Columbus (Meetup)", lambda: src_meetup("producttank-columbus", local_default="Columbus")),
    "columbusai":   ("Columbus AI (Meetup)", lambda: src_meetup("columbus-ai", local_default="Columbus")),
    "aitinkerers":  ("AI Tinkerers Columbus", lambda: src_generic("https://columbus.aitinkerers.org/", local_default="Columbus")),
    "techlife_mu":  ("TechLife (Meetup)", lambda: src_meetup("techlifecolumbus", local_default="Columbus")),
    "witit":        ("Columbus WIT (getWITit)", src_witit_columbus),
    # "worthington" disabled 2026-10-29 at Lauren's request — still blocked by a
    # Cloudflare Turnstile bot-check (src_worthington_chamber() is unchanged and
    # ready to re-enable once a workaround is found; see RUNBOOK.md).
    "devops":       ("DevOps Columbus (Meetup)", lambda: src_meetup("devops-columbus", local_default="Columbus")),
    "sparkworks":   ("SparkWorks Innovation", lambda: src_generic("https://sparkworksinnovation.com/event", local_default="Columbus")),
}


def within_window(ev):
    try:
        d = datetime.date.fromisoformat(ev["date"])
    except Exception:
        return False
    # keep upcoming within horizon; past events are handled separately (kept as-is)
    return TODAY <= d <= HORIZON


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------
def merge(existing, scraped):
    """Add genuinely-new events; fill gaps on matches. Never overwrite curated text.

    Matching, in order of confidence:
      1. Same numeric event ID embedded in both URLs (see url_id_key) — survives
         Lauren rewriting the title or the source changing its link slug.
      2. Same date + normalized first-6-words-of-title.
      3. Same date + loose word-overlap between titles (catches hand-rewritten
         curated titles like 'Tucci's California Wine Tasting' matching the
         source's own 'A Taste of California: Tucci's September Wine Tasting').
    """
    by_norm = {norm_key(e["title"], e["date"]): e for e in existing}
    by_id = {}
    for e in existing:
        uid = url_id_key(e.get("url", ""))
        if uid:
            by_id.setdefault((e.get("date", ""), uid), e)
    added, enriched = [], []

    def find_match(s):
        uid = url_id_key(s.get("url", ""))
        if uid:
            cur = by_id.get((s["date"], uid))
            if cur:
                return cur
        k = norm_key(s["title"], s["date"])
        if k in by_norm:
            return by_norm[k]
        for e in existing:
            if e.get("date") == s.get("date") and title_overlap(e.get("title", ""), s.get("title", "")):
                return e
        return None

    for s in scraped:
        if not in_metro(s.get("loc", "")):
            continue
        cur = find_match(s)
        if cur:
            for field in ("url", "cost", "members", "time", "desc"):
                if not cur.get(field) and s.get(field):
                    cur[field] = s[field]
                    if cur not in enriched:
                        enriched.append(cur)
        else:
            s.setdefault("_source", "scrape")
            s["_added"] = TODAY.isoformat()
            existing.append(s)
            k = norm_key(s["title"], s["date"])
            by_norm[k] = s
            uid = url_id_key(s.get("url", ""))
            if uid:
                by_id.setdefault((s["date"], uid), s)
            added.append(s)
    return added, enriched


def main():
    dry = "--dry-run" in sys.argv
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]

    existing = json.load(open(EVENTS_JSON, encoding="utf-8"))
    before = len(existing)

    all_scraped = []
    report = []
    for key, (label, fn) in SOURCES.items():
        if only and key != only:
            continue
        try:
            evs = fn() or []
            evs = [e for e in evs if within_window(e)]
            for e in evs:
                # Tag with the hosting org/group for display in the UI (e.g.
                # "Dublin Chamber of Commerce"). A source function may already
                # set a more precise host (e.g. from schema.org organizer.name);
                # only fall back to the source's own label when it hasn't.
                e.setdefault("host", HOST_LABELS.get(key, label))
            all_scraped.extend(evs)
            report.append("  OK    %-32s %2d upcoming event(s)" % (label, len(evs)))
        except urllib.error.HTTPError as e:
            report.append("  SKIP  %-32s HTTP %s" % (label, e.code))
        except urllib.error.URLError as e:
            report.append("  SKIP  %-32s network: %s" % (label, e.reason))
        except Exception as e:
            report.append("  SKIP  %-32s %s: %s" % (label, type(e).__name__, str(e)[:80]))

    added, enriched = merge(existing, all_scraped)

    print("scrape %s — horizon %s → %s" % (TODAY.isoformat(), TODAY.isoformat(), HORIZON.isoformat()))
    print("\n".join(report))
    print("  ---")
    print("  scraped(in-window): %d   new: %d   gap-filled: %d" %
          (len(all_scraped), len(added), len(enriched)))
    for a in added:
        print("     + %s  %s  (%s)" % (a["date"], a["title"][:50], a["type"]))

    if dry:
        print("  DRY RUN — events.json not written")
        return
    existing.sort(key=lambda e: (e.get("date", ""), e.get("title", "")))
    json.dump(existing, open(EVENTS_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("  wrote events.json (%d → %d events)" % (before, len(existing)))


if __name__ == "__main__":
    main()
