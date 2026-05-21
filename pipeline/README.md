# Pipeline

Scrapes professor data from RateMyProfessors (GraphQL) and UW Course Evaluations (Playwright), then cleans and joins both sources into the `professors` table in PostgreSQL.

```
rmp_scraper.py   →  rmp_professors_raw, rmp_ratings_raw
cec_scraper.py   →  cec_evaluations_raw
cleaner.py       →  professors (joined, normalized)
main.py          →  orchestrates all three
```

## Environment variables

```
DATABASE_URL     PostgreSQL connection string
RMP_AUTH         Authorization header value for RMP GraphQL API
NTFY_URL         (optional) ntfy server URL for push notifications
NTFY_TOPIC       (optional) ntfy topic
NTFY_TOKEN       (optional) ntfy auth token
```

## Setup

```bash
cd pipeline
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Running

```bash
python main.py              # full pipeline: RMP → CEC → cleaner
python main.py --rmp        # RMP scraper only
python main.py --cec        # CEC scraper only
python main.py --clean      # cleaner only
python main.py --force      # re-scrape all RMP data regardless of changes
```

## Notes

- The CEC scraper opens a **visible browser window** for manual UW NetID login, then switches to headless once authenticated.
- `CONCURRENT_PAGES = 10` in `cec_scraper.py` — increase cautiously, UW may rate-limit aggressive scrapers.
- The cleaner uses fuzzy name matching to join RMP and CEC data. Run it after either scraper updates data.
