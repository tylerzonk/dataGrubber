"""Log into learn.umgc.edu through Microsoft SSO and write cookies.json.

Credentials come from secrets_store (keyring / Windows Credential Manager /
DPAPI) — prompted in the terminal on first run. Requires playwright:

    pip install playwright
    playwright install chromium

UMGC's MFA texts an SMS code and then trusts the browser for ~a month, so
this script uses a persistent Chromium profile (.pw-profile/): run headful
once a month to enter the code, and every other run reuses the trusted
profile headlessly with no MFA and usually no login page at all.

Microsoft's login shows different pages depending on session state (email
form, account picker, prefilled password, "stay signed in?", MFA), in no
fixed order. So the login is driven as a state machine: one loop that
reacts to whatever page is showing, acts at most twice per step, and
polls for the D2L session cookies that mean we're done — which also lets
a human take over at any point in headful mode.

Usage:
    python refresh_cookies.py --headful  # first run of the month: enter the
                                         # SMS code, tick "don't ask again"
    python refresh_cookies.py            # rest of the month: silent/headless
"""

import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

import secrets_store

BASE = "https://learn.umgc.edu"
DEBUG_DIR = Path("debug")
PROFILE_DIR = Path(".pw-profile")  # holds the month-long MFA trust; gitignored

MFA_MARKERS = ("Approve sign in", "Verify your identity", "Enter code",
               "texted", "authenticator")


def dump(page, label):
    """Save a screenshot + HTML of the current page for post-mortem."""
    DEBUG_DIR.mkdir(exist_ok=True)
    page.screenshot(path=str(DEBUG_DIR / f"{label}.png"), full_page=True)
    (DEBUG_DIR / f"{label}.html").write_text(page.content(), encoding="utf-8")
    print(f"  [debug] saved debug/{label}.png and .html (url: {page.url})")


def refresh(headful=False):
    """Attempt a login and write cookies.json.

    Returns (status, detail): status is one of
      "ok", "bad_password", "mfa_required", "stuck".

    Never raises for login weirdness — an unexpected page or timeout comes
    back as ("stuck", detail) so the pipeline can retry headful.

    Credentials are fetched only if a login form actually appears — with a
    still-trusted .pw-profile the re-login is silent and needs no
    credentials at all (important for cron on a server).
    """
    try:
        return _refresh(headful)
    except Exception as e:
        return "stuck", f"{type(e).__name__}: {e}"


def _refresh(headful):
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=not headful
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(f"{BASE}/d2l/login", wait_until="domcontentloaded")

            wanted = {"d2lSessionVal", "d2lSecureSessionVal"}
            deadline = time.time() + (300 if headful else 90)
            restart_at = time.time() + 30
            cookies, creds = {}, {}
            acted = {}           # state -> times we acted on it (cap 2)
            submitted_pw = False
            restarted = False

            def cred():
                if not creds:
                    creds["u"], creds["p"] = secrets_store.credentials()
                return creds["u"], creds["p"]

            def visible(sel):
                try:
                    return page.locator(sel).first.is_visible()
                except Exception:
                    return False

            def body_text():
                try:
                    return page.inner_text("body", timeout=2000)
                except Exception:
                    return ""

            while time.time() < deadline:
                try:
                    got = {c["name"]: c["value"] for c in ctx.cookies(BASE)
                           if c["name"] in wanted}
                    if wanted <= set(got):
                        cookies = got
                        print("session cookies captured")
                        break
                    if not ctx.pages:
                        break  # browser window closed by hand
                    page = ctx.pages[0]

                    state = None
                    if "login.microsoftonline.com" in page.url:
                        if visible('input[type="password"]'):
                            state = "password"
                        elif visible('input[type="email"]'):
                            state = "email"
                        elif visible("div[data-test-id]"):
                            state = "picker"
                        elif visible("#idSIButton9"):
                            state = "kmsi"
                        elif any(s in body_text() for s in MFA_MARKERS):
                            state = "mfa"

                    # a password page reappearing after a submit usually
                    # means Microsoft rejected the password
                    if state == "password" and submitted_pw:
                        low = body_text().lower()
                        if "incorrect" in low or "account or password" in low:
                            dump(page, "bad-credentials")
                            return "bad_password", None

                    if state and acted.get(state, 0) < 2:
                        acted[state] = acted.get(state, 0) + 1
                        if state == "email":
                            print("step: email page")
                            page.fill('input[type="email"]', cred()[0])
                            page.click('input[type="submit"]')
                        elif state == "picker":
                            print("step: account picker")
                            tile = page.locator(
                                f'div[data-test-id="{cred()[0]}"]')
                            (tile.first if tile.count()
                             else page.locator("div[data-test-id]").first
                             ).click()
                        elif state == "password":
                            print("step: password page")
                            page.fill('input[type="password"]', cred()[1])
                            page.click('input[type="submit"]')
                            submitted_pw = True
                        elif state == "kmsi":
                            print("step: 'stay signed in' -> yes")
                            page.click("#idSIButton9")
                        elif state == "mfa":
                            if not headful:
                                dump(page, "mfa-prompt")
                                return "mfa_required", None
                            print("MFA prompt — complete the SMS code in "
                                  "the browser window (tick \"don't ask "
                                  "again for 30 days\").")
                    elif (state and not restarted
                          and time.time() > restart_at):
                        # Same page keeps re-rendering without progress
                        # (headless saml2 sso_reload loops do this). The MS
                        # session is usually established by now, so restart
                        # the SAML handshake from the D2L side once.
                        print("  login not progressing -> restarting the "
                              "D2L login redirect")
                        page.goto(f"{BASE}/d2l/login",
                                  wait_until="domcontentloaded")
                        acted.clear()
                        restarted = True
                except Exception:
                    if not ctx.pages:
                        break
                time.sleep(0.5)

            if wanted - set(cookies):
                pg = ctx.pages[0] if ctx.pages else None
                if pg:
                    try:
                        dump(pg, "stuck")
                    except Exception:
                        pass
                return "stuck", (pg.url if pg else "browser window closed "
                                 "before the session cookies were captured")
        finally:
            ctx.close()

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
    "stuck": "Login never produced D2L session cookies — check "
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
