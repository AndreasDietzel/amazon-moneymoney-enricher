"""
Amazon order scraper.
Matches MoneyMoney transactions to Amazon orders using two strategies:
  1. Primary: SEPA endToEndReference → Amazon order search (precise, no false matches)
  2. Fallback: booking date (±ORDER_LOOKUP_DAYS) + amount (±0.02€)
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

_ORDER_HISTORY_URL = AMAZON_ORDERS_URL + "?opt=ab&digitalOrders=1&unifiedOrders=1&returnTo=&orderFilter=months-6"
_MAX_PAGES = 5  # Fetch up to 5 pages (50 orders) per run


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
    """
    Parse Amazon order history page (amazon.de).
    Tested against live HTML — selectors as of March 2026.

    Structure per .js-order-card:
      - date:     first span.a-size-base.a-color-secondary.aok-break-word
      - amount:   second span.a-size-base.a-color-secondary.aok-break-word
      - order_id: span.a-color-secondary matching XXX-XXXXXXX-XXXXXXX pattern
      - items:    a[href*='/dp/'] product links (text = title)
    """
    soup = BeautifulSoup(html, "html.parser")
    orders = []

    for card in soup.select(".js-order-card"):
        try:
            info_spans = card.select("span.a-size-base.a-color-secondary.aok-break-word")
            if len(info_spans) < 2:
                continue

            order_date = _parse_date(info_spans[0].get_text())
            if not order_date:
                continue

            amount = _parse_amount(info_spans[1].get_text())
            if amount is None:
                continue

            # Order ID: XXX-XXXXXXX-XXXXXXX pattern
            order_id = None
            for span in card.select("span.a-color-secondary"):
                txt = span.get_text(strip=True)
                if re.match(r"^\d{3}-\d{7}-\d{7}$", txt):
                    order_id = txt
                    break
            if not order_id:
                continue

            # Product titles from /dp/ links
            items = []
            seen = set()
            for a in card.select("a[href*='/dp/']"):
                title = a.get_text(strip=True)
                if title and title not in seen and len(title) > 5:
                    items.append(title)
                    seen.add(title)

            if not items:
                # Fallback: img alt texts
                for img in card.select("img[alt]"):
                    alt = img.get("alt", "").strip()
                    if alt and len(alt) > 5 and alt not in seen:
                        items.append(alt)
                        seen.add(alt)

            if not items:
                items = ["Artikel unbekannt"]

            orders.append(AmazonOrder(
                order_id=order_id,
                order_date=order_date,
                amount=amount,
                items=items,
            ))
        except Exception as e:
            logger.debug(f"Skipping order card: {e}")
            continue

    logger.info(f"Parsed {len(orders)} orders from page")
    return orders


def find_matching_order(
    context: BrowserContext,
    booking_date: date,
    amount: float,
    end_to_end_ref: str = "",
    amazon_order_id: str = "",
) -> Optional[AmazonOrder]:
    """
    Find an Amazon order for a MoneyMoney transaction.

    Strategy 1: direct order detail page by order ID extracted from purpose field
               (handles split shipments charged as separate transactions)
    Strategy 2: cache lookup by reference or date+amount
    Strategy 3: fetch all pages from Amazon, match by date ±ORDER_LOOKUP_DAYS + amount ±0.02€
    """
    # --- Strategy 1: direct lookup by Amazon order ID from purpose field ---
    if amazon_order_id:
        cached = cache.get_order_by_id(amazon_order_id)
        if cached:
            logger.debug(f"Cache hit by order ID {amazon_order_id}")
            return AmazonOrder(**_row_to_kwargs(cached))
        order = _fetch_order_by_id(context, amazon_order_id)
        if order:
            cache.save_order(order.order_id, order.order_date, order.amount, order.items, end_to_end_ref)
            logger.info(f"Order ID match → {order.order_id}: {order.items}")
            return order

    # --- Strategy 2: cache lookup by reference or date+amount ---
    if end_to_end_ref:
        cached = cache.get_order_by_reference(end_to_end_ref)
        if cached:
            logger.debug(f"Cache hit by reference {end_to_end_ref}")
            return AmazonOrder(**_row_to_kwargs(cached))

    for delta in range(-ORDER_LOOKUP_DAYS, ORDER_LOOKUP_DAYS + 1):
        candidate_date = booking_date + timedelta(days=delta)
        cached = cache.get_order_by_date_amount(candidate_date, amount)
        if cached:
            logger.debug(f"Cache hit by date+amount: {amount}€ on {candidate_date}")
            return AmazonOrder(**_row_to_kwargs(cached))

    # --- Strategy 3: fetch all pages, match by date + amount ---
    logger.info(f"Fetching Amazon orders to match {amount}€ around {booking_date}")
    orders = _fetch_all_orders(context)

    window_start = booking_date - timedelta(days=ORDER_LOOKUP_DAYS)
    window_end   = booking_date + timedelta(days=ORDER_LOOKUP_DAYS)
    matched = None
    for order in orders:
        if window_start <= order.order_date <= window_end and abs(order.amount - amount) < 0.02:
            matched = order
            break

    for order in orders:
        ref = end_to_end_ref if (matched and order.order_id == matched.order_id) else ""
        cache.save_order(order.order_id, order.order_date, order.amount, order.items, ref)

    if matched:
        logger.info(f"Date+amount match → order {matched.order_id}: {matched.items}")
        return matched

    logger.warning(f"No Amazon order found for {amount}€ around {booking_date} (order_id={amazon_order_id!r})")
    return None


def find_refund_origin(context: BrowserContext, amount: float, booking_date: date) -> Optional[AmazonOrder]:
    """
    For a refund transaction (positive amount), find the original order.
    Looks in cache for an order with the same absolute amount, then falls back to Amazon.
    """
    abs_amount = abs(amount)

    # Search cache across a wider window (refunds can come weeks after order)
    with cache._connect() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE ABS(amount - ?) < 0.02 ORDER BY ABS(julianday(order_date) - julianday(?)) LIMIT 1",
            (abs_amount, booking_date.isoformat())
        ).fetchone()
        if row:
            import json
            return AmazonOrder(
                order_id=row["order_id"],
                order_date=date.fromisoformat(row["order_date"]),
                amount=row["amount"],
                items=json.loads(row["items"]),
            )

    # Not in cache — fetch order history and search
    orders = _fetch_all_orders(context)
    for order in orders:
        cache.save_order(order.order_id, order.order_date, order.amount, order.items)
        if abs(order.amount - abs_amount) < 0.02:
            return order
    return None


def _row_to_kwargs(row: dict) -> dict:
    return {
        "order_id":   row["order_id"],
        "order_date": date.fromisoformat(row["order_date"]),
        "amount":     row["amount"],
        "items":      row["items"],
    }


def _fetch_all_orders(context: BrowserContext) -> list[AmazonOrder]:
    """Fetch all orders across multiple pages (up to _MAX_PAGES)."""
    from .amazon_session import fetch_page
    from bs4 import BeautifulSoup as _BS
    from .config import AMAZON_BASE_URL

    all_orders: list[AmazonOrder] = []
    url = _ORDER_HISTORY_URL

    for page_num in range(1, _MAX_PAGES + 1):
        html = fetch_page(context, url)
        orders = _parse_orders_page(html)
        all_orders.extend(orders)
        logger.info(f"Page {page_num}: {len(orders)} orders (total so far: {len(all_orders)})")

        # Find next-page link
        soup = _BS(html, "html.parser")
        next_link = soup.select_one(".a-pagination .a-last:not(.a-disabled) a")
        if not next_link:
            break
        next_href = next_link.get("href", "")
        if not next_href:
            break
        url = AMAZON_BASE_URL + next_href if next_href.startswith("/") else next_href

    return all_orders


def _fetch_order_by_id(context: BrowserContext, order_id: str) -> Optional[AmazonOrder]:
    """
    Fetch a single order's detail page and parse items from it.
    URL: https://www.amazon.de/gp/your-account/order-details?orderID=XXX-XXXXXXX-XXXXXXX
    """
    from .amazon_session import fetch_page
    from .config import AMAZON_BASE_URL

    url = f"{AMAZON_BASE_URL}/gp/your-account/order-details?orderID={order_id}"
    html = fetch_page(context, url)
    soup = BeautifulSoup(html, "html.parser")

    # Parse order date
    order_date = None
    for span in soup.select("span.a-size-base.a-color-secondary"):
        d = _parse_date(span.get_text())
        if d:
            order_date = d
            break

    # Parse total amount — last "Gesamtbetrag" or grand total row
    amount = None
    for row in soup.select(".a-row"):
        text = row.get_text(" ", strip=True)
        if "Gesamtbetrag" in text or "Grand Total" in text or "Bestellsumme" in text:
            a = _parse_amount(text)
            if a is not None:
                amount = a
                break
    # Fallback: any price on the page
    if amount is None:
        for span in soup.select("span.a-color-price, .grand-total-price"):
            a = _parse_amount(span.get_text())
            if a is not None:
                amount = a
                break

    # Parse product titles
    items = []
    seen: set[str] = set()
    for a_tag in soup.select("a[href*='/dp/']"):
        title = a_tag.get_text(strip=True)
        if title and len(title) > 5 and title not in seen:
            items.append(title)
            seen.add(title)
    if not items:
        for img in soup.select("img[alt]"):
            alt = img.get("alt", "").strip()
            if alt and len(alt) > 5 and alt not in seen:
                items.append(alt)
                seen.add(alt)
    if not items:
        items = ["Artikel unbekannt"]

    if not order_date or amount is None:
        logger.warning(f"Could not parse date/amount from order detail page for {order_id}")
        # Still return with what we have if we have items
        if not order_date:
            from datetime import date as _date
            order_date = _date.today()
        if amount is None:
            amount = 0.0

    logger.info(f"Fetched order detail {order_id}: {items}")
    return AmazonOrder(order_id=order_id, order_date=order_date, amount=amount, items=items)


def _fetch_page(context: BrowserContext, url: str) -> str:
    from .amazon_session import fetch_page
    return fetch_page(context, url)


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
