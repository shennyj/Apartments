"""
SF 3BR/3BA apartment scraper — Craigslist, Zumper, Apartments.com
Deduplicates by URL and appends new listings to a Google Sheet.
"""

import json
import os
import time
from datetime import datetime

import gspread
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
SHEET_COLUMNS = ["Date Scraped", "Post Date", "Title", "Price", "Sqft", "Neighborhood", "Source", "URL"]
TODAY = datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Craigslist
# ---------------------------------------------------------------------------

def scrape_craigslist(min_price=None, max_price=None):
    base_url = "https://sfbay.craigslist.org/search/sfc/apa"
    params = {"bedrooms": 3, "bathrooms": 3}
    if min_price:
        params["min_price"] = min_price
    if max_price:
        params["max_price"] = max_price

    listings = []
    for offset in range(0, 481, 120):
        params["s"] = offset
        try:
            resp = requests.get(base_url, params=params, headers=HEADERS, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  [Craigslist] fetch error at offset {offset}: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        results = soup.select("li.cl-static-search-result") or soup.select("li.result-row")
        if not results:
            break

        for item in results:
            try:
                title_el = item.select_one("div.title") or item.select_one("a.result-title")
                price_el = item.select_one("div.price") or item.select_one("span.result-price")
                link_el = item.select_one("a[href]")
                if not title_el or not link_el:
                    continue
                url = link_el.get("href", "")
                if url and not url.startswith("http"):
                    url = "https://sfbay.craigslist.org" + url
                listings.append({
                    "source": "Craigslist",
                    "title": title_el.text.strip(),
                    "price": price_el.text.strip() if price_el else "",
                    "url": url,
                    "date_scraped": TODAY,
                })
            except Exception:
                continue

        print(f"  [Craigslist] offset {offset}: {len(results)} results")
        time.sleep(2)

    return listings


def enrich_craigslist(listing):
    """Fetch individual Craigslist page for post_date, neighborhood, sqft."""
    try:
        resp = requests.get(listing["url"], headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        post_date = ""
        date_el = soup.select_one("time.date") or soup.select_one("time[datetime]")
        if date_el:
            raw = date_el.get("datetime", date_el.text)
            post_date = raw[:10] if raw else ""

        neighborhood = ""
        for sel in ["span.postingtitletext small", "div.mapaddress", "span.maptag"]:
            el = soup.select_one(sel)
            if el:
                neighborhood = el.text.strip().strip("()")
                break

        sqft = ""
        for attr in soup.select("span.shared-line-bubble, span.housing"):
            text = attr.text.strip()
            if "ft2" in text:
                sqft = text.replace("ft2", "").strip()
                break

        listing.update({"post_date": post_date, "neighborhood": neighborhood, "sqft": sqft})
    except Exception as e:
        print(f"    [Craigslist] detail fetch failed: {e}")
    return listing


# ---------------------------------------------------------------------------
# Zumper
# ---------------------------------------------------------------------------

def scrape_zumper(min_price=None, max_price=None):
    """
    Zumper exposes a JSON API used by its own frontend. We query it directly.
    """
    api_url = "https://www.zumper.com/api/t/1/listings"
    params = {
        "beds": "3",
        "baths": "3",
        "city_ids": "1738",   # San Francisco, CA
        "order_by": "posted_time",
        "page": 1,
    }
    if min_price:
        params["price_min"] = min_price
    if max_price:
        params["price_max"] = max_price

    listings = []
    headers = {**HEADERS, "Accept": "application/json", "Referer": "https://www.zumper.com/"}

    for page in range(1, 6):
        params["page"] = page
        try:
            resp = requests.get(api_url, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  [Zumper] error on page {page}: {e}")
            break

        results = data if isinstance(data, list) else data.get("listings", data.get("data", []))
        if not results:
            break

        for item in results:
            try:
                price = item.get("price") or item.get("price_max") or ""
                price_str = f"${price:,}" if isinstance(price, (int, float)) and price else str(price)
                url = item.get("url") or item.get("link") or ""
                if url and not url.startswith("http"):
                    url = "https://www.zumper.com" + url
                sqft = str(item.get("sqft") or item.get("area") or "")
                neighborhood = (
                    item.get("neighborhood")
                    or item.get("area")
                    or item.get("city")
                    or ""
                )
                title = item.get("title") or item.get("name") or item.get("address") or ""
                listings.append({
                    "source": "Zumper",
                    "title": str(title).strip(),
                    "price": price_str,
                    "sqft": sqft,
                    "neighborhood": str(neighborhood).strip(),
                    "url": url,
                    "date_scraped": TODAY,
                    "post_date": "",
                })
            except Exception:
                continue

        print(f"  [Zumper] page {page}: {len(results)} results")
        if len(results) < 20:
            break
        time.sleep(1.5)

    return listings


# ---------------------------------------------------------------------------
# Apartments.com
# ---------------------------------------------------------------------------

def scrape_apartments_com(min_price=None, max_price=None):
    """
    Apartments.com embeds listing JSON inside a <script> tag (application/ld+json
    or a __NEXT_DATA__ blob). We try to parse whichever is present.
    """
    price_slug = ""
    if min_price and max_price:
        price_slug = f"{min_price}-to-{max_price}/"
    elif max_price:
        price_slug = f"under-{max_price}/"

    url = f"https://www.apartments.com/san-francisco-ca/3-bedrooms/{price_slug}"
    listings = []

    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Try __NEXT_DATA__ JSON blob first
        next_data_el = soup.select_one("script#__NEXT_DATA__")
        if next_data_el:
            payload = json.loads(next_data_el.string or "{}")
            props = payload.get("props", {}).get("pageProps", {})
            raw_listings = (
                props.get("listings")
                or props.get("searchResults", {}).get("listings", [])
            )
            for item in (raw_listings or []):
                try:
                    price = item.get("rentRange") or item.get("price") or ""
                    listings.append({
                        "source": "Apartments.com",
                        "title": item.get("name") or item.get("title") or "",
                        "price": str(price),
                        "sqft": str(item.get("sqft") or ""),
                        "neighborhood": item.get("neighborhood") or item.get("city") or "San Francisco",
                        "url": item.get("url") or item.get("listingUrl") or "",
                        "date_scraped": TODAY,
                        "post_date": "",
                    })
                except Exception:
                    continue
            print(f"  [Apartments.com] __NEXT_DATA__: {len(listings)} results")
            return listings

        # Fall back to HTML scraping
        for card in soup.select("article.placard, li.mortar-wrapper, div[data-listingid]"):
            try:
                title_el = card.select_one("span.js-placardTitle, .property-title, .js-propertyName")
                price_el = card.select_one(".price-range, .js-priceSuffix, span.altRentDisplay")
                link_el = card.select_one("a[href]")
                if not title_el or not link_el:
                    continue
                href = link_el.get("href", "")
                if href and not href.startswith("http"):
                    href = "https://www.apartments.com" + href
                listings.append({
                    "source": "Apartments.com",
                    "title": title_el.text.strip(),
                    "price": price_el.text.strip() if price_el else "",
                    "sqft": "",
                    "neighborhood": "San Francisco",
                    "url": href,
                    "date_scraped": TODAY,
                    "post_date": "",
                })
            except Exception:
                continue
        print(f"  [Apartments.com] HTML: {len(listings)} results")

    except Exception as e:
        print(f"  [Apartments.com] error: {e}")

    return listings


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------

def get_worksheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    creds_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")

    if creds_json:
        creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=scopes)
    elif creds_file:
        creds = Credentials.from_service_account_file(creds_file, scopes=scopes)
    else:
        raise EnvironmentError(
            "Set GOOGLE_SERVICE_ACCOUNT_JSON (JSON string) or "
            "GOOGLE_SERVICE_ACCOUNT_FILE (path to credentials .json file)"
        )

    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise EnvironmentError("Set GOOGLE_SHEET_ID to your Google Sheet's ID")

    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)

    try:
        ws = sh.worksheet("Listings")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet("Listings", rows=5000, cols=len(SHEET_COLUMNS))
        ws.append_row(SHEET_COLUMNS)
        ws.freeze(rows=1)
        ws.format("A1:H1", {"textFormat": {"bold": True}})

    return ws


def send_to_sheets(listings):
    ws = get_worksheet()

    try:
        all_values = ws.get_all_values()
        # URL is the last column (index 7)
        existing_urls = {row[7] for row in all_values[1:] if len(row) > 7 and row[7]}
    except Exception:
        existing_urls = set()

    new_rows = [
        [
            lst.get("date_scraped", ""),
            lst.get("post_date", ""),
            lst.get("title", ""),
            lst.get("price", ""),
            lst.get("sqft", ""),
            lst.get("neighborhood", ""),
            lst.get("source", ""),
            lst.get("url", ""),
        ]
        for lst in listings
        if lst.get("url") and lst["url"] not in existing_urls
    ]

    if new_rows:
        ws.append_rows(new_rows, value_input_option="USER_ENTERED")

    return len(new_rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    min_price = os.environ.get("MIN_PRICE")
    max_price = os.environ.get("MAX_PRICE")

    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Scraping SF 3BR/3BA apartments...")
    if min_price or max_price:
        print(f"  Price filter: ${min_price or '0'} – ${max_price or '∞'}")

    all_listings = []

    # --- Craigslist ---
    print("\nCraigslist:")
    cl_listings = scrape_craigslist(min_price=min_price, max_price=max_price)
    print(f"  Fetching details for {len(cl_listings)} listings...")
    for i, lst in enumerate(cl_listings, 1):
        enrich_craigslist(lst)
        print(f"    [{i}/{len(cl_listings)}] {lst.get('price', '?'):>8}  {lst['title'][:50]}")
        time.sleep(1)
    all_listings.extend(cl_listings)

    # --- Zumper ---
    print("\nZumper:")
    zp_listings = scrape_zumper(min_price=min_price, max_price=max_price)
    print(f"  {len(zp_listings)} listings found")
    all_listings.extend(zp_listings)

    # --- Apartments.com ---
    print("\nApartments.com:")
    apts_listings = scrape_apartments_com(min_price=min_price, max_price=max_price)
    print(f"  {len(apts_listings)} listings found")
    all_listings.extend(apts_listings)

    print(f"\nTotal: {len(all_listings)} listings across all sources")
    print("Sending to Google Sheets...")
    added = send_to_sheets(all_listings)
    print(f"Done — {added} new listing(s) added.")


if __name__ == "__main__":
    main()
