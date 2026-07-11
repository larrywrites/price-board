#!/usr/bin/env python3
"""
Daily price scraper for The Price Board.

Run by GitHub Actions on a schedule. Fetches produce prices and writes
them to docs/deals.json, which the static website reads.

    python3 scrape.py           # live fetch
    python3 scrape.py --demo    # write sample data (for testing)

Standard library only — no installs needed.
"""

import argparse
import gzip
import json
import re
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from http.cookiejar import CookieJar
from pathlib import Path

# curl_cffi impersonates a real Chrome TLS fingerprint — the main thing
# (beyond IP reputation) that Akamai/Imperva bot protection checks.
# The GitHub Actions workflow installs it; everything still works
# without it via plain urllib, just with worse odds at the supermarkets.
try:
    from curl_cffi import requests as cffi
    HAVE_CFFI = True
except ImportError:
    HAVE_CFFI = False

OUT = Path(__file__).parent / "docs" / "deals.json"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Woolworths is searched for every produce item we track, so the website's
# month switcher stays honest year-round.
SEARCH_TERMS = [
    "apple", "pear", "orange", "blood orange", "mandarin", "lemon",
    "grapefruit", "kiwifruit", "blackberry", "papaya", "banana",
    "strawberry", "mango", "avocado", "brussels sprouts", "wombok",
    "cabbage", "broccoli", "broccolini", "cauliflower", "carrot", "celery",
    "pumpkin", "sweet potato", "potato", "silverbeet", "kale", "leek",
    "fennel", "parsnip", "beetroot", "zucchini", "capsicum", "tomato",
    "spinach", "mushroom", "onion", "spring onion", "corn", "cucumber",
    "eggplant", "asparagus", "green beans", "snow peas", "watermelon",
    "rockmelon", "pineapple", "grapes", "peach", "nectarine", "plum",
    "cherries", "blueberry", "raspberry", "lime", "rhubarb", "turnip",
    "radish", "bok choy", "ginger", "garlic",
]

# Anything processed, jarred, prepared, or non-edible that sneaks into
# fruit & veg categories gets dropped before it ever reaches the site.
FRESH_EXCLUDE = re.compile(
    r"juice|dried|frozen|chips|soup|puree|passata|paste|sauce|jam\b|canned|"
    r"tinned|roasted|pickled|marinated|antipast|olive|brine|jar\b|jarred|"
    r"bottle|stock|broth|dip\b|hummus|pesto|salsa|relish|chutney|preserved|"
    r"candied|glac|coulis|syrup|nectar|coconut water|coconut milk|vinegar|"
    r"crumbed|battered|ready|meal\b|kit\b|salad kit|slaw|stir.?fry mix|"
    r"smoothie|snack|bar\b|powder|kimchi|sauerkraut|fermented|noodle|"
    r"wrap\b|sushi|plant\b|seedling|flowers|bouquet|hamper|box\b|platter|"
    r"bread|cake|muffin|loaf|pancake|waffle|doughnut|donut|scone|brownie|cookie|biscuit|pudding|custard\b|yoghurt|yogurt|gelato|sorbet|ice.?cream|chocolate|choc\b|lolly|lollies|candy|fritter|pastry|pie\b|crumble|danish|croissant",
    re.I)

KG_PATTERNS = [
    (re.compile(r"(\d+(?:\.\d+)?)\s*kg", re.I), 1.0),
    (re.compile(r"(\d+(?:\.\d+)?)\s*g\b", re.I), 0.001),
]


def price_per_kg(name, price):
    for pattern, to_kg in KG_PATTERNS:
        m = pattern.search(name)
        if m:
            weight = float(m.group(1)) * to_kg
            if weight > 0:
                return round(price / weight, 2)
    if re.search(r"\bper\s*kg\b|\bp/?kg\b|\(kg\)|\bkg\b", name, re.I):
        return round(price, 2)
    return None


def new_session():
    """Cookie-carrying session: Chrome-impersonating when curl_cffi is
    installed, plain urllib otherwise."""
    if HAVE_CFFI:
        return cffi.Session(impersonate="chrome")
    jar = CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def http_text(session, url, headers=None, timeout=25):
    """GET a page as text (used for the Coles homepage / build ID)."""
    if HAVE_CFFI and isinstance(session, cffi.Session):
        r = session.get(url, headers=headers or {}, timeout=timeout)
        r.raise_for_status()
        return r.text
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with session.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def http_json(url, payload=None, session=None, headers=None, timeout=25):
    if HAVE_CFFI and (session is None or isinstance(session, cffi.Session)):
        s = session or cffi.Session(impersonate="chrome")
        if payload is not None:
            r = s.post(url, json=payload, headers=headers or {}, timeout=timeout)
        else:
            r = s.get(url, headers={"Accept": "application/json", **(headers or {})},
                      timeout=timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}")
        return r.json()
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    req.add_header("Accept-Encoding", "gzip")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        req.add_header("Content-Type", "application/json")
    op = session or urllib.request.build_opener()
    with op.open(req, data=data, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw)


def fetch_shopify(domain, retailer, max_pages=20):
    results = []
    types = ("fruit", "vegetable", "veg", "produce", "fresh")
    for page in range(1, max_pages + 1):
        url = f"https://{domain}/products.json?limit=250&page={page}"
        try:
            data = http_json(url)
        except Exception as exc:
            print(f"  [{retailer}] page {page} failed: {exc}")
            break
        products = data.get("products", [])
        if not products:
            break
        for p in products:
            haystack = (p.get("product_type", "") + " "
                        + " ".join(p.get("tags", []))).lower()
            if not any(t in haystack for t in types):
                continue
            for v in p.get("variants", []):
                try:
                    price = float(v.get("price", 0))
                except (TypeError, ValueError):
                    continue
                if price <= 0 or not v.get("available", True):
                    continue
                name = p["title"]
                if v.get("title") and v["title"] != "Default Title":
                    name += " " + v["title"]
                results.append({
                    "retailer": retailer, "name": name, "price": price,
                    "price_per_kg": price_per_kg(name, price),
                    "url": f"https://{domain}/products/{p['handle']}",
                })
        time.sleep(0.6)
    print(f"  [{retailer}] {len(results)} produce items")
    return results


def fetch_woolworths(terms):
    results = []
    session = new_session()
    try:  # warm-up visit to collect cookies
        http_text(session, "https://www.woolworths.com.au/")
    except Exception as exc:
        print(f"  [Woolworths] warm-up failed ({exc}) — continuing anyway")
    failures = 0
    for term in terms:
        try:
            data = http_json(
                "https://www.woolworths.com.au/apis/ui/Search/products",
                payload={"SearchTerm": term, "PageSize": 24, "PageNumber": 1,
                         "SortType": "PriceAsc", "Filters": []},
                session=session,
                headers={"Origin": "https://www.woolworths.com.au",
                         "Referer": "https://www.woolworths.com.au/shop/browse/fruit-veg"})
        except Exception as exc:
            failures += 1
            print(f"  [Woolworths] '{term}' failed: {exc}")
            if failures >= 5 and not results:
                print("  [Woolworths] first 5 queries all blocked — "
                      "bot protection is rejecting this IP, skipping the rest")
                break
            time.sleep(1.2)
            continue
        for group in data.get("Products", []):
            for prod in group.get("Products", []):
                price = prod.get("Price")
                if not price:
                    continue
                cup = prod.get("CupString", "") or ""
                ppk = None
                m = re.search(r"\$([\d.]+)\s*/\s*1\s*KG", cup, re.I)
                if m:
                    ppk = float(m.group(1))
                name = prod.get("DisplayName", prod.get("Name", ""))
                results.append({
                    "retailer": "Woolworths", "name": name,
                    "price": float(price),
                    "price_per_kg": ppk or price_per_kg(name, float(price)),
                    "url": "https://www.woolworths.com.au/shop/productdetails/"
                           + str(prod.get("Stockcode", "")),
                })
        time.sleep(1.2)
    print(f"  [Woolworths] {len(results)} items")
    return results


def fetch_woocommerce(domain, retailer, max_pages=30):
    """WooCommerce Store API — the public product feed for WordPress shops.
    Panetta Mercato runs WooCommerce, not Shopify (its /product-category/
    URLs are the giveaway), so it needs this fetcher instead.

    Prices arrive in minor units, e.g. {"price": "450", "currency_minor_unit": 2}
    means $4.50. Categories are matched against Panetta's real produce
    category names."""
    results = []
    produce_cats = (
        "fruit", "vegetable", "veg", "produce", "berries", "citrus", "grapes",
        "melons", "stone fruit", "banana", "apples", "pears", "brassicas",
        "beans & peas", "capsicums", "chillies", "hard vegetables", "lettuce",
        "mushrooms", "tomatoes", "cucumbers", "asian vegetables", "herbs fresh",
    )
    for page in range(1, max_pages + 1):
        url = (f"https://{domain}/wp-json/wc/store/v1/products"
               f"?per_page=100&page={page}")
        try:
            products = http_json(url)
        except Exception as exc:
            print(f"  [{retailer}] page {page} failed: {exc}")
            break
        if not isinstance(products, list) or not products:
            break
        page_sig = products[0].get("name"), len(products)
        if page == 1:
            prev_sig = page_sig
        elif page_sig == prev_sig:
            print(f"  [{retailer}] page {page} identical to previous — "
                  "server ignoring pagination, stopping")
            break
        else:
            prev_sig = page_sig
        for p in products:
            cats = " ".join(
                (c.get("name") or "").lower()
                for c in (p.get("categories") or [])
            )
            if not any(k in cats for k in produce_cats):
                continue
            if p.get("is_in_stock") is False:
                continue
            prices = p.get("prices") or {}
            raw = prices.get("price")
            if not raw:
                continue
            try:
                minor = int(prices.get("currency_minor_unit", 2))
                price = round(int(raw) / (10 ** minor), 2)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            name = re.sub(r"<[^>]+>", "", p.get("name") or "").strip()
            if not name:
                continue
            results.append({
                "retailer": retailer, "name": name, "price": price,
                "price_per_kg": price_per_kg(name, price),
                "url": p.get("permalink") or "",
            })
        time.sleep(0.6)
    print(f"  [{retailer}] {len(results)} produce items")
    return results


def fetch_coles(terms):
    """Coles' internal Next.js search API. The most aggressively
    bot-protected of the lot (Imperva) — expect frequent failures from
    datacentre IPs. Degrades gracefully."""
    results = []
    session = new_session()

    # The API path includes a build ID that changes with each site deploy,
    # so pull it from the homepage first.
    build_id = None
    try:
        html = http_text(session, "https://www.coles.com.au/",
                         headers={"Accept": "text/html"})
        m = re.search(r'"buildId"\s*:\s*"([^"]+)"', html)
        if m:
            build_id = m.group(1)
    except Exception as exc:
        print(f"  [Coles] homepage failed (bot protection?): {exc}")
    if not build_id:
        print("  [Coles] couldn't find build ID — skipping Coles this run")
        return results

    from urllib.parse import quote
    for term in terms:
        url = (f"https://www.coles.com.au/_next/data/{build_id}"
               f"/en/search.json?q={quote(term)}")
        try:
            data = http_json(url, session=session,
                             headers={"Referer": "https://www.coles.com.au/"})
        except Exception as exc:
            print(f"  [Coles] '{term}' failed: {exc}")
            time.sleep(1.5)
            continue
        page = data.get("pageProps", {}) or {}
        found = (page.get("searchResults", {}) or {}).get("results", []) or []
        for prod in found:
            if prod.get("_type") not in (None, "PRODUCT"):
                continue
            pricing = prod.get("pricing") or {}
            price = pricing.get("now")
            name = prod.get("name") or ""
            if not price or not name:
                continue
            size = prod.get("size") or ""
            full_name = f"{name} {size}".strip()
            ppk = None
            comparable = pricing.get("comparable") or ""
            m = re.search(r"\$([\d.]+)\s*per\s*1?\s*kg", comparable, re.I)
            if m:
                ppk = float(m.group(1))
            pid = prod.get("id", "")
            results.append({
                "retailer": "Coles", "name": full_name,
                "price": float(price),
                "price_per_kg": ppk or price_per_kg(full_name, float(price)),
                "url": f"https://www.coles.com.au/product/{pid}" if pid else "",
            })
        time.sleep(1.5)
    print(f"  [Coles] {len(results)} items")
    return results


DEMO = [
    ("Panetta Mercato", "Wombok Whole Each", 5.00),
    ("Harris Farm", "Wombok Half Each", 3.50),
    ("Woolworths", "Wombok Whole Each", 6.90),
    ("Harris Farm", "Brussels Sprouts 500g", 3.50),
    ("Woolworths", "Brussels Sprouts per kg", 9.00),
    ("Panetta Mercato", "Brussels Sprouts per kg", 7.99),
    ("Harris Farm", "Pink Lady Apples per kg", 5.90),
    ("Woolworths", "Pink Lady Apples 1kg Bag", 6.50),
    ("Panetta Mercato", "Granny Smith Apples per kg", 4.99),
    ("Harris Farm", "Navel Oranges 3kg Bag", 6.99),
    ("Woolworths", "Navel Orange per kg", 3.80),
    ("Panetta Mercato", "Imperial Mandarins per kg", 3.99),
    ("Harris Farm", "Tomato Gourmet per kg", 4.20),
    ("Woolworths", "Tomato Truss per kg", 5.90),
    ("Harris Farm", "Cauliflower Whole Each", 3.99),
    ("Woolworths", "Cauliflower Whole Each", 4.90),
    ("Harris Farm", "Broccoli per kg", 4.90),
    ("Woolworths", "Broccoli per kg", 5.50),
    ("Harris Farm", "Kent Pumpkin Cut per kg", 1.99),
    ("Woolworths", "Pumpkin Kent Whole per kg", 2.50),
    ("Harris Farm", "Sweet Potato Gold per kg", 2.99),
    ("Woolworths", "Potato Brushed 2kg Bag", 4.50),
    ("Panetta Mercato", "Silverbeet Bunch", 3.49),
    ("Harris Farm", "Blackberries 125g Punnet", 4.99),
    ("Woolworths", "Blackberries 125g", 4.00),
    ("Harris Farm", "Zucchini per kg", 4.50),
    ("Panetta Mercato", "Carrots 1kg Bag", 1.99),
    ("Woolworths", "Carrots 1kg", 2.20),
    ("Harris Farm", "Celery Whole Each", 3.50),
    ("Panetta Mercato", "Papaya Red per kg", 5.99),
    ("Woolworths", "Kiwifruit Green each", 0.90),
    ("Harris Farm", "Packham Pears per kg", 3.99),
    ("Woolworths", "Lemons per kg", 4.90),
    ("Panetta Mercato", "Fennel Bulb Each", 2.99),
    ("Harris Farm", "Leeks Bunch of 2", 4.50),
    ("Woolworths", "Cabbage Savoy Half", 3.00),
    ("Coles", "Wombok Cabbage Whole Each", 6.50),
    ("Coles", "Brussels Sprouts 400g", 4.00),
    ("Coles", "Pink Lady Apples per kg", 5.50),
    ("Coles", "Navel Oranges per kg", 3.50),
    ("Coles", "Imperial Mandarins per kg", 4.50),
    ("Coles", "Tomatoes Gourmet per kg", 4.90),
    ("Coles", "Cauliflower Whole Each", 4.50),
    ("Coles", "Broccoli per kg", 4.90),
    ("Coles", "Pumpkin Kent Cut per kg", 2.20),
    ("Coles", "Sweet Potato Gold per kg", 3.20),
    ("Coles", "Carrots 1kg Bag", 2.10),
    ("Coles", "Zucchini per kg", 5.50),
    ("Coles", "Celery Half Each", 2.50),
    ("Coles", "Kiwifruit Green 4 Pack", 3.50),
    ("Coles", "Packham Pears per kg", 4.20),
    ("Coles", "Lemons Each", 1.20),
    ("Coles", "Silverbeet Bunch Each", 3.90),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()

    if args.demo:
        products = [{"retailer": r, "name": n, "price": p,
                     "price_per_kg": price_per_kg(n, p), "url": ""}
                    for r, n, p in DEMO]
        note = "sample data"
    else:
        mode = "Chrome TLS impersonation (curl_cffi)" if HAVE_CFFI else \
               "plain urllib — install curl_cffi for better supermarket odds"
        print(f"Network mode: {mode}")
        products = []
        products += fetch_shopify("www.harrisfarm.com.au", "Harris Farm")
        products += fetch_woocommerce("panettamercato.com.au", "Panetta Mercato")
        products += fetch_woolworths(SEARCH_TERMS)
        products += fetch_coles(SEARCH_TERMS)
        note = "live"

    before = len(products)
    products = [p for p in products if not FRESH_EXCLUDE.search(p["name"])]
    print(f"Fresh-only filter: {before} -> {len(products)} products")

    if not products:
        raise SystemExit("No products fetched — leaving previous deals.json alone")

    sydney = timezone(timedelta(hours=10))
    payload = {
        "fetched_at": datetime.now(sydney).strftime("%-I:%M %p, %A %-d %B"),
        "note": note,
        "retailers": sorted({p["retailer"] for p in products}),
        "products": products,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=1))
    print(f"\nWrote {len(products)} products ({note}) -> {OUT}")


if __name__ == "__main__":
    main()
