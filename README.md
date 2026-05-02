# SF 3BR/3BA Apartment Scraper → Google Sheets

Scrapes daily SF apartment listings from **Craigslist**, **Zumper**, and **Apartments.com**, deduplicates by URL, and appends new results to a Google Sheet.

The sheet columns are:

| Date Scraped | Post Date | Title | Price | Sqft | Neighborhood | Source | URL |

---

## Setup

### 1. Google Cloud service account

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → **APIs & Services** → **Library**
2. Enable **Google Sheets API** and **Google Drive API**
3. **IAM & Admin** → **Service Accounts** → Create a service account
4. Create a JSON key and download it

### 2. Google Sheet

1. Create a new Google Sheet
2. Share it with the service account email (`...@....iam.gserviceaccount.com`) as **Editor**
3. Copy the Sheet ID from the URL: `https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit`

### 3. Local usage

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in .env with your credentials and sheet ID
python scraper.py
```

Optional price filter via `.env`:
```
MIN_PRICE=3000
MAX_PRICE=7000
```

### 4. Automated daily runs (GitHub Actions)

Add two **repository secrets** (`Settings → Secrets and variables → Actions`):

| Secret | Value |
|--------|-------|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | The full contents of your service account JSON file (single line or multi-line) |
| `GOOGLE_SHEET_ID` | Your Google Sheet ID |

Optionally set `MIN_PRICE` / `MAX_PRICE` as **repository variables** (not secrets).

The workflow runs automatically at **7 AM PST on weekdays**. You can also trigger it manually from the **Actions** tab with optional price overrides.

---

## Sources

| Source | Method |
|--------|--------|
| Craigslist | HTML scraping (requests + BeautifulSoup) |
| Zumper | JSON API |
| Apartments.com | HTML scraping with `__NEXT_DATA__` JSON fallback |

> **Note:** Craigslist and Apartments.com may occasionally block scrapers or change their page structure. If results drop to zero for a source, inspect the HTML and update the selectors in `scraper.py`.
