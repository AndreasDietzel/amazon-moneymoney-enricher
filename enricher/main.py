"""
Amazon MoneyMoney Enricher - Entry point.

Usage:
    python -m enricher          # normal run
    python -m enricher --dry-run  # preview without writing
    python -m enricher --reset-session  # clear stored cookies and re-login
"""
import argparse
import logging
import sys
import getpass
from pathlib import Path

from playwright.sync_api import sync_playwright

from . import cache
from .amazon_scraper import find_matching_order, find_refund_origin, format_description
from .amazon_session import (
    get_authenticated_context,
    SessionLoginRequiredError,
    _clear_cookies,
    save_credentials,
    clear_credentials,
)
from .config import LOG_FILE, APP_DIR
from .moneymoney import get_amazon_transactions, is_already_enriched, set_transaction_comment

# Logging setup
APP_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


def run(dry_run: bool = False, allow_interactive_login: bool = True, include_categorized: bool = False) -> None:
    logger.info("=== Amazon Enricher gestartet ===")
    cache.init_db()

    # 1. Fetch Amazon transactions from MoneyMoney
    amazon_transactions = get_amazon_transactions(days_back=60, override_filter=include_categorized)
    logger.info(f"Gefundene Amazon-Transaktionen: {len(amazon_transactions)}")

    if not amazon_transactions:
        logger.info("Keine Amazon-Transaktionen zum Anreichern.")
        return

    # Filter already enriched (check comment field AND local cache)
    pending = [
        t for t in amazon_transactions
        if not is_already_enriched(t) and not cache.is_already_enriched(str(t.id))
    ]
    logger.info(f"Noch nicht angereichert: {len(pending)}")

    if not pending:
        logger.info("Alle Transaktionen bereits angereichert.")
        return

    # 2. Open Amazon session (reuses cookies or prompts login)
    with sync_playwright() as playwright:
        try:
            context = get_authenticated_context(
                playwright,
                allow_interactive_login=allow_interactive_login,
            )
        except SessionLoginRequiredError as error:
            logger.warning(str(error))
            logger.warning("Run beendet ohne Änderungen. Nächster Schritt: python -m enricher --interactive-login")
            return

        enriched_count = 0
        failed_count = 0

        try:
            for tx in pending:
                logger.info(f"Verarbeite: {tx.purpose[:60]}... | {tx.amount}€ | {tx.booking_date}")

                if tx.is_refund:
                    order = find_refund_origin(context, tx.amount, tx.booking_date)
                    is_refund = True
                else:
                    order = find_matching_order(
                        context,
                        tx.booking_date,
                        abs(tx.amount),
                        end_to_end_ref=tx.end_to_end_reference,
                        amazon_order_id=tx.amazon_order_id,
                    )
                    is_refund = False

                if not order:
                    logger.warning(f"  ✗ Keine passende Bestellung gefunden")
                    failed_count += 1
                    continue

                tx_amount = abs(tx.amount) if not is_refund else None
                description = format_description(order, tx_amount=tx_amount)
                logger.info(f"  ✓ {'[Erstattung] ' if is_refund else ''}{description}")

                if dry_run:
                    prefix = "[ERSTATTUNG] " if is_refund else ""
                    print(f"[DRY RUN] {prefix}{description}")
                else:
                    success = set_transaction_comment(tx, description, is_refund=is_refund)
                    if success:
                        cache.mark_enriched(str(tx.id), order.order_id)
                        enriched_count += 1
                    else:
                        failed_count += 1
        finally:
            context.browser.close()

    logger.info(f"=== Fertig: {enriched_count} angereichert, {failed_count} fehlgeschlagen ===")


def main() -> None:
    parser = argparse.ArgumentParser(description="Amazon MoneyMoney Enricher")
    parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nicht schreiben")
    parser.add_argument("--interactive-login", action="store_true", help="Erlaubt interaktiven Browser-Login (für Erstanmeldung/Session-Erneuerung)")
    parser.add_argument("--non-interactive", action="store_true", help="Keine Login-Prompts, beendet sauber wenn Session fehlt (für launchd)")
    parser.add_argument("--reset-session", action="store_true", help="Amazon-Session zurücksetzen (erneuter Login)")
    parser.add_argument("--store-credentials", action="store_true", help="Amazon-Zugangsdaten sicher im macOS Keychain speichern")
    parser.add_argument("--clear-credentials", action="store_true", help="Gespeicherte Amazon-Zugangsdaten aus dem Keychain löschen")
    parser.add_argument("--include-categorized", action="store_true", help="Auch kategorisierte Amazon-Transaktionen anreichern (ignoriert ONLY_ENRICH_UNCATEGORIZED)")
    args = parser.parse_args()

    if args.reset_session:
        _clear_cookies()
        print("Session zurückgesetzt. Beim nächsten Start wird ein neuer Login benötigt.")
        return

    if args.store_credentials:
        username = input("Amazon E-Mail: ").strip()
        password = getpass.getpass("Amazon Passwort (wird nicht angezeigt): ")
        if not username or not password:
            print("Abgebrochen: Benutzername/Passwort fehlt.")
            return
        save_credentials(username, password)
        print("Zugangsdaten im macOS Keychain gespeichert.")
        return

    if args.clear_credentials:
        clear_credentials()
        print("Gespeicherte Zugangsdaten aus dem Keychain gelöscht.")
        return

    allow_interactive_login = True
    if args.non_interactive:
        allow_interactive_login = False
    if args.interactive_login:
        allow_interactive_login = True

    run(dry_run=args.dry_run, allow_interactive_login=allow_interactive_login, include_categorized=args.include_categorized)


if __name__ == "__main__":
    main()
