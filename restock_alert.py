#!/usr/bin/env python3
"""PS5 Pro restock alert: watches PlayStation Direct and blasts SMS via Twilio when stock appears.

Usage:
    python restock_alert.py               # keep watching
    python restock_alert.py --once        # check stock once and print the result
    python restock_alert.py --test-sms    # send one test SMS to verify the Twilio setup
    python restock_alert.py --test-alert  # simulate a restock and run the full alert burst
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import time
from datetime import datetime

import requests
from dotenv import load_dotenv
from twilio.rest import Client

API_URL = (
    "https://api.direct.playstation.com/commercewebservices/{site}"
    "/users/anonymous/products/productList"
)
HARDWARE_PAGE = "https://direct.playstation.com/en-us/hardware/ps5"
PRODUCT_PAGES = {
    "1000050805": "https://direct.playstation.com/en-us/buy-consoles/playstation5-pro-console",
    "1000057525": "https://direct.playstation.com/en-us/buy-consoles/certified-refurbished-playstation5-pro-console",
}
DEFAULT_SKUS = "1000050805,1000057525"  # PS5 Pro (new), Certified Refurbished PS5 Pro
IN_STOCK_STATUSES = {"inStock", "lowStock"}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://direct.playstation.com",
    "Referer": "https://direct.playstation.com/",
}

log = logging.getLogger("restock")


# ---------------------------------------------------------------- config

def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        sys.exit(f"[config error] {name} is missing from .env")
    return value or ""


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"[config error] {name} must be an integer, got {raw!r}")


class Config:
    def __init__(self) -> None:
        self.sid = env("TWILIO_ACCOUNT_SID", required=True)
        self.token = env("TWILIO_AUTH_TOKEN", required=True)
        self.from_number = env("TWILIO_FROM_NUMBER", required=True)
        self.to_numbers = [n.strip() for n in env("ALERT_TO_NUMBERS", required=True).split(",") if n.strip()]

        self.skus = [s.strip() for s in env("PS_SKUS", DEFAULT_SKUS).split(",") if s.strip()]
        self.site = env("PS_SITE", "ps-direct-us")

        self.check_interval = max(env_int("CHECK_INTERVAL_SECONDS", 60), 15)
        self.sms_count = max(env_int("SMS_COUNT", 10), 1)
        self.sms_gap = max(env_int("SMS_INTERVAL_SECONDS", 3), 0)
        self.realert_minutes = env_int("REALERT_MINUTES", 30)


# ---------------------------------------------------------------- stock

def fetch_stock(cfg: Config, session: requests.Session) -> list[dict]:
    """Return {code, name, status, in_stock, price} for each SKU."""
    resp = session.get(
        API_URL.format(site=cfg.site),
        params={"fields": "BASIC", "productCodes": ",".join(cfg.skus)},
        headers=HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    results = []
    for p in resp.json().get("products", []):
        status = (p.get("stock") or {}).get("stockLevelStatus", "unknown")
        price = (p.get("price") or {}).get("value")
        results.append({
            "code": p.get("code"),
            "name": (p.get("name") or p.get("code") or "").strip(),
            "status": status,
            "in_stock": status in IN_STOCK_STATUSES,
            "price": price,
        })
    return results


# ---------------------------------------------------------------- sms

def send_sms(cfg: Config, client: Client, body: str) -> None:
    for to in cfg.to_numbers:
        try:
            msg = client.messages.create(body=body, from_=cfg.from_number, to=to)
            log.info("SMS sent -> %s (sid=%s)", to, msg.sid)
        except Exception as e:  # noqa: BLE001 - one failed message must not stop the burst
            log.error("SMS failed -> %s: %s", to, e)


def send_alert_burst(cfg: Config, client: Client, product: dict) -> None:
    price = f" ${product['price']:.0f}" if product.get("price") else ""
    link = PRODUCT_PAGES.get(product["code"], HARDWARE_PAGE)
    log.warning("🚨 IN STOCK: %s%s - sending %d SMS", product["name"], price, cfg.sms_count)
    for i in range(1, cfg.sms_count + 1):
        body = (
            f"🚨 [{i}/{cfg.sms_count}] {product['name']} is IN STOCK!{price}\n"
            f"Buy now: {link}"
        )
        send_sms(cfg, client, body)
        if i < cfg.sms_count:
            time.sleep(cfg.sms_gap)


# ---------------------------------------------------------------- main loop

def run(cfg: Config, client: Client) -> None:
    session = requests.Session()
    last_alert: dict[str, float] = {}   # sku -> time of last alert burst
    failures = 0

    log.info(
        "Watching SKUs %s every %ds; will send %d SMS to %s on restock",
        ",".join(cfg.skus), cfg.check_interval, cfg.sms_count, ",".join(cfg.to_numbers),
    )

    while True:
        try:
            products = fetch_stock(cfg, session)
            failures = 0
            if not products:
                log.warning("API returned no products - check PS_SKUS")
            for p in products:
                log.info("%s -> %s", p["name"], p["status"])
                if p["in_stock"]:
                    prev = last_alert.get(p["code"])
                    cooldown_over = (
                        prev is not None
                        and cfg.realert_minutes > 0
                        and time.time() - prev >= cfg.realert_minutes * 60
                    )
                    if prev is None or cooldown_over:
                        send_alert_burst(cfg, client, p)
                        last_alert[p["code"]] = time.time()
                else:
                    # Out of stock again: reset so the next restock alerts again
                    last_alert.pop(p["code"], None)
        except requests.RequestException as e:
            failures += 1
            log.error("Stock check failed (%d in a row): %s", failures, e)
        except ValueError as e:
            failures += 1
            log.error("Response was not JSON - possibly blocked or in a queue page (%d in a row): %s", failures, e)

        # Exponential backoff on failure (max 10 min); small random jitter otherwise
        if failures:
            delay = min(cfg.check_interval * 2 ** min(failures, 4), 600)
        else:
            delay = cfg.check_interval + random.uniform(0, cfg.check_interval * 0.2)
        time.sleep(delay)


def main() -> None:
    parser = argparse.ArgumentParser(description="PS5 Pro restock alert with Twilio SMS")
    parser.add_argument("--once", action="store_true", help="check stock once and exit")
    parser.add_argument("--test-sms", action="store_true", help="send one test SMS")
    parser.add_argument("--test-alert", action="store_true", help="simulate a restock and send the full alert burst")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    load_dotenv()

    if args.once:
        # Checking stock does not need Twilio credentials
        os.environ.setdefault("TWILIO_ACCOUNT_SID", "-")
        os.environ.setdefault("TWILIO_AUTH_TOKEN", "-")
        os.environ.setdefault("TWILIO_FROM_NUMBER", "-")
        os.environ.setdefault("ALERT_TO_NUMBERS", "-")
        cfg = Config()
        for p in fetch_stock(cfg, requests.Session()):
            mark = "✅ IN STOCK " if p["in_stock"] else "❌ SOLD OUT"
            print(f"{mark}  {p['name']} ({p['code']})  status={p['status']}  price={p['price']}")
        return

    cfg = Config()
    client = Client(cfg.sid, cfg.token)

    if args.test_sms:
        send_sms(cfg, client, f"✅ PS5 Restock Alert test message ({datetime.now():%H:%M:%S}) - setup works!")
        return

    if args.test_alert:
        send_alert_burst(cfg, client, {"code": "TEST", "name": "PlayStation®5 Pro Console (TEST)", "price": None})
        return

    try:
        run(cfg, client)
    except KeyboardInterrupt:
        log.info("Stopped")


if __name__ == "__main__":
    main()
