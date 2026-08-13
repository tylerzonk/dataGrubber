"""Central pipeline: check every prerequisite, fix what breaks, then archive.

    python pipeline.py             # full incremental archive
    python pipeline.py --week 3    # just week 3

Stages and their self-repairs:
  1. config  -> creates config.json from the example if missing
  2. session -> if the cookies are stale, refreshes them via browser login,
     which itself repairs whatever it hits:
        - credentials missing     -> prompts in the terminal, saves to
                                     keyring/DPAPI for next time
        - playwright missing      -> installs it (pip + chromium)
        - MFA trust expired       -> reopens headful so you can enter the
                                     SMS code (monthly)
        - password rotated        -> prompts for the new one, saves it,
                                     and retries
  3. archive -> incremental grab (new / updated / verified per course)
  4. publish -> mirrors the archive into the Obsidian vault
     (config "publish_dir"); only new/updated files are copied, and a
     file you edited in the vault is never overwritten unless the source
     item itself changed after your edit.

Only steps that genuinely need you (SMS code, new password) are
interactive; everything else repairs itself.
"""

import argparse
import getpass
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import secrets_store


def stage(n, label):
    print(f"\n[{n}/4] {label}")


def check_config():
    if not Path("config.json").exists():
        shutil.copy("config.example.json", "config.json")
        print("  config.json was missing -> created from config.example.json "
              "(review the course list once)")
    else:
        print("  config.json ok")


def make_client():
    """A logged-in D2LClient, or None if the session cookies don't work."""
    from d2l_client import D2LClient
    try:
        client = D2LClient()
        client.login()
        return client
    except Exception as e:
        print(f"  session check failed: {e}")
        return None


def ensure_playwright():
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        print("  playwright missing -> installing (one-time, includes a "
              "Chromium download)")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright"])
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])


def fix_session():
    ensure_playwright()
    import refresh_cookies

    headful = False
    for attempt in range(4):
        status, detail = refresh_cookies.refresh(headful=headful)
        if status == "ok":
            return
        if status == "bad_password":
            print("  Microsoft rejected the stored password — if it was "
                  "rotated, enter the new one.")
            secrets_store.set_secret(
                "password", getpass.getpass("  New UMGC password: "))
        elif status == "mfa_required":
            print("  MFA trust expired (monthly) -> opening a visible "
                  "browser; enter the SMS code and tick \"don't ask again "
                  "for 30 days\".")
            headful = True
        else:  # stuck
            if headful:
                raise SystemExit(f"  Login still stuck ({detail}) — see "
                                 "debug/stuck.png.")
            print(f"  login stuck headless ({detail}) -> retrying headful "
                  "so you can see it")
            headful = True
    raise SystemExit("  Could not repair the session after several attempts.")


def resolve_publish_dir(cfg):
    """publish_dir from config, translated for WSL if it is a Windows path."""
    raw = cfg.get("publish_dir")
    if not raw:
        return None
    if os.name != "nt" and re.match(r"^[A-Za-z]:[/\\]", raw):
        return Path(f"/mnt/{raw[0].lower()}/{raw[2:].replace(chr(92), '/').lstrip('/')}")
    return Path(raw)


def publish(cfg):
    dest_root = resolve_publish_dir(cfg)
    if dest_root is None:
        print("  no publish_dir configured -> skipping")
        return
    if not dest_root.parent.exists():
        # e.g. running on a server where the Obsidian vault doesn't exist
        print(f"  publish_dir parent missing ({dest_root.parent}) -> skipping")
        return
    out_root = Path(cfg.get("output_dir", "output"))
    copied = skipped = 0
    for course in cfg["courses"]:
        src_dir = out_root / course
        # vault convention has no spaces in course folder names (ARIN440)
        dst_dir = dest_root / course.replace(" ", "")
        for src in sorted(src_dir.rglob("*")):
            if src.is_dir() or src.name == ".manifest.json":
                continue
            dst = dst_dir / src.relative_to(src_dir)
            # copy when new, or when the source item changed after the vault
            # copy (a vault file you edited more recently is left alone);
            # a re-render that produced identical bytes is not a change
            if dst.exists() and (
                    src.stat().st_mtime <= dst.stat().st_mtime + 1
                    or src.read_bytes() == dst.read_bytes()):
                skipped += 1
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)  # copy2 keeps mtime for future compares
            copied += 1
            print(f"  -> {dst_dir.name}/{src.relative_to(src_dir)}")
    print(f"  published {copied} file(s), {skipped} already current, "
          f"into {dest_root}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int, help="only this week (default: everything)")
    args = ap.parse_args()

    stage(1, "config")
    check_config()

    stage(2, "session")
    client = make_client()
    if client is None:
        fix_session()
        client = make_client()
        if client is None:
            raise SystemExit("  Session still invalid after a successful "
                             "login — something changed on D2L's side.")
    print("  session ok")

    stage(3, "archive")
    import grab_week
    grab_week.run(week=args.week, client=client)

    stage(4, "publish")
    publish(client.cfg)


if __name__ == "__main__":
    main()
