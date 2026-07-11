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


def http_json(url, payload=None, opener=None, headers=None, timeout=25):
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
    op = opener or urllib.request.build_opener()
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
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        req = urllib.request.Request("https://www.woolworths.com.au/",
                                     headers={"User-Agent": UA})
        opener.open(req, timeout=20).read(1024)
    except Exception:
        pass
    for term in terms:
        try:
            data = http_json(
                "https://www.woolworths.com.au/apis/ui/Search/products",
                payload={"SearchTerm": term, "PageSize": 24, "PageNumber": 1,
                         "SortType": "PriceAsc", "Filters": []},
                opener=opener,
                headers={"Origin": "https://www.woolworths.com.au",
                         "Referer": "https://www.woolworths.com.au/shop/browse/fruit-veg"})
        except Exception as exc:
            print(f"  [Woolworths] '{term}' failed: {exc}")
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
        products = []
        products += fetch_shopify("www.harrisfarm.com.au", "Harris Farm")
        products += fetch_shopify("panettamercato.com.au", "Panetta Mercato")
        products += fetch_woolworths(SEARCH_TERMS)
        note = "live"

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
