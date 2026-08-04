"""
Configuration for Amazon MoneyMoney Enricher.
All sensitive values (credentials) are stored in macOS Keychain - never here.
"""
from pathlib import Path

APP_DIR = Path.home() / "Library" / "Application Support" / "AmazonEnricher"
CACHE_DB = APP_DIR / "orders.sqlite"
LOG_FILE = APP_DIR / "enricher.log"

KEYCHAIN_SERVICE = "AmazonMoneyMoneyEnricher"
KEYCHAIN_COOKIE_KEY = "amazon_session_cookies"
KEYCHAIN_STATE_KEY = "amazon_storage_state"
KEYCHAIN_USERNAME_KEY = "amazon_username"
KEYCHAIN_PASSWORD_KEY = "amazon_password"

# Amazon marketplace to use
AMAZON_BASE_URL = "https://www.amazon.de"
AMAZON_ORDERS_URL = f"{AMAZON_BASE_URL}/gp/css/order-history"

# How many days back to search for matching orders.
# Amazon charges 1-6 days after the order date (when item ships).
ORDER_LOOKUP_DAYS = 7

# How many days ahead to include from MoneyMoney exports.
# This catches Amazon card transactions that already appear in MoneyMoney's
# uncategorized view before their final booking date.
FUTURE_BOOKING_LOOKAHEAD_DAYS = 7

# Maximum number of items to show in MoneyMoney description
MAX_ITEMS_IN_DESCRIPTION = 4

# Prefixes for enriched comments
ENRICHER_COMMENT_PREFIX = "🛒 "
ENRICHER_REFUND_PREFIX  = "↩ Erstattung: "

# Only enrich Amazon transactions that are still uncategorized in MoneyMoney.
# This keeps the job focused on unresolved entries.
ONLY_ENRICH_UNCATEGORIZED = True

# Retry settings for temporary MoneyMoney lock errors.
MONEYMONEY_LOCK_RETRY_ATTEMPTS = 3
MONEYMONEY_LOCK_RETRY_BACKOFF_SECONDS = [30, 90, 180]

# Pattern to detect Amazon transactions in MoneyMoney
AMAZON_TRANSACTION_PATTERNS = [
    "Amzn",
    "AMAZON",
    "Amazon",
    "AMZ",
]
