"""
Amazon order scraper.
Fetches order history and matches orders to MoneyMoney transactions
by booking date (±ORDER_LOOKUP_DAYS) and amount.
"""
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from bs4 import BeautifulSoup
from playwright.sync_api import BrowserContext

from .config import (
    AMAZON_ORDERS_URL,
    ORDER_LOOKUP_DAYS,
    MAX_ITEMS_IN_DESCRIPTION,
)
from . import cache

logger = logging.getLogger(__name__)

_ORDER_FILTER_URL = (
    AMAZON_ORDERS_URL
    + "?opt=ab&digitalOrders=1&unifiedOrders=1&returnTo=&orderFilter=months-6"
)


@dataclass
class AmazonOrder:
    order_id: str
    order_date: date
    amount: float
    items: list[str]


def _parse_amount(text: str) -> Optional[float]:
    """Parse German-formatted price like '43,99 €' → 43.99"""
    match = re.search(r"[\d.]+,\d{2}", text)
    if match:
        return float(match.group().replace(".", "").replace(",", "."))
    return None


def _parse_date(text: str) -> Optional[date]:
    """Parse German date formats like '26. März 2025' or '26.03.2025'."""
    months_de = {
        "januar": 1, "februar": 2, "märz": 3, "april": 4,
        "mai": 5, "juni": 6, "juli": 7, "august": 8,
        "september": 9, "oktober": 10, "november": 11, "dezember": 12,
    }
    text = text.strip().lower()

    # Format: "26. märz 2025"
    match = re.search(r"(\d{1,2})\.\s+(\w+)\s+(\d{4})", text)
    if match:
        day, month_str, year = match.groups()
        month = months_de.get(month_str)
        if month:
            return date(int(year), month, int(day))

    # Format: "26.03.2025"
    match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
    if match:
        day, month, year = match.groups()
        return date(int(year), int(month), int(day))

    return None


def _parse_orders_page(html: str) -> list[AmazonOrder]:
    """Parse the Amazon order history page HTML into AmazonOrder objects."""
    soup = BeautifulSoup(html, "html.parser")
    orders = []

    for order_card in soup.select(".order, [class*='order-card'], .a-box-group"):
        try:
            # Order ID
            order_id_el = order_card.select_one("[class*='order-id'] span, .yohtmlc-order-id span")
            if not order_id_el:
                continue
            order_id = order_id_el.get_text(strip=True)

            # Date
            date_el = order_card.select_one(".order-date-invoice-item, [class*='order-date']")
            if not date_el:
                continue
            order_date = _parse_date(date_el.get_text())
            if not order_date:
                continue

            # Amount (grand total)
            total_el = order_card.select_one(".grand-total-price, [class*='grand-total']")
            if not total_el:
                continue
            amount = _parse_amount(total_el.get_text())
            if amount is None:
                continue

            # Items — collect all product titles in the order
            item_elements = order_card.select(".yohtmlc-product-title, [class*='product-title']")
            items = [el.get_text(strip=True) for el in item_elements if el.get_text(strip=True)]

            if not items:
                # Fallback: try generic link titles within order cards
                items = [
                    a.get_text(strip=True)
                    for a in order_card.select("a[href*='/dp/']")
                    if a.get_text(strip=True)
                ]

            if not items:
                items = ["Artikel unbekannt"]

            orders.append(AmazonOrder(
                order_id=order_id,
                order_date=order_date,
                amount=amount,
                items=items,
            ))
        except Exception as e:
            logger.debug(f"Skipping order card due to parse error: {e}")
            continue

    logger.info(f"Parsed {len(orders)} orders from page")
    return orders


def find_matching_order(
    context: BrowserContext,
    booking_date: date,
    amount: float,
) -> Optional[AmazonOrder]:
    """
    Find an Amazon order matching the given booking date and amount.
    Checks cache first, then fetches from Amazon if needed.
    Amount tolerance is ±0.01 € to handle rounding.
    Date window is booking_date ± ORDER_LOOKUP_DAYS (orders ship before charge).
    """
    # Check cache for each date in the window
    for delta in range(-ORDER_LOOKUP_DAYS, ORDER_LOOKUP_DAYS + 1):
        candidate_date = booking_date + timedelta(days=delta)
        cached = cache.get_order_by_date_amount(candidate_date, amount)
        if cached:
            logger.debug(f"Cache hit for {amount}€ on {candidate_date}")
            return AmazonOrder(
                order_id=cached["order_id"],
                order_date=date.fromisoformat(cached["order_date"]),
                amount=cached["amount"],
                items=cached["items"],
            )

    # Cache miss — fetch from Amazon
    logger.info(f"Fetching Amazon orders to match {amount}€ around {booking_date}")
    html = _fetch_orders_html(context)
    orders = _parse_orders_page(html)

    # Cache all fetched orders
    for order in orders:
        cache.save_order(order.order_id, order.order_date, order.amount, order.items)

    # Find match
    window_start = booking_date - timedelta(days=ORDER_LOOKUP_DAYS)
    window_end = booking_date + timedelta(days=ORDER_LOOKUP_DAYS)

    for order in orders:
        date_match = window_start <= order.order_date <= window_end
        amount_match = abs(order.amount - amount) < 0.02
        if date_match and amount_match:
            logger.info(f"Matched order {order.order_id}: {order.items}")
            return order

    logger.warning(f"No Amazon order found for {amount}€ around {booking_date}")
    return None


def _fetch_orders_html(context: BrowserContext) -> str:
    from .amazon_session import fetch_page
    return fetch_page(context, _ORDER_FILTER_URL)


def format_description(order: AmazonOrder, max_items: int = MAX_ITEMS_IN_DESCRIPTION) -> str:
    """
    Format order items for the MoneyMoney comment field.
    Example: "USB-C Kabel, Druckerpapier A4 (2x) [+1 weiterer]"
    """
    items = order.items[:max_items]
    result = ", ".join(items)
    remaining = len(order.items) - max_items
    if remaining > 0:
        result += f" [+{remaining} {'weiterer' if remaining == 1 else 'weitere'}]"
    return result
