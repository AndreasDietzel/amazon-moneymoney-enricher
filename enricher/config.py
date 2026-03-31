"""
Configuration for Amazon MoneyMoney Enricher.
All sensitive values (credentials) are stored in macOS Keychain - never here.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

APP_DIR = Path.home() / "Library" / "Application Support" / "AmazonEnricher"
CACHE_DB = APP_DIR / "orders.sqlite"
LOG_FILE = APP_DIR / "enricher.log"

KEYCHAIN_SERVICE = "AmazonMoneyMoneyEnricher"
KEYCHAIN_COOKIE_KEY = "amazon_session_cookies"
KEYCHAIN_USERNAME_KEY = "amazon_username"

# Amazon marketplace to use
AMAZON_BASE_URL = "https://www.amazon.de"
AMAZON_ORDERS_URL = f"{AMAZON_BASE_URL}/gp/css/order-history"

# How many days back to search for matching orders
ORDER_LOOKUP_DAYS = 3

# Maximum number of items to show in MoneyMoney description
MAX_ITEMS_IN_DESCRIPTION = 4

# Prefix for enriched comments to identify them later
ENRICHER_COMMENT_PREFIX = "🛒 "

# Pattern to detect Amazon transactions in MoneyMoney
AMAZON_TRANSACTION_PATTERNS = [
    "Amzn",
    "AMAZON",
    "Amazon",
    "AMZ",
]
