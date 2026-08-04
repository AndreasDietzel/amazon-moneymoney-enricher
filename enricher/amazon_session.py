"""
Amazon session management.
- Uses WebKit (Safari engine) — natürlicher auf macOS, weniger Bot-Erkennung
- Öffnet einmalig ein sichtbares Fenster für den Login (2FA, Captcha unterstützt)
- Session-Cookies werden verschlüsselt im macOS Keychain gespeichert
- Folge-Runs nutzen die gespeicherten Cookies ohne Browser-Öffnung
"""
import json
import logging
import keyring
import os
import time
from playwright.sync_api import BrowserContext, Page

from .config import (
    AMAZON_BASE_URL,
    KEYCHAIN_SERVICE,
    KEYCHAIN_COOKIE_KEY,
    KEYCHAIN_STATE_KEY,
    KEYCHAIN_USERNAME_KEY,
    KEYCHAIN_PASSWORD_KEY,
)

logger = logging.getLogger(__name__)

_ORDERS_URL = f"{AMAZON_BASE_URL}/gp/css/order-history?opt=ab&digitalOrders=1&unifiedOrders=1&returnTo=&orderFilter=months-3"

# Selector that only appears when the order history page is fully loaded
_ORDERS_LOADED_SELECTOR = ".order, [class*='order-card'], #ordersContainer, .order-date-invoice-item, .a-pagination"


class SessionLoginRequiredError(RuntimeError):
    """Raised when no valid Amazon session is available in non-interactive mode."""


def _load_cookies() -> list[dict] | None:
    raw = keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_COOKIE_KEY)
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Gespeicherte Cookies sind ungültig, erneuter Login erforderlich")
    return None


def _load_storage_state() -> dict | None:
    raw = keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_STATE_KEY)
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Gespeicherter Storage-State ist ungültig, erneuter Login erforderlich")
    return None


def _save_cookies(cookies: list[dict]) -> None:
    keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_COOKIE_KEY, json.dumps(cookies))
    logger.debug(f"{len(cookies)} Cookies im Keychain gespeichert")


def _save_storage_state(state: dict) -> None:
    keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_STATE_KEY, json.dumps(state))
    logger.debug("Storage-State im Keychain gespeichert")


def _clear_cookies() -> None:
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, KEYCHAIN_COOKIE_KEY)
    except keyring.errors.PasswordDeleteError:
        pass
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, KEYCHAIN_STATE_KEY)
    except keyring.errors.PasswordDeleteError:
        pass


def _load_credentials() -> tuple[str | None, str | None]:
    """
    Load credentials from env first, then from macOS Keychain.
    This lets scheduled runs work without interactive login when cookies expire.
    """
    env_user = os.getenv("AMAZON_USERNAME")
    env_pass = os.getenv("AMAZON_PASSWORD")
    if env_user and env_pass:
        return env_user, env_pass

    user = keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME_KEY)
    password = keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_PASSWORD_KEY)
    return user, password


def save_credentials(username: str, password: str) -> None:
    keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_USERNAME_KEY, username)
    keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_PASSWORD_KEY, password)


def clear_credentials() -> None:
    for key in (KEYCHAIN_USERNAME_KEY, KEYCHAIN_PASSWORD_KEY):
        try:
            keyring.delete_password(KEYCHAIN_SERVICE, key)
        except keyring.errors.PasswordDeleteError:
            pass


def _try_auto_login(page: Page) -> bool:
    """
    Attempt interactive page login with stored credentials.
    MFA/captcha may still require manual confirmation.
    """
    username, password = _load_credentials()
    if not username or not password:
        logger.info("Keine gespeicherten Amazon-Zugangsdaten gefunden (Keychain/env)")
        return False

    try:
        # Email step
        if page.locator("#ap_email").count() > 0:
            page.fill("#ap_email", username)
            if page.locator("#continue").count() > 0:
                page.click("#continue")
                page.wait_for_load_state("domcontentloaded")

        # Password step
        if page.locator("#ap_password").count() > 0:
            page.fill("#ap_password", password)
            if page.locator("#signInSubmit").count() > 0:
                page.click("#signInSubmit")

        # Give Amazon a moment for redirects/challenges
        page.wait_for_load_state("networkidle", timeout=12_000)
        return True
    except Exception as e:
        logger.debug(f"Auto-Login nicht vollständig möglich: {e}")
        return False


def _is_session_valid(page: Page) -> bool:
    """
    Navigiert zur Bestellseite und wartet auf echten Inhalt.
    Gibt False zurück wenn auf die Login-Seite weitergeleitet wird.
    """
    try:
        page.goto(_ORDERS_URL, wait_until="domcontentloaded")
        # Amazon redirects to sign-in via server-side or JS — wait to settle
        page.wait_for_load_state("networkidle", timeout=10_000)
        url = page.url
        if "sign-in" in url or "ap/signin" in url or "ap/mfa" in url:
            return False
        # Also verify actual order content loaded (not a blank/loading page)
        try:
            page.wait_for_selector(_ORDERS_LOADED_SELECTOR, timeout=8_000)
            return True
        except Exception:
            # Selector not found — page might be sign-in or empty
            logger.debug(f"Order content selector not found on: {url}")
            return False
    except Exception as e:
        logger.debug(f"Session check failed: {e}")
        return False


def get_authenticated_context(playwright, allow_interactive_login: bool = True) -> BrowserContext:
    """
    Gibt einen Playwright BrowserContext mit gültiger Amazon-Session zurück.
    Nutzt WebKit (Safari-Engine). Öffnet bei Bedarf ein Fenster zum Einloggen.
    """
    browser = playwright.webkit.launch(headless=not allow_interactive_login)

    state = _load_storage_state()
    if state:
        context = browser.new_context(storage_state=state)
    else:
        context = browser.new_context()

    cookies = None if state else _load_cookies()
    if cookies:
        context.add_cookies(cookies)
        page = context.new_page()
        if _is_session_valid(page):
            logger.info("Gespeicherte Amazon-Session wiederhergestellt")
            page.close()
            return context
        else:
            logger.info("Session abgelaufen, erneuter Login erforderlich")
            _clear_cookies()
            page.close()
            context.clear_cookies()

    if not allow_interactive_login:
        context.close()
        raise SessionLoginRequiredError(
            "Keine gültige Amazon-Session verfügbar. Bitte einmal interaktiv mit --interactive-login anmelden."
        )

    # Kein gültiger Login — Browser öffnen
    page = context.new_page()
    page.goto(f"{AMAZON_BASE_URL}/gp/sign-in.html", wait_until="domcontentloaded")

    auto_login_attempted = _try_auto_login(page)

    print("\n" + "="*60)
    print("Amazon-Login erforderlich.")
    if auto_login_attempted:
        print("Auto-Login wurde mit gespeicherten Keychain-Zugangsdaten versucht.")
        print("Bitte ggf. nur noch MFA/Captcha bestätigen.")
    else:
        print("Bitte melde dich im geöffneten Browserfenster an.")
    print("Das Fenster schließt sich automatisch nach erfolgreichem Login.")
    print("="*60 + "\n")

    # Wait until order history is really accessible.
    timeout_seconds = 300
    poll_seconds = 3
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if _is_session_valid(page):
            break
        time.sleep(poll_seconds)
    else:
        context.close()
        raise SessionLoginRequiredError(
            "Amazon-Login nicht abgeschlossen (Timeout). Bitte Login inklusive MFA/Captcha vollständig beenden."
        )

    # Give Amazon time to set all session cookies
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass  # Timeout ist OK — Cookies sind bereits gesetzt

    all_cookies = context.cookies()
    _save_cookies(all_cookies)
    _save_storage_state(context.storage_state())
    logger.info(f"Login erfolgreich, {len(all_cookies)} Cookies im Keychain gespeichert")
    page.close()

    return context


def fetch_page(context: BrowserContext, url: str) -> str:
    """
    Lädt eine URL im authentifizierten Kontext und gibt den HTML-Inhalt zurück.
    Wartet auf networkidle damit JavaScript-gerenderte Inhalte vollständig geladen sind.
    """
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=15_000)
        return page.content()
    except Exception as e:
        logger.warning(f"Timeout beim Laden von {url}: {e} — nehme verfügbaren Inhalt")
        return page.content()
    finally:
        page.close()
