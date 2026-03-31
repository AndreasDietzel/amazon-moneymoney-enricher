"""
Amazon session management.
- First run: opens a real browser window via Playwright for login (handles 2FA, captcha)
- Session cookies are stored encrypted in macOS Keychain
- Subsequent runs reuse the stored cookies silently
"""
import json
import logging
import keyring
from playwright.sync_api import sync_playwright, BrowserContext, Page

from .config import (
    AMAZON_BASE_URL,
    KEYCHAIN_SERVICE,
    KEYCHAIN_COOKIE_KEY,
    KEYCHAIN_USERNAME_KEY,
)

logger = logging.getLogger(__name__)

# Page that requires login to confirm session is valid
_SESSION_CHECK_URL = f"{AMAZON_BASE_URL}/gp/css/order-history?opt=ab&digitalOrders=1&unifiedOrders=1&returnTo=&orderFilter=months-6"


def _load_cookies() -> list[dict] | None:
    raw = keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_COOKIE_KEY)
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Stored cookies are malformed, will re-login")
    return None


def _save_cookies(cookies: list[dict]) -> None:
    keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_COOKIE_KEY, json.dumps(cookies))
    logger.debug(f"Saved {len(cookies)} cookies to Keychain")


def _clear_cookies() -> None:
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, KEYCHAIN_COOKIE_KEY)
    except keyring.errors.PasswordDeleteError:
        pass


def _is_session_valid(page: Page) -> bool:
    """Check if we're logged in by navigating to orders page."""
    page.goto(_SESSION_CHECK_URL, wait_until="domcontentloaded")
    # If redirected to sign-in, session is invalid
    return "sign-in" not in page.url and "ap/signin" not in page.url


def get_authenticated_context(playwright) -> BrowserContext:
    """
    Returns a Playwright BrowserContext with a valid Amazon session.
    If no valid cookies exist, opens a visible browser for the user to log in.
    """
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    )

    cookies = _load_cookies()
    if cookies:
        context.add_cookies(cookies)
        page = context.new_page()
        if _is_session_valid(page):
            logger.info("Reusing stored Amazon session")
            page.close()
            return context
        else:
            logger.info("Stored session expired, re-login required")
            _clear_cookies()
            page.close()
            context.clear_cookies()

    # No valid session — open browser for manual login
    page = context.new_page()
    page.goto(f"{AMAZON_BASE_URL}/ap/signin", wait_until="domcontentloaded")

    print("\n" + "="*60)
    print("Amazon-Login erforderlich.")
    print("Bitte melde dich im geöffneten Browserfenster an.")
    print("Das Fenster schließt sich automatisch nach erfolgreichem Login.")
    print("="*60 + "\n")

    # Wait until the user reaches the orders or home page (login complete)
    page.wait_for_url(
        lambda url: "order-history" in url or "/gp/css" in url or url == f"{AMAZON_BASE_URL}/",
        timeout=300_000  # 5 minutes
    )

    # Persist cookies
    all_cookies = context.cookies()
    _save_cookies(all_cookies)
    logger.info("Login successful, session cookies saved to Keychain")
    page.close()

    return context


def fetch_page(context: BrowserContext, url: str) -> str:
    """Fetch a URL with the authenticated context, return page HTML."""
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded")
        return page.content()
    finally:
        page.close()
