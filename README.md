```
 █████╗ ███╗   ███╗ █████╗ ███████╗ ██████╗ ███╗   ██╗
██╔══██╗████╗ ████║██╔══██╗╚══███╔╝██╔═══██╗████╗  ██║
███████║██╔████╔██║███████║  ███╔╝ ██║   ██║██╔██╗ ██║
██╔══██║██║╚██╔╝██║██╔══██║ ███╔╝  ██║   ██║██║╚██╗██║
██║  ██║██║ ╚═╝ ██║██║  ██║███████╗╚██████╔╝██║ ╚████║
╚═╝  ╚═╝╚═╝     ╚═╝╚═╝  ╚═╝╚══════╝ ╚═════╝ ╚═╝  ╚═══╝
███╗   ███╗ ██████╗ ███╗   ██╗███████╗██╗   ██╗███╗   ███╗ ██████╗ ███╗   ██╗███████╗██╗   ██╗
████╗ ████║██╔═══██╗████╗  ██║██╔════╝╚██╗ ██╔╝████╗ ████║██╔═══██╗████╗  ██║██╔════╝╚██╗ ██╔╝
██╔████╔██║██║   ██║██╔██╗ ██║█████╗   ╚████╔╝ ██╔████╔██║██║   ██║██╔██╗ ██║█████╗   ╚████╔╝
██║╚██╔╝██║██║   ██║██║╚██╗██║██╔══╝    ╚██╔╝  ██║╚██╔╝██║██║   ██║██║╚██╗██║██╔══╝    ╚██╔╝
██║ ╚═╝ ██║╚██████╔╝██║ ╚████║███████╗   ██║   ██║ ╚═╝ ██║╚██████╔╝██║ ╚████║███████╗   ██║
╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═══╝╚══════╝   ╚═╝   ╚═╝     ╚═╝ ╚═════╝ ╚═╝  ╚═══╝╚══════╝   ╚═╝
███████╗███╗   ██╗██████╗ ██╗ ██████╗██╗  ██╗███████╗██████╗
██╔════╝████╗  ██║██╔══██╗██║██╔════╝██║  ██║██╔════╝██╔══██╗
█████╗  ██╔██╗ ██║██████╔╝██║██║     ███████║█████╗  ██████╔╝
██╔══╝  ██║╚██╗██║██╔══██╗██║██║     ██╔══██║██╔══╝  ██╔══██╗
███████╗██║ ╚████║██║  ██║██║╚██████╗██║  ██║███████╗██║  ██║
╚══════╝╚═╝  ╚═══╝╚═╝  ╚═╝╚═╝ ╚═════╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝
```

Enriches MoneyMoney bank transaction descriptions with the actual Amazon items behind each charge. It logs into Amazon once with Playwright and supports 2FA, captcha, and secure session reuse via macOS Keychain. The tool matches orders to MoneyMoney transactions by booking date and amount, then writes the product list into the note field. A local SQLite cache keeps repeat runs fast and avoids unnecessary Amazon requests.

The sync now only runs for Amazon transactions that are still uncategorized in MoneyMoney. If no uncategorized Amazon entries exist, the job exits immediately without opening Amazon.

**Before:**
```
Nr xxxx 7023 Amzn.com/BI Gutschriftsbeleg26.03 43.99
```

**After (MoneyMoney Notiz field):**
```
🛒 USB-C Kabel, Druckerpapier A4 500 Blatt [+1 weiterer]
```

---

## How it works

1. Reads Amazon transactions from MoneyMoney via AppleScript
2. Logs into Amazon once (Playwright browser — supports 2FA, captcha)
3. Session state + cookies are stored securely in **macOS Keychain** — no plaintext credentials anywhere
4. Matches orders by booking date (±3 days) and amount
5. Writes item list into the **Notiz** (comment) field of the MoneyMoney transaction
6. Caches all order data locally in SQLite — no redundant Amazon requests

---

## Requirements

- macOS 13+
- Python 3.11+
- [MoneyMoney](https://moneymoney-app.com) with AppleScript enabled

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt
playwright install chromium

# 2. First run — bootstrap login once (browser window opens)
python -m enricher --interactive-login

# 3. Install as background service (runs every hour and at login)
#    Edit com.example.amazon-enricher.plist first — replace YOUR_USERNAME,
#    project path and Python path (for example .venv/bin/python)
cp com.example.amazon-enricher.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.example.amazon-enricher.plist
```

## Usage

```bash
python -m enricher                # Normal run
python -m enricher --dry-run      # Preview without writing to MoneyMoney
python -m enricher --interactive-login  # Force interactive browser login flow
python -m enricher --non-interactive  # No login prompts, safe for launchd/cron
python -m enricher --reset-session  # Clear cookies, force re-login
python -m enricher --store-credentials  # Store Amazon login in macOS Keychain
python -m enricher --clear-credentials  # Remove saved login from Keychain
python -m enricher --import-safari-session  # Reuse the session from a Safari tab already logged into Amazon
```

### Login once, then weekly runs stay automatic

If Amazon expires the web session regularly, store credentials once in Keychain:

```bash
python -m enricher --store-credentials
```

On future runs, the enricher will try to auto-login with saved credentials and usually only ask for manual interaction when Amazon requires MFA/captcha.

For background execution, use `--non-interactive` so the process never hangs waiting for login. If session renewal is needed, run `python -m enricher --interactive-login` once.

### If Amazon keeps rejecting automated login (captcha/new-device loop)

Amazon sometimes treats the automated browser as an untrusted device and throws it into a captcha/MFA loop that never resolves cleanly — retrying `--interactive-login` won't help since it's Amazon's bot-detection, not a wrong password or code. The reliable fix is to reuse the session your real, already-trusted Safari has:

```bash
# 1. Log into amazon.de normally in Safari (if not already)
# 2. Import that session directly — no MFA/captcha involved
python -m enricher --import-safari-session
```

This reads Amazon's session cookies straight out of Safari's cookie store (via `browser_cookie3`) and saves them the same way `--interactive-login` would. It requires Full Disk Access for the terminal/Python running it (System Settings → Privacy & Security → Full Disk Access), since Safari's cookie store is TCC-protected. Like any session, it will eventually expire — just re-run the command when that happens.

## Security

- Credentials are **never stored in files** — only in macOS Keychain
- Session state/cookies encrypted by Keychain, accessible only by this user account
- Cache database (SQLite) contains order data — **excluded from git** via `.gitignore`
- No data is sent to any third party

---

*Inspired by [Amazon-MoneyMoney](https://github.com/Michael-Beutling/Amazon-MoneyMoney)*
