"""
SF 3BR/3BA apartment scraper — Craigslist (requests), Zumper/Apartments.com/Zillow (Playwright)
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
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync

load_dotenv()

REQUESTS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
SHEET_COLUMNS = ["Date Scraped", "Post Date", "Title", "Price", "Sqft", "Neighborhood", "Source", "URL"]
TODAY = datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Craigslist  (plain HTTP — works without a browser)
# ---------------------------------------------------------------------------

def scrape_craigslist(min_price=None, max_price=None):
    base_url = "https://sfbay.craigslist.org/search/sfc/apa"
    params = {"bedrooms": 3, "bathrooms": 3}
    if min_price:
        params["min_price"] = min_price
    if max_price:
        params["max_price"] = max_price

    listings = []
    seen_urls = set()

    for offset in range(0, 481, 120):
        params["s"] = offset
        try:
            resp = requests.get(base_url, params=params, headers=REQUESTS_HEADERS, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  [Craigslist] fetch error at offset {offset}: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        results = soup.select("li.cl-static-search-result") or soup.select("li.result-row")
        if not results:
            break

        new_on_page = 0
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
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_on_page += 1
                listings.append({
                    "source": "Craigslist",
                    "title": title_el.text.strip(),
                    "price": price_el.text.strip() if price_el else "",
                    "url": url,
                    "date_scraped": TODAY,
                })
            except Exception:
                continue

        print(f"  [Craigslist] offset {offset}: {new_on_page} new / {len(results)} total")
        if new_on_page == 0:
            break
        time.sleep(2)

    return listings


def enrich_craigslist(listing):
    """Fetch individual Craigslist page for post_date, neighborhood, sqft."""
    try:
        resp = requests.get(listing["url"], headers=REQUESTS_HEADERS, timeout=15)
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
# Playwright helpers
# ---------------------------------------------------------------------------

def _get_next_data(page, url, label, wait_for=None):
    """Navigate to url, optionally wait for a selector, return parsed __NEXT_DATA__ or None."""
    try:
        page.goto(url, wait_until="networkidle", timeout=45_000)
        if wait_for:
            try:
                page.wait_for_selector(wait_for, timeout=10_000)
            except Exception:
                pass
        print(f"  [{label}] page title: {page.title()!r}")
        raw = page.evaluate(
            "() => { const e = document.getElementById('__NEXT_DATA__'); return e ? e.textContent : null; }"
        )
        if raw:
            return json.loads(raw)
        print(f"  [{label}] no __NEXT_DATA__ found")
    except Exception as e:
        print(f"  [{label}] Playwright error: {e}")
    return None


def _page_html(page, url, label, wait_for=None):
    """Navigate and return BeautifulSoup of the fully rendered page."""
    try:
        page.goto(url, wait_until="networkidle", timeout=45_000)
        if wait_for:
            try:
                page.wait_for_selector(wait_for, timeout=10_000)
            except Exception:
                pass
        return BeautifulSoup(page.content(), "html.parser")
    except Exception as e:
        print(f"  [{label}] Playwright error: {e}")
    return None


# ---------------------------------------------------------------------------
# Zillow  (Playwright)
# ---------------------------------------------------------------------------

def _scrape_zillow(page, min_price=None, max_price=None):
    url = "https://www.zillow.com/san-francisco-ca/rentals/3-_beds/3.0-_baths/"
    if min_price or max_price:
        parts = []
        if min_price:
            parts.append(f"price_min={min_price}")
        if max_price:
            parts.append(f"price_max={max_price}")
        url += "?" + "&".join(parts)

    data = _get_next_data(page, url, "Zillow")
    listings = []
    if not data:
        return listings

    list_results = (
        data.get("props", {})
        .get("pageProps", {})
        .get("searchPageState", {})
        .get("cat1", {})
        .get("searchResults", {})
        .get("listResults", [])
    )

    for item in list_results:
        try:
            detail_url = item.get("detailUrl") or item.get("url") or ""
            if detail_url and not detail_url.startswith("http"):
                detail_url = "https://www.zillow.com" + detail_url
            home_info = item.get("hdpData", {}).get("homeInfo", {})
            neighborhood = (
                home_info.get("neighborhood")
                or home_info.get("city")
                or "San Francisco"
            )
            listings.append({
                "source": "Zillow",
                "title": item.get("address") or item.get("streetAddress") or "",
                "price": str(item.get("price") or item.get("unformattedPrice") or ""),
                "sqft": str(item.get("area") or item.get("sqft") or ""),
                "neighborhood": neighborhood,
                "url": detail_url,
                "date_scraped": TODAY,
                "post_date": "",
            })
        except Exception:
            continue

    print(f"  [Zillow] {len(listings)} results")
    return listings


# ---------------------------------------------------------------------------
# Apartments.com  (Playwright)
# ---------------------------------------------------------------------------

def _scrape_apartments_com(page, min_price=None, max_price=None):
    price_slug = ""
    if min_price and max_price:
        price_slug = f"{min_price}-to-{max_price}/"
    elif max_price:
        price_slug = f"under-{max_price}/"

    url = f"https://www.apartments.com/san-francisco-ca/3-bedrooms/{price_slug}"
    listings = []

    # Try __NEXT_DATA__ first
    data = _get_next_data(page, url, "Apartments.com", wait_for="article.placard")
    if data:
        props = data.get("props", {}).get("pageProps", {})
        raw = props.get("listings") or props.get("searchResults", {}).get("listings", [])
        for item in (raw or []):
            try:
                price = item.get("rentRange") or item.get("price") or ""
                item_url = item.get("url") or item.get("listingUrl") or ""
                if item_url:
                    listings.append({
                        "source": "Apartments.com",
                        "title": item.get("name") or item.get("title") or "",
                        "price": str(price),
                        "sqft": str(item.get("sqft") or ""),
                        "neighborhood": item.get("neighborhood") or "San Francisco",
                        "url": item_url,
                        "date_scraped": TODAY,
                        "post_date": "",
                    })
            except Exception:
                continue
        if listings:
            print(f"  [Apartments.com] {len(listings)} results (JSON)")
            return listings

    # Fall back to DOM
    soup = _page_html(page, url, "Apartments.com", wait_for="article.placard")
    if not soup:
        return listings

    for card in soup.select("article.placard, li.mortar-wrapper"):
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

    print(f"  [Apartments.com] {len(listings)} results (DOM)")
    return listings


# ---------------------------------------------------------------------------
# Zumper  (Playwright)
# ---------------------------------------------------------------------------

def _scrape_zumper(page, min_price=None, max_price=None):
    params = "?beds=3&baths=3"
    if min_price:
        params += f"&price_min={min_price}"
    if max_price:
        params += f"&price_max={max_price}"
    url = f"https://www.zumper.com/apartments-for-rent/san-francisco-ca{params}"

    listings = []
    data = _get_next_data(page, url, "Zumper", wait_for="[data-tid='listing-card']")
    if data:
        props = data.get("props", {}).get("pageProps", {})
        raw = (
            props.get("listings")
            or props.get("initialState", {}).get("listings", {}).get("listings", [])
            or props.get("searchResults", [])
        )
        for item in (raw or []):
            try:
                price = item.get("price") or item.get("price_max") or ""
                price_str = f"${price:,}" if isinstance(price, (int, float)) and price else str(price)
                detail_url = item.get("url") or item.get("link") or ""
                if detail_url and not detail_url.startswith("http"):
                    detail_url = "https://www.zumper.com" + detail_url
                listings.append({
                    "source": "Zumper",
                    "title": str(item.get("address") or item.get("title") or "").strip(),
                    "price": price_str,
                    "sqft": str(item.get("sqft") or item.get("area") or ""),
                    "neighborhood": str(item.get("neighborhood") or item.get("city") or "San Francisco"),
                    "url": detail_url,
                    "date_scraped": TODAY,
                    "post_date": "",
                })
            except Exception:
                continue

    print(f"  [Zumper] {len(listings)} results")
    return listings


# ---------------------------------------------------------------------------
# Playwright runner  (single browser for all three sites)
# ---------------------------------------------------------------------------

def scrape_with_playwright(min_price=None, max_price=None):
    all_listings = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=BROWSER_UA,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        page = ctx.new_page()
        stealth_sync(page)  # Patch headless browser detection signals

        print("\nZillow:")
        all_listings.extend(_scrape_zillow(page, min_price, max_price))

        print("\nApartments.com:")
        all_listings.extend(_scrape_apartments_com(page, min_price, max_price))

        print("\nZumper:")
        all_listings.extend(_scrape_zumper(page, min_price, max_price))

        browser.close()
    return all_listings


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

    # --- Craigslist (plain HTTP) ---
    print("\nCraigslist:")
    cl_listings = scrape_craigslist(min_price=min_price, max_price=max_price)
    print(f"  Fetching details for {len(cl_listings)} listings...")
    for i, lst in enumerate(cl_listings, 1):
        enrich_craigslist(lst)
        print(f"    [{i}/{len(cl_listings)}] {lst.get('price', '?'):>8}  {lst['title'][:50]}")
        time.sleep(1)
    all_listings.extend(cl_listings)

    # --- Zillow, Apartments.com, Zumper (Playwright) ---
    all_listings.extend(scrape_with_playwright(min_price=min_price, max_price=max_price))

    print(f"\nTotal: {len(all_listings)} listings across all sources")
    print("Sending to Google Sheets...")
    added = send_to_sheets(all_listings)
    print(f"Done — {added} new listing(s) added.")


if __name__ == "__main__":
    main()
