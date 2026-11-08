#!/usr/bin/env python3
"""
build.py — regenerate index.html + sitemap.xml from the JSON data files.

The page is 100% self-contained (inline CSS + JS, no runtime fetches), so the
event data has to live *inside* index.html as two JavaScript arrays. This script
is the "compiler": you edit the data in events.json / recurring.json, run build.py,
and it re-injects the arrays into index.html between marker comments, then stamps
the "last refreshed" date and the sitemap.

It is IDEMPOTENT: running it twice with the same JSON produces the same file.
It preserves everything else in index.html byte-for-byte (all markup, CSS, JS) —
it only rewrites the two marked data regions and a few dated values.

Usage:
    python3 build.py            # build using today's date
    python3 build.py --check    # verify index.html is already up to date (exit 1 if not)
"""
import json, re, sys, datetime, os
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(HERE, "index.html")
SITEMAP = os.path.join(HERE, "sitemap.xml")
EVENTS_JSON = os.path.join(HERE, "events.json")
RECURRING_JSON = os.path.join(HERE, "recurring.json")

MONTHS_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Field order used when emitting each object (optional fields skipped if empty)
EVENT_FIELDS = ["date", "title", "type", "host", "time", "loc", "cost", "members", "free", "desc", "url"]
RECUR_FIELDS = ["title", "type", "note", "url"]


def js_str(s):
    """Escape a Python string into a double-quoted JS string literal (keeps unicode literal)."""
    out = []
    for ch in str(s):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def emit_obj(obj, fields):
    parts = []
    for k in fields:
        v = obj.get(k)
        if v is None or v == "":
            continue
        # `free` is a boolean flag in the page's JS; emit as true if truthy
        if k == "free":
            parts.append("free:true")
        else:
            parts.append("%s:%s" % (k, js_str(v)))
    return "    {" + ",".join(parts) + "}"


def emit_array(var_name, items, fields):
    """Render a JS array literal: `  var NAME=[\\n    {..},\\n    {..}\\n  ];`"""
    lines = ["  var %s=[" % var_name]
    body = [emit_obj(o, fields) for o in items]
    lines.append(",\n".join(body))
    lines.append("  ];")
    return "\n".join(lines)


def replace_region(src, name, new_body):
    """
    Replace a `var NAME=[ ... \\n  ];` array assignment in the inline script.

    We anchor on the assignment itself (not on marker comments) so the build
    survives hand-edits to index.html: the array always closes with a newline +
    two-space indent + `];`, and no event/recurring object contains that
    sequence, so the non-greedy match lands on the real array end.
    """
    pat = re.compile(r"  var %s=\[.*?\n  \];" % name, re.S)
    if not pat.search(src):
        raise SystemExit("ERROR: could not find `var %s=[...]` in index.html" % name)
    # re.sub treats backslashes in the replacement specially — pass a function
    return pat.sub(lambda m: new_body, src, count=1)


ET = ZoneInfo("America/New_York")

# Suburbs checked BEFORE the "Columbus" default, so "7775 Walton Pkwy, New Albany"
# is labeled New Albany, not Columbus. Polaris/Easton/Short North are Columbus.
METRO_CITIES = [
    "New Albany", "Dublin", "Westerville", "Lewis Center", "Hilliard", "Delaware",
    "Upper Arlington", "Canal Winchester", "Worthington", "Powell", "Gahanna",
    "Grove City", "Reynoldsburg", "Bexley", "Grandview Heights", "Marysville",
    "Pickerington", "Plain City",
]

_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b|(\d{1,2})(?::(\d{2}))?(?=\s*[–—-])", re.I)


def _parse_times(text):
    """Pull start (and end, if a range) wall-clock times out of free text like
    '7:30 AM', '4:30–6:30pm', '8:30 AM – 1:00 PM', '8am - 5pm', 'Doors 7:00 AM'.
    Returns [(hour24, minute), ...] (0–2 items); [] when there's no clear time
    ('See event page', 'Full day'). A start with no am/pm borrows the end's."""
    out = []
    for m in _TIME_RE.finditer(text or ""):
        if m.group(1):
            out.append([int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()])
        else:
            out.append([int(m.group(4)), int(m.group(5) or 0), None])
        if len(out) == 2:
            break
    if not out or all(t[2] is None for t in out):
        return []
    if out[0][2] is None:
        out[0][2] = out[1][2]
    res = []
    for h, mi, ap in out:
        if ap is None or not (1 <= h <= 12) or mi > 59:
            return res[:1]
        res.append(((h % 12) + (12 if ap == "p" else 0), mi))
    return res


def _iso(date_str, hm):
    d = datetime.date.fromisoformat(date_str)
    return datetime.datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=ET).isoformat()


def _address(loc):
    """Best-effort PostalAddress from the free-text location: real suburb when
    named (else Columbus), plus ZIP when present."""
    text = loc or ""
    # City usually comes after the street, so the LAST suburb named wins
    # ("9200 Worthington Rd, Westerville" -> Westerville).
    hits = [(m.start(), c) for c in METRO_CITIES
            for m in re.finditer(r"\b%s\b" % re.escape(c), text, re.I)]
    city = max(hits)[1] if hits else "Columbus"
    addr = {"@type": "PostalAddress", "addressLocality": city, "addressRegion": "OH",
            "addressCountry": "US"}
    z = re.search(r"\b(43\d{3})\b", text)
    if z:
        addr["postalCode"] = z.group(1)
    return addr


def jsonld_events(events):
    """Build a schema.org Event list from events.json for SEO/AEO purposes.

    Emitted STATICALLY (baked into the HTML by build.py) rather than generated
    by client-side JS — best practice for structured data, since it stays
    visible to crawlers and answer-engines that don't execute JavaScript, and
    it's available the instant the page is fetched rather than after render.
    """
    out = []
    for e in events:
        item = {
            "@context": "https://schema.org",
            "@type": "Event",
            "name": e.get("title"),
            "startDate": e.get("date"),
            "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
            "eventStatus": "https://schema.org/EventScheduled",
            "location": {
                "@type": "Place",
                "name": e.get("loc") or "Columbus, OH",
                "address": _address(e.get("loc")),
            },
            "description": e.get("desc") or e.get("title"),
        }
        # Full date-time with Eastern offset when the time is parseable (Google
        # prefers it); date-only otherwise — never invent a time.
        times = _parse_times(e.get("time"))
        if times and e.get("date"):
            item["startDate"] = _iso(e["date"], times[0])
            if len(times) == 2 and times[1] > times[0]:
                item["endDate"] = _iso(e["date"], times[1])
        if e.get("url"):
            item["url"] = e["url"]
        if e.get("host"):
            item["organizer"] = {"@type": "Organization", "name": e["host"]}
        # Offers: only emit when price is ACTUALLY known — either explicitly
        # free, or a real number. An unstated cost is not the same thing as
        # "$0", so we omit the offers field entirely rather than guess; wrong
        # structured data (e.g. a paid event claimed as free) is worse for
        # SEO/AEO trust than no price data at all.
        cost = (e.get("cost") or "").strip()
        if e.get("free") or cost.lower() == "free":
            item["offers"] = {"@type": "Offer", "price": "0", "priceCurrency": "USD", "url": e.get("url", "")}
        elif re.search(r"\d", cost):
            price = re.sub(r"[^\d.]", "", cost)
            if price:
                item["offers"] = {"@type": "Offer", "price": price, "priceCurrency": "USD", "url": e.get("url", "")}
        # else: cost unknown — omit offers rather than assume free.
        out.append(item)
    return out


def replace_jsonld(src, events):
    """Replace the static JSON-LD block between the JSONLD:START/END HTML
    comments in <head> with a fresh <script type="application/ld+json"> tag
    built from events.json. See jsonld_events() for the schema shape."""
    payload = json.dumps(jsonld_events(events), ensure_ascii=False, indent=None, separators=(",", ":"))
    block = ('<!-- JSONLD:START — generated by build.py from events.json; do not hand-edit -->\n'
             '<script type="application/ld+json">' + payload + '</script>\n'
             '<!-- JSONLD:END -->')
    pat = re.compile(r"<!-- JSONLD:START.*?<!-- JSONLD:END -->", re.S)
    if not pat.search(src):
        raise SystemExit("ERROR: could not find JSONLD:START/END markers in index.html")
    return pat.sub(lambda m: block, src, count=1)


def build(today=None):
    today = today or datetime.date.today()
    events = json.load(open(EVENTS_JSON, encoding="utf-8"))
    recurring = json.load(open(RECURRING_JSON, encoding="utf-8"))

    # sort events by date so the source stays tidy and diffs stay small
    events.sort(key=lambda e: (e.get("date", ""), e.get("title", "")))

    src = open(HTML, encoding="utf-8").read()
    src = replace_region(src, "EVENTS", emit_array("EVENTS", events, EVENT_FIELDS))
    src = replace_region(src, "RECURRING", emit_array("RECURRING", recurring, RECUR_FIELDS))
    src = replace_jsonld(src, events)

    # --- dated values ---
    refreshed = "%s %d, %d" % (MONTHS_ABBR[today.month - 1], today.day, today.year)
    src = re.sub(r'(<b id="refreshed">)[^<]*(</b>)',
                 lambda m: m.group(1) + refreshed + m.group(2), src)

    # "Showing: Sep–Nov 2026" — current month through +2 months
    end = today.month - 1 + 2
    end_year = today.year + end // 12
    end_month = end % 12
    if end_year == today.year:
        showing = "%s&ndash;%s %d" % (MONTHS_ABBR[today.month - 1], MONTHS_ABBR[end_month], today.year)
    else:
        showing = "%s %d&ndash;%s %d" % (MONTHS_ABBR[today.month - 1], today.year,
                                         MONTHS_ABBR[end_month], end_year)
    src = re.sub(r'(<span>Showing: )[^<]*(</span>)',
                 lambda m: m.group(1) + showing + m.group(2), src)

    # --- sitemap lastmod ---
    smap = open(SITEMAP, encoding="utf-8").read()
    iso = today.isoformat()
    if "<lastmod>" in smap:
        smap = re.sub(r"<lastmod>[^<]*</lastmod>", "<lastmod>%s</lastmod>" % iso, smap)
    else:
        smap = smap.replace("<priority>1.0</priority>",
                            "<priority>1.0</priority>\n    <lastmod>%s</lastmod>" % iso)

    return src, smap


def main():
    check = "--check" in sys.argv
    src, smap = build()
    cur_html = open(HTML, encoding="utf-8").read()
    cur_smap = open(SITEMAP, encoding="utf-8").read()
    changed = (src != cur_html) or (smap != cur_smap)
    if check:
        print("up to date" if not changed else "OUT OF DATE — run build.py")
        sys.exit(0 if not changed else 1)
    if src != cur_html:
        open(HTML, "w", encoding="utf-8").write(src)
    if smap != cur_smap:
        open(SITEMAP, "w", encoding="utf-8").write(smap)
    n_ev = len(json.load(open(EVENTS_JSON, encoding="utf-8")))
    n_re = len(json.load(open(RECURRING_JSON, encoding="utf-8")))
    print("built index.html (%d events, %d recurring)%s" %
          (n_ev, n_re, "" if changed else " — no changes"))


if __name__ == "__main__":
    main()
