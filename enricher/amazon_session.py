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
from playwright.sync_api import BrowserContext, Page
try:
    from playwright_stealth import stealth_sync as _stealth
except ImportError:
    _stealth = None  # optional – graceful fallback
try:
    import pyotp
except ImportError:
    pyotp = None  # optional – TOTP-Autofill nur wenn installiert

from .config import (
    AMAZON_BASE_URL,
    KEYCHAIN_SERVICE,
    KEYCHAIN_COOKIE_KEY,
    KEYCHAIN_STATE_KEY,
    KEYCHAIN_USERNAME_KEY,
    KEYCHAIN_PASSWORD_KEY,
    KEYCHAIN_TOTP_KEY,
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


def _load_totp_secret() -> str | None:
    return keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_TOTP_KEY)


def save_totp_secret(secret: str) -> None:
    # Normalize like an authenticator app would (spaces are a common copy/paste artifact)
    keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_TOTP_KEY, secret.replace(" ", "").upper())


def clear_totp_secret() -> None:
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, KEYCHAIN_TOTP_KEY)
    except keyring.errors.PasswordDeleteError:
        pass


def _try_auto_totp(page: Page) -> bool:
    """
    Fills Amazon's authenticator-app OTP challenge from the stored TOTP secret.
    Returns True if an OTP field was found and a code was submitted.
    """
    otp_field = page.locator("#auth-mfa-otpcode, input[name='otpCode']")
    if otp_field.count() == 0:
        return False

    secret = _load_totp_secret()
    if not secret or not pyotp:
        logger.warning("MFA-Code angefordert, aber kein TOTP-Secret gespeichert (--store-totp-secret)")
        return False

    code = pyotp.TOTP(secret).now()
    otp_field.first.fill(code)

    # Reduces future MFA prompts for this browser fingerprint/session
    remember = page.locator("#auth-mfa-remember-device")
    if remember.count() > 0 and not remember.first.is_checked():
        remember.first.check()

    submit = page.locator("#auth-signin-button, input[type='submit']")
    if submit.count() > 0:
        submit.first.click()
        page.wait_for_load_state("networkidle", timeout=12_000)
    return True


def _try_auto_login(page: Page, auto_totp: bool = True) -> bool:
    """
    Attempt full page login with stored credentials, including the TOTP MFA
    step when Amazon challenges for it. Captcha still requires manual entry.

    auto_totp=False skips the automatic OTP fill+submit (used for interactive
    logins, where a race between the stored secret and the user's own device
    would burn the challenge and force Amazon to reissue it as "invalid").
    """
    username, password = _load_credentials()
    if not username or not password:
        logger.info("Keine gespeicherten Amazon-Zugangsdaten gefunden (Keychain/env)")
        return False

    try:
        # Email step (Amazon renamed #ap_email -> #ap_email_login and dropped
        # the separate #continue button in favor of the form's plain submit)
        email_field = page.locator("#ap_email, #ap_email_login")
        if email_field.count() > 0:
            email_field.first.fill(username)
            continue_btn = page.locator("#continue, input[type='submit']")
            if continue_btn.count() > 0:
                continue_btn.first.click()
                page.wait_for_load_state("domcontentloaded")

        # Password step
        if page.locator("#ap_password").count() > 0:
            page.fill("#ap_password", password)
            if page.locator("#signInSubmit").count() > 0:
                page.click("#signInSubmit")
                page.wait_for_load_state("networkidle", timeout=12_000)

        # MFA step (authenticator-app OTP) — auto-filled from stored secret
        if auto_totp:
            _try_auto_totp(page)

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
        if _stealth:
            _stealth(page)
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


def _attempt_headless_login(browser) -> BrowserContext | None:
    """
    Fully unattended login for scheduled/non-interactive runs: fills stored
    email + password, answers the TOTP-MFA challenge from the stored secret,
    and saves the resulting session. Returns None (does not raise) on any
    failure — e.g. missing credentials, captcha, or a challenge type that
    can't be automated — so the caller can fall back to interactive login.
    """
    context = browser.new_context()
    page = context.new_page()
    if _stealth:
        _stealth(page)
    try:
        page.goto(f"{AMAZON_BASE_URL}/gp/sign-in.html", wait_until="domcontentloaded")
        if not _try_auto_login(page):
            logger.info("Headless Auto-Login übersprungen (keine Zugangsdaten gespeichert)")
            page.close()
            context.close()
            return None

        if _is_session_valid(page):
            logger.info("Headless Auto-Login erfolgreich (Zugangsdaten + TOTP)")
            page.close()
            all_cookies = context.cookies()
            _save_cookies(all_cookies)
            _save_storage_state(context.storage_state())
            return context

        logger.warning("Headless Auto-Login fehlgeschlagen (Captcha/neues Gerät?) — aktuelle URL: " + page.url)
        page.close()
        context.close()
        return None
    except Exception as e:
        logger.warning(f"Headless Auto-Login fehlgeschlagen: {e}")
        try:
            page.close()
        except Exception:
            pass
        context.close()
        return None


def get_authenticated_context(playwright, allow_interactive_login: bool = True) -> BrowserContext:
    """
    Gibt einen Playwright BrowserContext mit gültiger Amazon-Session zurück.
    Interaktiver Login: Chromium (erscheint im macOS Dock, bessere Sichtbarkeit).
    Headless-Runs: WebKit (Safari-Engine, weniger Bot-Erkennung).
    """
    if allow_interactive_login:
        # Chromium für interaktiven Login — erscheint im Dock und kann fokussiert werden.
        # Bevorzuge system-installierten Chrome (bessere macOS-Integration), Fallback auf Playwright-Chromium.
        try:
            browser = playwright.chromium.launch(headless=False, channel="chrome")
        except Exception:
            browser = playwright.chromium.launch(headless=False)
    else:
        browser = playwright.webkit.launch(headless=True)

    # Try storage state first (most complete — includes localStorage/sessionStorage)
    state = _load_storage_state()
    if state:
        context = browser.new_context(storage_state=state)
        page = context.new_page()
        if _is_session_valid(page):
            logger.info("Gespeicherte Amazon-Session wiederhergestellt")
            page.close()
            return context
        logger.info("Storage-State abgelaufen, erneuter Login erforderlich")
        _clear_cookies()
        page.close()
        context.close()
        context = browser.new_context()
    else:
        context = browser.new_context()
        cookies = _load_cookies()
        if cookies:
            context.add_cookies(cookies)
            page = context.new_page()
            if _is_session_valid(page):
                logger.info("Gespeicherte Amazon-Session wiederhergestellt")
                page.close()
                return context
            logger.info("Session abgelaufen, erneuter Login erforderlich")
            _clear_cookies()
            page.close()
            context.clear_cookies()

    if not allow_interactive_login:
        context.close()
        headless_context = _attempt_headless_login(browser)
        if headless_context:
            return headless_context
        browser.close()
        raise SessionLoginRequiredError(
            "Automatischer Login (Zugangsdaten + TOTP) fehlgeschlagen — vermutlich Captcha oder neues Gerät. "
            "Bitte einmal interaktiv mit --interactive-login anmelden."
        )

    # Kein gültiger Login — Browser öffnen
    page = context.new_page()
    if _stealth:
        _stealth(page)  # Bot-Detection maskieren bevor erste Navigation
    page.goto(f"{AMAZON_BASE_URL}/gp/sign-in.html", wait_until="domcontentloaded")
    page.bring_to_front()  # Browser-Fenster in den Vordergrund

    # macOS-Notification damit der User weiß, dass ein Browser-Fenster wartet
    import subprocess
    # Gesprochene Ansage (immer hörbar, unabhängig von Fensterfokus)
    subprocess.Popen(["say", "-v", "Anna", "Bitte im Browser bei Amazon einloggen"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Notification-Banner
    subprocess.run(
        ["osascript", "-e",
         'display notification "Bitte im Browser bei Amazon einloggen" '
         'with title "Amazon Enricher" sound name "Glass"'],
        check=False, capture_output=True,
    )
    # Browser-Fenster in den Vordergrund — System Events findet den Playwright-Chromium-Prozess
    subprocess.run(
        ["osascript", "-e",
         'tell application "System Events"\n'
         '  set procs to (every process whose name contains "Chrom")\n'
         '  if length of procs > 0 then\n'
         '    set frontmost of item 1 of procs to true\n'
         '  end if\n'
         'end tell'],
        check=False, capture_output=True,
    )

    auto_login_attempted = _try_auto_login(page, auto_totp=False)

    print("\n" + "="*60)
    print("⚠️  AMAZON-LOGIN ERFORDERLICH")
    print()
    if auto_login_attempted:
        print("Auto-Login mit gespeicherten Zugangsdaten versucht.")
        print("→ Bitte NUR noch MFA/Captcha im Browser bestätigen.")
    else:
        print("→ WECHSLE JETZT ZUM GERADE GEÖFFNETEN CHROME-FENSTER!")
        print("  (du hörst einen Ton + siehst eine Benachrichtigung)")
        print("  E-Mail, Passwort und ggf. MFA-Code eingeben.")
        print()
        print("  Tipp: --store-credentials einmalig ausführen für")
        print("         automatischen Login beim nächsten Mal.")
    print()
    print("Das Fenster schließt sich automatisch nach erfolgreichem Login.")
    print("(Timeout: 10 Minuten)")
    print("="*60 + "\n")

    # Poll every 3 s until the browser leaves all Amazon sign-in/auth pages.
    # Using a polling loop (instead of wait_for_url) to log the current URL for debugging.
    import time
    # ax/ = OpenID intermediate pages (ax/claim, ax/authportal, etc.) – warte bis diese abgeschlossen sind
    _AUTH_PATHS = ("ap/signin", "ap/mfa", "ap/cvf", "ap/register", "ap/captcha", "ap/challenge", "/ax/")
    deadline = time.time() + 600  # 10 Minuten
    last_logged_url = ""
    logged_in = False

    while time.time() < deadline:
        try:
            current_url = page.url
        except Exception:
            current_url = ""

        if current_url and current_url != last_logged_url:
            logger.info(f"Browser-URL: {current_url}")
            last_logged_url = current_url

        if current_url and not any(p in current_url for p in _AUTH_PATHS):
            # Muss eine echte Amazon-Seite sein (home page oder orders), nicht eine Zwischenseite
            if ("amazon.de/" in current_url and
                    not current_url.rstrip("/").endswith("amazon.de")):
                # Landed on a real subpage (e.g. /gp/..., /?...) → vollständig eingeloggt
                logged_in = True
                break
            elif current_url in (f"https://www.amazon.de/", f"https://www.amazon.de"):
                logged_in = True
                break

        page.wait_for_timeout(3_000)

    if not logged_in:
        context.close()
        raise SessionLoginRequiredError(
            "Amazon-Login nicht abgeschlossen (Timeout). Bitte Login inklusive MFA/Captcha vollständig beenden."
        )

    logger.info("Login im Browser erfolgreich abgeschlossen")

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


class SafariCookieImportError(RuntimeError):
    """Raised when no usable Amazon session could be read from Safari's cookie store."""


def import_safari_session(playwright) -> None:
    """
    Reads the Amazon session cookies straight out of the user's own logged-in
    Safari (via browser_cookie3), validates them in a headless context, and
    saves them to the Keychain — the same place get_authenticated_context()
    reads from. Sidesteps Amazon's bot/new-device MFA challenges entirely,
    since it's reusing a session Amazon already trusts.

    Requires Full Disk Access for the terminal/python running this, since
    Safari's cookie store is TCC-protected.
    """
    import browser_cookie3

    # Bare "amazon.de" (not "www.amazon.de") so browser_cookie3's substring
    # match also picks up cookies scoped to ".amazon.de" (at-acbde, session-id, …).
    domain = AMAZON_BASE_URL.split("//")[-1].removeprefix("www.")
    try:
        raw_cookies = list(browser_cookie3.safari(domain_name=domain))
    except Exception as e:
        raise SafariCookieImportError(
            f"Safari-Cookies konnten nicht gelesen werden: {e}. "
            "Terminal/Python braucht ggf. Full Disk Access "
            "(Systemeinstellungen → Datenschutz & Sicherheit → Vollständiger Festplattenzugriff)."
        ) from e

    if not raw_cookies:
        raise SafariCookieImportError(
            f"Keine {domain}-Cookies in Safari gefunden. Bitte erst in Safari bei Amazon einloggen."
        )

    seen = set()
    pw_cookies = []
    for c in raw_cookies:
        key = (c.name, c.domain, c.path)
        if key in seen:
            continue
        seen.add(key)
        entry = {
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path or "/",
            "secure": bool(c.secure),
            "httpOnly": c.name in ("at-acbde", "sess-at-acbde", "x-acbde", "session-token"),
            "sameSite": "Lax",
        }
        if c.expires:
            entry["expires"] = c.expires
        pw_cookies.append(entry)

    browser = playwright.webkit.launch(headless=True)
    try:
        context = browser.new_context()
        context.add_cookies(pw_cookies)
        page = context.new_page()
        valid = _is_session_valid(page)
        page.close()
        if not valid:
            context.close()
            raise SafariCookieImportError(
                "Aus Safari importierte Cookies ergeben keine gültige Amazon-Session. "
                "Bitte in Safari neu bei Amazon einloggen und erneut versuchen."
            )
        all_cookies = context.cookies()
        _save_cookies(all_cookies)
        _save_storage_state(context.storage_state())
        context.close()
    finally:
        browser.close()


def fetch_page(context: BrowserContext, url: str) -> str:
    """
    Lädt eine URL im authentifizierten Kontext und gibt den HTML-Inhalt zurück.
    Wartet auf networkidle damit JavaScript-gerenderte Inhalte vollständig geladen sind.
    """
    page = context.new_page()
    if _stealth:
        _stealth(page)
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=15_000)
        return page.content()
    except Exception as e:
        logger.warning(f"Timeout beim Laden von {url}: {e} — nehme verfügbaren Inhalt")
        return page.content()
    finally:
        page.close()
