"""Log into learn.umgc.edu through Microsoft SSO and write cookies.json.

Credentials come from secrets_store (keyring / Windows Credential Manager /
DPAPI) — prompted in the terminal on first run. Requires playwright:

    pip install playwright
    playwright install chromium

UMGC's MFA texts an SMS code and then trusts the browser for ~a month, so
this script uses a persistent Chromium profile (.pw-profile/): run headful
once a month to enter the code, and every other run reuses the trusted
profile headlessly with no MFA and usually no login page at all.

Usage:
    python refresh_cookies.py --headful  # first run of the month: enter the
                                         # SMS code, tick "don't ask again"
    python refresh_cookies.py            # rest of the month: silent/headless
"""

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import secrets_store

BASE = "https://learn.umgc.edu"
DEBUG_DIR = Path("debug")
PROFILE_DIR = Path(".pw-profile")  # holds the month-long MFA trust; gitignored


def dump(page, label):
    """Save a screenshot + HTML of the current page for post-mortem."""
    DEBUG_DIR.mkdir(exist_ok=True)
    page.screenshot(path=str(DEBUG_DIR / f"{label}.png"), full_page=True)
    (DEBUG_DIR / f"{label}.html").write_text(page.content(), encoding="utf-8")
    print(f"  [debug] saved debug/{label}.png and .html (url: {page.url})")


def refresh(headful=False):
    """Attempt a login and write cookies.json.

    Returns (status, detail): status is one of
      "ok", "bad_password", "mfa_required", "no_password_box", "stuck".

    Credentials are fetched only if the Microsoft login page actually
    appears — with a still-trusted .pw-profile the re-login is silent and
    needs no credentials at all (important for cron on a server).
    """
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=not headful
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(f"{BASE}/d2l/login", wait_until="domcontentloaded")
            page.wait_for_timeout(2000)  # let any silent SSO redirect settle

            if "login.microsoftonline.com" in page.url:
                print("step 1: Microsoft email page")
                username, password = secrets_store.credentials()
                page.fill('input[type="email"]', username)
                page.click('input[type="submit"]')
                try:
                    page.wait_for_selector('input[type="password"]', timeout=15000)
                except PWTimeout:
                    dump(page, "no-password-box")
                    return "no_password_box", page.url
                print("step 2: password page")
                page.fill('input[type="password"]', password)
                page.click('input[type="submit"]')
                page.wait_for_timeout(3000)

                body = page.inner_text("body")
                if "incorrect" in body.lower() or "account or password" in body.lower():
                    dump(page, "bad-credentials")
                    return "bad_password", None
                if any(s in body for s in ("Approve sign in", "Verify your identity",
                                           "Enter code", "texted", "authenticator")):
                    if not headful:
                        dump(page, "mfa-prompt")
                        return "mfa_required", None
                    print("step 2b: MFA prompt — complete the SMS code in the "
                          "browser window (tick \"don't ask again for 30 "
                          "days\"). Waiting up to 5 minutes...")

                # "Stay signed in?" prompt — say yes for a longer session.
                # (After manual MFA it may appear late; in headful mode just
                # click Yes yourself if the script already moved on.)
                try:
                    page.wait_for_selector("#idSIButton9", timeout=10000)
                    print("step 3: 'stay signed in' -> yes")
                    page.click("#idSIButton9")
                except PWTimeout:
                    pass

            try:
                page.wait_for_url(f"{BASE}/**",
                                  timeout=300000 if headful else 30000)
                print("step 4: back on learn.umgc.edu")
            except PWTimeout:
                dump(page, "stuck")
                return "stuck", page.url

            wanted = {"d2lSessionVal", "d2lSecureSessionVal"}
            cookies = {
                c["name"]: c["value"]
                for c in ctx.cookies(BASE)
                if c["name"] in wanted
            }
        finally:
            ctx.close()

    if wanted - set(cookies):
        return "stuck", f"missing cookies {wanted - set(cookies)}"
    with open("cookies.json", "w") as f:
        json.dump(cookies, f, indent=2)
    print("cookies.json refreshed.")
    return "ok", None


MESSAGES = {
    "bad_password": "Microsoft says the password is incorrect. "
                    "Rerun `python secrets_store.py` to fix it.",
    "mfa_required": "MFA prompt detected in headless mode. Rerun with "
                    "--headful, enter the SMS code, and tick \"don't ask "
                    "again for 30 days\".",
    "no_password_box": "Password box never appeared after submitting the "
                       "email — check debug/no-password-box.png (username "
                       "usually must be the full email address).",
    "stuck": "Login never landed back on learn.umgc.edu — check "
             "debug/stuck.png.",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headful", action="store_true")
    args = ap.parse_args()
    status, detail = refresh(headful=args.headful)
    if status != "ok":
        sys.exit(MESSAGES[status] + (f" ({detail})" if detail else ""))


if __name__ == "__main__":
    main()
