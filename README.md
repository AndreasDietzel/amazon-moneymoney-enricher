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

Enriches MoneyMoney bank transaction descriptions with actual Amazon order details.

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
3. Session cookies are stored securely in **macOS Keychain** — no plaintext credentials anywhere
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

# 2. First run — browser window opens for Amazon login
python -m enricher

# 3. Install as background service (runs every 4 hours)
cp com.andreasdietzel.amazon-enricher.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.andreasdietzel.amazon-enricher.plist
```

## Usage

```bash
python -m enricher                # Normal run
python -m enricher --dry-run      # Preview without writing to MoneyMoney
python -m enricher --reset-session  # Clear cookies, force re-login
```

## Security

- Credentials are **never stored in files** — only in macOS Keychain
- Session cookies encrypted by Keychain, accessible only by this user account
- Cache database (SQLite) contains order data — **excluded from git** via `.gitignore`
- No data is sent to any third party

---

*Inspired by [Amazon-MoneyMoney](https://github.com/Michael-Beutling/Amazon-MoneyMoney)*
