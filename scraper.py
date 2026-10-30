"""Scrape bike listings (with full descriptions) from standvirtual.com and olx.pt.

Both sites are server-rendered: the listing data is embedded as JSON in the HTML.
Standvirtual is fetched with plain HTTP requests; OLX blocks non-browser
clients, so its pages are loaded in headless Edge (Playwright).

Usage:  python scraper.py                  -> writes listings.json + listings.html
        python scraper.py --year-from 2022 -> only bikes registered 2022 or later
        python scraper.py --report-only    -> regenerates listings.html from the
                                              existing listings.json (no scraping)
        python scraper.py --serve          -> local web server for the report;
                                              its "Scrape" button re-runs the
                                              scraper with a chosen minimum year
                                              (optional: --port NNNN)

Cross-posted ads (the same bike published on both OLX and Standvirtual — the
sites belong to the same group) are detected and share a `duplicate_group` id;
the HTML report can collapse them to one entry per bike.
"""

import html as htmllib
import json
import math
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse

import requests
from playwright.sync_api import Error as PlaywrightError, sync_playwright

# ---------------------------------------------------------------- config

# (make, display name, model slug). Both sites share the same slugs; an
# unknown slug on Standvirtual silently falls back to *every* bike of the make,
# so check a new one there (e.g. /motos/bmw/r-1250-gsa) before adding it.
MODELS = [
    ("Honda", "Africa Twin", "africa-twin"),
    ("Yamaha", "Ténéré 700", "tenere-700"),
    ("BMW", "R 1250 GS", "r-1250-gs"),
    ("BMW", "R 1250 GS Adventure", "r-1250-gsa"),
    ("BMW", "R 1200 GS Adventure", "r-1200-gsa"),
    ("BMW", "F 900 GS Adventure", "f-900-gsa"),
    ("Ducati", "DesertX", "desertx"),
]

SEARCHES = [
    {
        "site": "standvirtual",
        "make": make,
        "model": model,
        "model_slug": slug,
        "url": f"https://www.standvirtual.com/motos/{make.lower()}/{slug}",
    }
    for make, model, slug in MODELS
] + [
    {
        "site": "olx",
        "make": make,
        "model": model,
        "model_slug": slug,
        "url": "https://www.olx.pt/carros-motos-e-barcos/motociclos-scooters/"
               f"{make.lower()}/?search%5Bfilter_enum_modelo%5D%5B0%5D={slug}",
    }
    for make, model, slug in MODELS
]

OUTPUT_FILE = "listings.json"
REQUEST_DELAY = 0.8  # seconds between requests, be polite
TIMEOUT = 30
RETRIES = 3
DEFAULT_PORT = 8777

# progress lines are mirrored here while a --serve scrape runs, so the report
# page can poll /status and show what the scraper is doing
LOG_SINK = None


def log(*args):
    msg = " ".join(str(a) for a in args)
    try:
        print(msg)
    except UnicodeEncodeError:
        # redirected stdout may be cp1252; never let a fancy character in an
        # ad title abort the scrape
        enc = sys.stdout.encoding or "utf-8"
        print(msg.encode(enc, "replace").decode(enc))
    if LOG_SINK is not None:
        LOG_SINK.append(msg)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)

# ---------------------------------------------------------------- http

session = requests.Session()
session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
})
_last_request = 0.0


def _get_requests(url):
    resp = session.get(url, timeout=TIMEOUT)
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}"
    return resp.text, None


# Since Oct 2026 olx.pt's firewall returns 403 to every non-browser client
# (python-requests and any curl build), so OLX pages are loaded in a real
# headless browser — the installed Edge, so no browser download is needed.
_browser = None  # (playwright, browser, page), started on the first OLX fetch


def _get_browser(url):
    global _browser
    if _browser is None:
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.launch(channel="msedge", headless=True)
        except Exception:
            pw.stop()
            raise
        _browser = (pw, browser, browser.new_page(locale="pt-PT"))
    resp = _browser[2].goto(url, wait_until="domcontentloaded", timeout=TIMEOUT * 1000)
    if resp is None or resp.status != 200:
        return None, f"HTTP {resp.status if resp else 'no response'}"
    return resp.text(), None


def close_browser():
    """Playwright objects belong to the thread that created them, and each
    --serve scrape runs in a fresh thread, so every run closes its own."""
    global _browser
    if _browser is not None:
        pw, browser, _ = _browser
        _browser = None
        browser.close()
        pw.stop()


def fetch(url):
    """GET a URL with polite delay and retries. Returns HTML text or None."""
    global _last_request
    getter = _get_browser if "olx.pt" in url else _get_requests
    for attempt in range(1, RETRIES + 1):
        wait = REQUEST_DELAY - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            _last_request = time.monotonic()
            text, error = getter(url)
            if text is not None:
                return text
            log(f"  ! {error} for {url} (attempt {attempt})")
        except (requests.RequestException, OSError, PlaywrightError) as exc:
            log(f"  ! {exc} for {url} (attempt {attempt})")
        time.sleep(2 * attempt)
    return None


def with_page(url, page):
    """Return url with ?page=N set (page 1 = original url)."""
    if page <= 1:
        return url
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["page"] = str(page)
    return urlunparse(parts._replace(query=urlencode(query)))


def search_start_url(search, year_from):
    """The search URL, optionally restricted to bikes registered >= year_from."""
    url = search["url"]
    if not year_from:
        return url
    parts = urlparse(url)
    if search["site"] == "standvirtual":
        return urlunparse(parts._replace(path=parts.path.rstrip("/") + f"/desde-{year_from}"))
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("search[filter_float_year:from]", str(year_from)))
    return urlunparse(parts._replace(query=urlencode(query)))

# ---------------------------------------------------------------- helpers

TAG_RE = re.compile(r"<[^>]+>")
BREAK_RE = re.compile(r"<\s*(br\s*/?|/p|/li|/div|/h[1-6])\s*>", re.I)


def strip_html(text):
    """Turn an HTML fragment into readable plain text."""
    if not text:
        return None
    text = BREAK_RE.sub("\n", text)
    text = TAG_RE.sub("", text)
    text = htmllib.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip() or None


def digits(value):
    """Extract an int from strings like '39.000 km' / '46739'. None if absent."""
    if value is None:
        return None
    found = re.sub(r"[^\d]", "", str(value))
    return int(found) if found else None

# ---------------------------------------------------------------- trade stance

# Sellers state in the description whether they take trades ("troca") or
# part-exchange ("retoma"). Patterns run over lowercase, accent-stripped text;
# refusals are checked first so "nao aceito trocas" never matches "aceito trocas".
TRADE_NO_RES = [re.compile(p) for p in (
    r"\bnao\s+(se\s+)?aceit\w+\s+(nem\s+)?(qualquer\s+)?(trocas?|retomas?)",
    r"\bnao\s+(e|sao)\s+aceites?\s+(trocas?|retomas?)",
    r"\bnao\s+(faco|fazemos|dou|damos|ha)\s+(trocas?|retomas?)",
    r"\bnao\s+se\s+faz(em)?\s+(trocas?|retomas?)",
    r"\bnao\s+(estou|estamos)\s+interessad\w+\s+em\s+(trocas?|retomas?)",
    r"\bnao\s+(tenho|temos)\s+interesse\s+(por|em)\s+(trocas?|retomas?)",
    r"\bsem\s+interesse\s+(por|em)\s+(trocas?|retomas?)",
    r"\bnao\s+troco\b",
    r"\bsem\s+(trocas?|retomas?)\b",
    r"\bsem\s+possibilidade\s+de\s+(trocas?|retomas?)",
    r"\b(trocas?|retomas?)\s+nao\b",
    r"\b(trocas?|retomas?)\s+excluid\w+",
    r"\bnada\s+de\s+(trocas?|retomas?)",
    r"\bdispenso\s+(trocas?|retomas?)",
)]
TRADE_YES_RES = [re.compile(p) for p in (
    r"\baceit[a-z]*(-se)?\s+(propostas?\s+de\s+)?(trocas?|retomas?)",
    r"\bpossibilidade\s+de\s+(trocas?|retomas?)",
    r"\b(trocas?|retomas?)\s+possive(l|is)",
    r"\bestud(o|amos)\s+(trocas?|retomas?)",
    r"\b(faco|fazemos)\s+retomas?\b",
    r"\babert[oa]s?\s+a\s+(trocas?|retomas?)",
    r"\binteress\w*\s+(por|em)\s+(trocas?|retomas?)",
    r"\b(troco|trocamos)\s+por\b",
)]


def deaccent(text):
    """Lowercase and strip accents, keeping a 1:1 character mapping."""
    return "".join(unicodedata.normalize("NFD", c)[0] for c in text).lower()


NEGATOR_RE = re.compile(r"\b(nao|nunca|sem|nem)\b")


def classify_trades(description):
    """Return (stance, snippet): stance is 'no', 'yes' or None (unstated)."""
    if not description:
        return None, None
    text = deaccent(description)

    def snippet(m):
        s = description[max(0, m.start() - 40):m.end() + 40].strip()
        return re.sub(r"\s+", " ", s)

    for rx in TRADE_NO_RES:
        m = rx.search(text)
        if m:
            return "no", snippet(m)
    for rx in TRADE_YES_RES:
        m = rx.search(text)
        if m:
            # a negator shortly before the acceptance phrase, in the same
            # sentence, flips it to a refusal: "nao ha possibilidade de troca",
            # "nao aceito propostas de troca", "nao estou aberto a trocas", ...
            prefix = re.split(r"[.!?;\n]", text[max(0, m.start() - 40):m.start()])[-1]
            return ("no" if NEGATOR_RE.search(prefix) else "yes"), snippet(m)
    return None, None

# ---------------------------------------------------------------- standvirtual

def sv_next_data(page_html):
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
        page_html, re.S)
    return json.loads(m.group(1)) if m else None


def sv_advert_search(page_html):
    """Extract the advertSearch object from a Standvirtual search page."""
    data = sv_next_data(page_html)
    if not data:
        return None
    urql = data.get("props", {}).get("pageProps", {}).get("urqlState", {})
    for entry in urql.values():
        payload = entry.get("data")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                continue
        if isinstance(payload, dict) and "advertSearch" in payload:
            return payload["advertSearch"]
    return None


def sv_param(node, key):
    for p in node.get("parameters") or []:
        if p.get("key") == key:
            return p.get("value")
    return None


def sv_description(ad_url):
    """Fetch a Standvirtual ad page and return its plain-text description."""
    page_html = fetch(ad_url)
    if not page_html:
        return None
    data = sv_next_data(page_html)
    if not data:
        return None
    advert = data.get("props", {}).get("pageProps", {}).get("advert") or {}
    return strip_html(advert.get("description"))


def scrape_standvirtual(search, year_from=None):
    listings = []
    base_url = search_start_url(search, year_from)
    log(f"[standvirtual] {search['make']} {search['model']}")
    first = fetch(base_url)
    if not first:
        log("  ! could not fetch search page, skipping")
        return listings
    result = sv_advert_search(first)
    if not result:
        log("  ! could not find advertSearch data, skipping")
        return listings

    total = result.get("totalCount") or 0
    page_size = (result.get("pageInfo") or {}).get("pageSize") or 32
    pages = max(1, math.ceil(total / page_size))
    log(f"  {total} ads across {pages} page(s)")

    nodes, seen = [], set()
    for page in range(1, pages + 1):
        if page > 1:
            page_html = fetch(with_page(base_url, page))
            result = sv_advert_search(page_html) if page_html else None
            if not result:
                log(f"  ! page {page} failed, skipping it")
                continue
        for edge in result.get("edges") or []:
            node = edge.get("node") or {}
            if node.get("id") and node["id"] not in seen:
                seen.add(node["id"])
                nodes.append(node)

    for i, node in enumerate(nodes, 1):
        url = node.get("url")
        log(f"  [{i}/{len(nodes)}] {(node.get('title') or '')[:60]}")
        price = ((node.get("price") or {}).get("amount") or {})
        location = node.get("location") or {}
        city = (location.get("city") or {}).get("name")
        region = (location.get("region") or {}).get("name")
        seller_typename = (node.get("seller") or {}).get("__typename") or ""
        engine_cc = sv_param(node, "engine_capacity")
        listings.append({
            "site": "standvirtual",
            "make": search["make"],
            "model": search["model"],
            "ad_id": str(node.get("id")),
            "title": node.get("title"),
            "price_eur": digits(price.get("units")),
            "year": digits(sv_param(node, "first_registration_year")),
            "mileage_km": digits(sv_param(node, "mileage")),
            "engine": engine_cc and f"{engine_cc} cc",
            "location": ", ".join(x for x in (city, region) if x) or None,
            "seller_type": "dealer" if "Professional" in seller_typename else "private",
            "url": url,
            "thumbnail": (node.get("thumbnail") or {}).get("x1"),
            "cross_post_url": None,
            "description": sv_description(url) if url else None,
            "published_at": node.get("createdAt"),
        })
    return listings

# ---------------------------------------------------------------- olx

def olx_state(page_html):
    """Extract the __PRERENDERED_STATE__ JSON from an OLX page."""
    i = page_html.find("__PRERENDERED_STATE__")
    if i == -1:
        return None
    j = page_html.find('"', i)
    if j == -1:
        return None
    try:
        outer, _ = json.JSONDecoder().raw_decode(page_html[j:])
        return json.loads(outer)
    except json.JSONDecodeError:
        return None


def olx_param(ad, key, normalized=False):
    for p in ad.get("params") or []:
        if p.get("key") == key:
            return p.get("normalizedValue") if normalized else p.get("value")
    return None


def scrape_olx(search, year_from=None):
    listings, seen = [], set()
    base_url = search_start_url(search, year_from)
    log(f"[olx] {search['make']} {search['model']}")
    page, total_pages = 1, 1
    while page <= total_pages:
        page_html = fetch(with_page(base_url, page))
        state = olx_state(page_html) if page_html else None
        listing = (state or {}).get("listing", {}).get("listing", {})
        ads = listing.get("ads") or []
        if not listing and page == 1:  # an empty listing is a valid 0-ad result
            log("  ! could not read listing data, skipping")
            return listings
        if page == 1:
            total_pages = listing.get("totalPages") or 1
            log(f"  {listing.get('totalElements')} ads across {total_pages} page(s)")

        for ad in ads:
            ad_id = str(ad.get("id"))
            if ad_id in seen:
                continue  # promoted ads repeat across pages
            seen.add(ad_id)
            # OLX pads short result sets with loosely related ads; keep on-model only
            if olx_param(ad, "modelo", normalized=True) != search["model_slug"]:
                continue
            price = (ad.get("price") or {}).get("regularPrice") or {}
            location = ad.get("location") or {}
            city = location.get("cityName")
            region = location.get("regionName")
            listings.append({
                "site": "olx",
                "make": search["make"],
                "model": search["model"],
                "ad_id": ad_id,
                "title": ad.get("title"),
                "price_eur": price.get("value"),
                "year": digits(olx_param(ad, "year")),
                "mileage_km": digits(olx_param(ad, "quilometros")),
                "engine": olx_param(ad, "cilindrada"),
                "location": ", ".join(x for x in (city, region) if x) or None,
                "seller_type": "dealer" if ad.get("isBusiness") else "private",
                "url": ad.get("url"),
                "thumbnail": (ad.get("photos") or [None])[0],
                # cross-posted ads carry a link to the same ad on standvirtual
                "cross_post_url": ad.get("externalUrl"),
                "description": strip_html(ad.get("description")),
                "published_at": ad.get("createdTime"),
            })
        page += 1
    return listings

# ---------------------------------------------------------------- duplicates

def mark_duplicates(listings):
    """Group listings that are the same physical bike.

    Signals, in order of strength:
      1. hard link: an OLX ad's cross_post_url pointing at a Standvirtual ad
      2. heuristic: identical (model, year, price, mileage)
    Every listing gets a `duplicate_group` (null when unique).
    """
    parent = list(range(len(listings)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        parent[find(i)] = find(j)

    by_url = {x["url"]: i for i, x in enumerate(listings) if x.get("url")}
    for i, x in enumerate(listings):
        target = x.get("cross_post_url")
        if target and target in by_url:
            union(i, by_url[target])

    by_key = {}
    for i, x in enumerate(listings):
        key = (x["model"], x["year"], x["price_eur"], x["mileage_km"])
        if all(v is not None for v in key):
            by_key.setdefault(key, []).append(i)
    for idxs in by_key.values():
        for j in idxs[1:]:
            union(idxs[0], j)

    groups = {}
    for i in range(len(listings)):
        groups.setdefault(find(i), []).append(i)

    n_groups = n_flagged = 0
    for members in groups.values():
        if len(members) > 1:
            n_groups += 1
            n_flagged += len(members)
            for m in members:
                listings[m]["duplicate_group"] = f"g{n_groups:02d}"
    for x in listings:
        x.setdefault("duplicate_group", None)
    return n_groups, n_flagged

# ---------------------------------------------------------------- html report

REPORT_FILE = "listings.html"

TEMPLATE_FILE = "report_template.html"  # the report UI, kept next to this script

def write_text(path, text):
    """Atomic write — --serve reads these files concurrently, so they must
    never be observable half-written."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def write_report(output):
    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), TEMPLATE_FILE)
    with open(template_path, encoding="utf-8") as fh:
        template = fh.read()
    # <-escape every "<" so listing text can never terminate or corrupt
    # the <script> data block (covers </script> and the <!--<script trick)
    payload = json.dumps(output, ensure_ascii=False).replace("<", "\\u003c")
    write_text(REPORT_FILE, template.replace("__DATA__", payload))


def write_output(output):
    write_text(OUTPUT_FILE, json.dumps(output, ensure_ascii=False, indent=2))

# ---------------------------------------------------------------- main

def apply_trade_stance(listings):
    for x in listings:
        x["trade_stance"], x["trade_note"] = classify_trades(x.get("description"))


def run_scrape(year_from=None):
    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if year_from:
        log(f"Scraping bikes registered {year_from} or later")
    all_listings = []
    try:
        for search in SEARCHES:
            scraper = scrape_standvirtual if search["site"] == "standvirtual" else scrape_olx
            all_listings.extend(scraper(search, year_from))
    finally:
        close_browser()

    for item in all_listings:
        item["scraped_at"] = scraped_at
    all_listings.sort(key=lambda x: (x["site"], x["model"], x["price_eur"] or 0))
    n_groups, n_flagged = mark_duplicates(all_listings)
    apply_trade_stance(all_listings)

    output = {
        "scraped_at": scraped_at,
        "year_from": year_from,
        "total": len(all_listings),
        "duplicate_groups": n_groups,
        "listings": all_listings,
    }
    write_output(output)
    write_report(output)

    log("\n=== Summary ===")
    for search in SEARCHES:
        count = sum(1 for x in all_listings
                    if x["site"] == search["site"] and x["model"] == search["model"])
        missing = sum(1 for x in all_listings
                      if x["site"] == search["site"] and x["model"] == search["model"]
                      and not x["description"])
        note = f" ({missing} without description)" if missing else ""
        name = f"{search['make']} {search['model']}"
        log(f"{search['site']:>13}  {name:<24} {count:>4} listings{note}")
    log(f"duplicates: {n_flagged} listings in {n_groups} cross-post groups")
    refuses = sum(1 for x in all_listings if x["trade_stance"] == "no")
    accepts = sum(1 for x in all_listings if x["trade_stance"] == "yes")
    log(f"trades: {refuses} refuse, {accepts} explicitly accept, "
          f"{len(all_listings) - refuses - accepts} unstated")
    log(f"\nWrote {len(all_listings)} listings to {OUTPUT_FILE} and {REPORT_FILE}")
    return output

# ---------------------------------------------------------------- serve

def serve(port):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs

    state = {"running": False, "log": [], "error": None}
    lock = threading.Lock()

    def scrape_thread(year_from):
        global LOG_SINK
        try:
            run_scrape(year_from)
        except Exception as exc:  # surface any failure to the page
            state["error"] = str(exc)
            log(f"! scrape failed: {exc}")
        finally:
            LOG_SINK = None
            state["running"] = False

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.partition("?")[0]
            if path in ("/", "/listings.html"):
                if not os.path.exists(REPORT_FILE):
                    self._send(200, "<p>No report yet — POST /scrape or run the scraper once.</p>",
                               "text/html")
                    return
                with open(REPORT_FILE, "rb") as fh:
                    self._send(200, fh.read(), "text/html")
            elif path == "/listings.json" and os.path.exists(OUTPUT_FILE):
                with open(OUTPUT_FILE, "rb") as fh:
                    self._send(200, fh.read())
            elif path == "/status":
                self._send(200, json.dumps({
                    "running": state["running"],
                    "error": state["error"],
                    "log": state["log"][-30:],
                }))
            else:
                self._send(404, '{"error": "not found"}')

        def do_POST(self):
            global LOG_SINK
            path, _, query = self.path.partition("?")
            if path != "/scrape":
                self._send(404, '{"error": "not found"}')
                return
            # a web page from any site can POST to localhost — only accept
            # requests from the report itself (or tools like curl, which send
            # no Origin header)
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
                self._send(403, '{"error": "forbidden origin"}')
                return
            params = parse_qs(query)
            raw = (params.get("year_from") or [""])[0].strip()
            if raw and not (raw.isdecimal() and 1970 <= int(raw) <= 2100):
                self._send(400, '{"error": "year_from must be a year like 2022"}')
                return
            with lock:
                if state["running"]:
                    self._send(409, '{"error": "a scrape is already running"}')
                    return
                state.update(running=True, error=None, log=[])
                LOG_SINK = state["log"]
            threading.Thread(target=scrape_thread,
                             args=(int(raw) if raw else None,), daemon=True).start()
            self._send(200, '{"started": true}')

        def log_message(self, *args):  # keep the console for scrape progress only
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Serving the report at {url}  (Ctrl+C to stop)")
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass
    server.serve_forever()

# ---------------------------------------------------------------- cli

def main():
    args = sys.argv[1:]
    known = {"--serve", "--report-only", "--year-from", "--port"}
    unknown = [a for a in args if a.startswith("-") and a not in known]
    if unknown:
        sys.exit(f"Unknown option(s): {' '.join(unknown)}\n"
                 "Valid options: --year-from N, --report-only, --serve, --port N")

    def int_after(flag, default):
        if flag in args:
            try:
                return int(args[args.index(flag) + 1])
            except (IndexError, ValueError):
                sys.exit(f"{flag} needs a number, e.g. {flag} 2022")
        return default

    if "--serve" in args:
        serve(int_after("--port", DEFAULT_PORT))
        return
    if "--report-only" in args:
        # regenerate outputs from the existing JSON, re-running the trade
        # classifier so pattern improvements apply without a re-scrape
        with open(OUTPUT_FILE, encoding="utf-8") as fh:
            output = json.load(fh)
        apply_trade_stance(output["listings"])
        write_output(output)
        write_report(output)
        log(f"Rebuilt {REPORT_FILE} from {OUTPUT_FILE} ({output['total']} listings)")
        return
    year_from = int_after("--year-from", None)
    if year_from is not None and not 1970 <= year_from <= 2100:
        sys.exit("--year-from must be a year between 1970 and 2100")
    run_scrape(year_from)


if __name__ == "__main__":
    main()
