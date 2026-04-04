"""
MoneyMoney AppleScript bridge.

Tested against MoneyMoney's actual SDEF dictionary. Available commands:
  export transactions from date "YYYY-MM-DD" [to date "YYYY-MM-DD"] as "plist"
  set transaction id <int> comment to "<text>"

Transaction plist fields: id, name, purpose, amount, bookingDate, bookingText,
  currency, comment (if set), endToEndReference (if present), accountUuid,
  booked, checkmark, category, categoryId, categoryUuid, valueDate
"""
import plistlib
import re
import subprocess
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from .config import (
    AMAZON_TRANSACTION_PATTERNS,
    ENRICHER_COMMENT_PREFIX,
    ENRICHER_REFUND_PREFIX,
    FUTURE_BOOKING_LOOKAHEAD_DAYS,
)

logger = logging.getLogger(__name__)


@dataclass
class MMTransaction:
    id: int
    name: str
    purpose: str
    amount: float
    booking_date: date
    comment: str
    end_to_end_reference: str  # Amazon payment reference, e.g. "3WX110GYHY8M9F36"
    account_uuid: str

    @property
    def is_refund(self) -> bool:
        """Positive Amazon amount = Erstattung (refund/credit)."""
        return self.amount > 0

    @property
    def amazon_order_id(self) -> str:
        """
        Extract Amazon order ID from the purpose field.
        Purpose format: "305.9901323.8516315 Amzn Mktp DE ..."
        Amazon order IDs use dashes: "305-9901323-8516315"
        """
        match = re.search(r"\b(\d{3})\.(\d{7})\.(\d{7})\b", self.purpose)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
        return ""


def _run_applescript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"AppleScript error: {result.stderr.strip()}")
    return result.stdout.strip()


def get_amazon_transactions(days_back: int = 60) -> list[MMTransaction]:
    """
    Export recent transactions from MoneyMoney as plist and filter for Amazon.
    Uses the documented 'export transactions' AppleScript command.
    """
    from_date = (date.today() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    to_date = (date.today() + timedelta(days=FUTURE_BOOKING_LOOKAHEAD_DAYS)).strftime("%Y-%m-%d")

    script = f'tell application "MoneyMoney" to export transactions from date "{from_date}" to date "{to_date}" as "plist"'
    try:
        raw = _run_applescript(script)
    except RuntimeError as e:
        logger.error(f"Failed to export MoneyMoney transactions: {e}")
        return []

    data = plistlib.loads(raw.encode("utf-8"))
    all_transactions = data.get("transactions", [])
    logger.info(f"Exported {len(all_transactions)} transactions from MoneyMoney")

    result = []
    for t in all_transactions:
        tx = MMTransaction(
            id=t["id"],
            name=t.get("name", ""),
            purpose=t.get("purpose", ""),
            amount=t.get("amount", 0.0),
            booking_date=t["bookingDate"].date() if isinstance(t["bookingDate"], datetime) else t["bookingDate"],
            comment=t.get("comment", ""),
            end_to_end_reference=t.get("endToEndReference", ""),
            account_uuid=t.get("accountUuid", ""),
        )
        result.append(tx)

    amazon = [t for t in result if _is_amazon(t)]
    logger.info(f"Amazon transactions: {len(amazon)}")
    return amazon


def _is_amazon(tx: MMTransaction) -> bool:
    text = (tx.name + tx.purpose).upper()
    return any(p.upper() in text for p in AMAZON_TRANSACTION_PATTERNS)


def is_already_enriched(tx: MMTransaction) -> bool:
    return tx.comment.startswith(ENRICHER_COMMENT_PREFIX) or tx.comment.startswith(ENRICHER_REFUND_PREFIX)


def set_transaction_comment(tx: MMTransaction, comment_text: str, is_refund: bool = False) -> bool:
    """
    Write enriched item list into the MoneyMoney transaction comment field.
    Uses 'set transaction id X comment to "..."' AppleScript command.
    """
    if is_already_enriched(tx):
        logger.debug(f"Transaction {tx.id} already enriched, skipping")
        return False

    prefix = ENRICHER_REFUND_PREFIX if is_refund else ENRICHER_COMMENT_PREFIX
    full_comment = f"{prefix}{comment_text}"
    # Escape double quotes for AppleScript
    safe_comment = full_comment.replace('"', '\\"')

    script = f'tell application "MoneyMoney" to set transaction id {tx.id} comment to "{safe_comment}"'
    try:
        _run_applescript(script)
        logger.info(f"Set comment on transaction {tx.id}: {full_comment}")
        return True
    except RuntimeError as e:
        logger.error(f"Failed to set comment on transaction {tx.id}: {e}")
        return False
