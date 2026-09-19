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

Standard library only — no pip installs. Many event sites are JavaScript-rendered;
where we can't get structured data (JSON-LD / an API / an ICS feed) we log the
source as needing a feed and move on rather than screen-scraping brittle markup.
See RUNBOOK.md for the per-source status + fallbacks.

Usage:
    python3 scrape.py                 # fetch, merge, write events.json
    python3 scrape.py --dry-run       # fetch + report, write nothing
    python3 scrape.py --only ohiox    # run a single source (dev/testing)
"""
import json, re, sys, os, ssl, html, datetime, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
EVENTS_JSON = os.path.join(HERE, "events.json")

TODAY = datetime.date.today()
HORIZON = TODAY + datetime.timedelta(days=95)   # ~3 months forward
UA = "CBusTechCircuitBot/1.0 (+https://github.com/ellieandraffie/cbus-tech-circuit) weekly community calendar"

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


# ---------------------------------------------------------------------------
# fetch helper — polite, guarded, times out fast
# ---------------------------------------------------------------------------
def fetch(url, timeout=20):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/json,application/xhtml+xml,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        raw = r.read()
    enc = "utf-8"
    return raw.decode(enc, errors="replace")


# ---------------------------------------------------------------------------
# generic JSON-LD schema.org/Event extractor
# ---------------------------------------------------------------------------
def jsonld_events(page_html, default_url=None):
    """Pull schema.org Event objects out of <script type=application/ld+json> blocks."""
    out = []
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                         page_html, re.S | re.I):
        blob = m.group(1).strip()
        try:
            data = json.loads(blob)
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            if "@graph" in node:
                stack.extend(node["@graph"] if isinstance(node["@graph"], list) else [node["@graph"]])
            t = node.get("@type", "")
            t = t if isinstance(t, str) else (t[0] if isinstance(t, list) and t else "")
            if "event" in str(t).lower():
                ev = _event_from_jsonld(node, default_url)
                if ev:
                    out.append(ev)
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


# ---------------------------------------------------------------------------
# per-source adapters
# Each returns a list of normalized event dicts, or raises (caught by caller).
# Sources that require an API key / are JS-only degrade to "generic JSON-LD try",
# which returns [] cleanly if nothing structured is present.
# ---------------------------------------------------------------------------
def src_generic(url, local_default=None):
    """Fetch a page and pull schema.org Events from its JSON-LD.

    `local_default` stamps a city on events whose location the page didn't expose.
    Use it ONLY for inherently-local single-org sources (OhioX, a specific Meetup
    group, etc.) — never for aggregators, whose unknown-location events could be
    anywhere in the world."""
    evs = jsonld_events(fetch(url), default_url=url)
    if local_default:
        for e in evs:
            if not e.get("loc"):
                e["loc"] = local_default
    return evs


def src_dublin_chamber():
    """Dublin Chamber runs on GrowthZone/ChamberMaster — server-rendered HTML with
    predictable /events/register/#### links. We pull the calendar list page and
    keep member/cost hints. This is the most scrape-friendly source."""
    base = "https://www.dublinchamber.org"
    page = fetch(base + "/events")
    events = jsonld_events(page, default_url=base + "/events")
    # also harvest register links + titles from list markup as a fallback
    for m in re.finditer(r'href="(/events/register/(\d+))"[^>]*>(.*?)</a>', page, re.S | re.I):
        link, _id, label = m.groups()
        label = html.unescape(re.sub(r"<[^>]+>", "", label)).strip()
        if not label or len(label) < 4:
            continue
        if any(e.get("url", "").endswith(link) for e in events):
            continue
        events.append({
            "date": "", "title": label, "type": classify(label),
            "time": "See event page", "loc": "Dublin",
            "members": "Dublin Chamber event — verify member vs. guest access.",
            "desc": label, "url": base + link,
        })
    # everything from the Chamber gets a members-only advisory unless already set
    for e in events:
        e.setdefault("members", "Dublin Chamber event — member pricing may apply.")
        if "dublin" not in e.get("loc", "").lower():
            e["loc"] = e.get("loc") or "Dublin"
    return [e for e in events if e.get("date")]  # drop dateless list-only rows


SOURCES = {
    # key: (label, callable)
    # Inherently-local single-org sources pass local_default so location-less
    # events still land in the metro. Aggregators do NOT — they stay strict.
    "ohiox":        ("OhioX", lambda: src_generic("https://www.ohiox.org/events", local_default="Columbus")),
    "techlife":     ("TechLife Columbus", lambda: src_generic("https://www.techlifecolumbus.com/events/", local_default="Columbus")),
    "dublinchamber":("Dublin Chamber", src_dublin_chamber),
    "startupgrind": ("Startup Grind / Rev1", lambda: src_generic("https://www.startupgrind.com/columbus/", local_default="Columbus")),
    "eventbrite":   ("Eventbrite Columbus", lambda: src_generic("https://www.eventbrite.com/d/oh--columbus/technology--events/")),
    "luma_osn":     ("Luma — Ohio Startup Network", lambda: src_generic("https://lu.ma/ohiostartupnetwork")),
    "devevents":    ("dev.events", lambda: src_generic("https://dev.events/north-america/us/oh")),
    "womenpm":      ("Women in Product Columbus (own site/Luma)",
                     lambda: src_generic("https://community.womenpm.org/c/all-local-events/", local_default="Columbus")),
    "ixda":         ("IxDA Columbus (Meetup)", lambda: src_generic("https://www.meetup.com/columbus-ixda-group/events/", local_default="Columbus")),
    "producttank":  ("ProductTank Columbus (Meetup)", lambda: src_generic("https://www.meetup.com/producttank-columbus/events/", local_default="Columbus")),
    "columbusai":   ("Columbus AI (Meetup)", lambda: src_generic("https://www.meetup.com/columbus-ai/events/", local_default="Columbus")),
    "aitinkerers":  ("AI Tinkerers Columbus", lambda: src_generic("https://columbus.aitinkerers.org/", local_default="Columbus")),
    "techlife_mu":  ("TechLife (Meetup)", lambda: src_generic("https://www.meetup.com/techlifecolumbus/events/", local_default="Columbus")),
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
    """Add genuinely-new events; fill gaps on matches. Never overwrite curated text."""
    by_key = {norm_key(e["title"], e["date"]): e for e in existing}
    added, enriched = [], []
    for s in scraped:
        if not in_metro(s.get("loc", "")):
            continue
        k = norm_key(s["title"], s["date"])
        if k in by_key:
            cur = by_key[k]
            for field in ("url", "cost", "members", "time", "desc"):
                if not cur.get(field) and s.get(field):
                    cur[field] = s[field]
                    if cur not in enriched:
                        enriched.append(cur)
        else:
            s.setdefault("_source", "scrape")
            s["_added"] = TODAY.isoformat()
            existing.append(s)
            by_key[k] = s
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
            all_scraped.extend(evs)
            report.append("  OK    %-32s %2d upcoming event(s)" % (label, len(evs)))
        except urllib.error.HTTPError as e:
            report.append("  SKIP  %-32s HTTP %s" % (label, e.code))
        except urllib.error.URLError as e:
            report.append("  SKIP  %-32s network: %s" % (label, e.reason))
        except Exception as e:
            report.append("  SKIP  %-32s %s: %s" % (label, type(e).__name__, str(e)[:60]))

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
