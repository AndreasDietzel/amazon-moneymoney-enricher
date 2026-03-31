"""
MoneyMoney AppleScript bridge.
Reads Amazon transactions and writes enriched comments back.

MoneyMoney's writable field per transaction is `comment` (Notiz).
The bank-provided `purpose` field is read-only.

AppleScript dictionary reference:
  transaction properties: name, purpose, amount, bookingDate, comment,
                          bankCode, accountNumber, category, currency
"""
import subprocess
import json
import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

from .config import AMAZON_TRANSACTION_PATTERNS, ENRICHER_COMMENT_PREFIX

logger = logging.getLogger(__name__)


@dataclass
class MMTransaction:
    transaction_id: str   # synthetic: accountNumber + bookingDate + amount
    name: str
    purpose: str
    amount: float
    booking_date: date
    account_number: str
    comment: str


def _run_applescript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"AppleScript error: {result.stderr.strip()}")
    return result.stdout.strip()


def _run_applescript_file(path: str) -> str:
    result = subprocess.run(
        ["osascript", path],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"AppleScript error: {result.stderr.strip()}")
    return result.stdout.strip()


def get_amazon_transactions(days_back: int = 60) -> list[MMTransaction]:
    """
    Fetch recent transactions from all MoneyMoney accounts
    that match Amazon payment patterns.
    """
    script = f"""
    tell application "MoneyMoney"
        set txList to {{}}
        set allAccounts to every account
        repeat with acc in allAccounts
            set txns to transactions of acc where booking date >= (current date) - {days_back} * days
            repeat with t in txns
                set txList to txList & {{{{name:(name of t), purpose:(purpose of t), amount:(amount of t), bookingDate:(booking date of t as string), comment:(comment of t), accountNumber:(account number of acc)}}}}
            end repeat
        end repeat
        return txList
    end tell
    """
    # NOTE: MoneyMoney's AppleScript API for transaction iteration varies by version.
    # This implementation uses the documented properties. Adjust if needed.
    try:
        raw = _run_applescript(script)
        return _parse_transactions(raw)
    except RuntimeError as e:
        logger.error(f"Failed to fetch MoneyMoney transactions: {e}")
        logger.info("Falling back to CSV export approach - see docs/applescript-troubleshooting.md")
        return []


def _parse_transactions(raw: str) -> list[MMTransaction]:
    """Parse AppleScript record list output into MMTransaction objects."""
    transactions = []
    # AppleScript returns records as comma-separated key:value pairs
    # Each record is wrapped in {} — parse conservatively
    # Real parsing depends on actual AppleScript output format
    # This is a stub that will be refined once we test against live MoneyMoney
    logger.debug(f"Raw AppleScript output length: {len(raw)}")
    return transactions


def _is_amazon(tx: MMTransaction) -> bool:
    text = (tx.name + tx.purpose).upper()
    return any(p.upper() in text for p in AMAZON_TRANSACTION_PATTERNS)


def filter_amazon(transactions: list[MMTransaction]) -> list[MMTransaction]:
    return [t for t in transactions if _is_amazon(t)]


def set_transaction_comment(transaction: MMTransaction, comment_text: str) -> bool:
    """
    Write enriched item list into the MoneyMoney transaction comment (Notiz) field.
    Only updates if the comment isn't already enriched by us.
    """
    if transaction.comment.startswith(ENRICHER_COMMENT_PREFIX):
        logger.debug(f"Transaction {transaction.transaction_id} already enriched, skipping")
        return False

    full_comment = f"{ENRICHER_COMMENT_PREFIX}{comment_text}"

    script = f"""
    tell application "MoneyMoney"
        set allAccounts to every account
        repeat with acc in allAccounts
            if account number of acc is "{transaction.account_number}" then
                set txns to transactions of acc where booking date is date "{transaction.booking_date.strftime('%d.%m.%Y')}"
                repeat with t in txns
                    if amount of t is {transaction.amount} then
                        set comment of t to "{full_comment}"
                        return "ok"
                    end if
                end repeat
            end if
        end repeat
        return "not_found"
    end tell
    """
    try:
        result = _run_applescript(script)
        if result == "ok":
            logger.info(f"Updated comment for transaction {transaction.transaction_id}")
            return True
        else:
            logger.warning(f"Transaction not found in MoneyMoney: {transaction.transaction_id}")
            return False
    except RuntimeError as e:
        logger.error(f"Failed to set comment: {e}")
        return False
