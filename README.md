# PS5 Restock Alert

Watches PlayStation Direct (US) for PS5 Pro stock — both the new console and the Certified Refurbished one — and sends a burst of SMS messages through Twilio (10 by default) the moment it's available.

## Setup

1. Fill in your Twilio credentials and phone number in `.env` (template: `.env.example`).
2. Install dependencies:
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   ```

## Usage

```bash
.venv/bin/python restock_alert.py --once        # check stock once
.venv/bin/python restock_alert.py --test-sms    # send one test SMS
.venv/bin/python restock_alert.py --test-alert  # simulate a restock (sends the full burst)
.venv/bin/python restock_alert.py               # start watching (Ctrl+C to stop)
```

## How it works

- Polls PlayStation Direct's stock API; `stockLevelStatus` of `inStock` or `lowStock` counts as available.
- Sends one alert burst when a SKU goes from sold out to in stock, then repeats every `REALERT_MINUTES` while it stays in stock.
- Resets once a SKU sells out again, so the next restock triggers a fresh burst.
- Backs off exponentially on errors (up to 10 minutes) to avoid getting blocked.

## Finding a SKU

Open the product page on direct.playstation.com, view the page source, and search for `"sku"`. Add the number to `PS_SKUS` in `.env`.

## Notes

- Twilio trial accounts can only text verified numbers, and messages get a trial prefix.
- The script only works while it's running. To keep your Mac awake while it runs:
  ```bash
  caffeinate -i .venv/bin/python restock_alert.py
  ```
