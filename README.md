# dataGrubber

Archives each week's UMGC D2L (Brightspace) course material to local folders:

```
output/
  ARIN 440/
    Week 5/
      course_content/   topic files, pages as markdown, links.md
      assignments/      <name>/assignment.md + rubric + attachments + raw.json
      quizzes/          <name>.md + raw.json
  ARIN 460/
    ...
```

## Setup (venv already created by Claude; recreate with virtualenv if needed)

```bash
python3 -m virtualenv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.json config.json
```

Credentials: the first script that needs them (e.g. `refresh_cookies.py`)
prompts for username/password right in the terminal and saves them — to the
system keyring where one exists, or (on WSL, automatically) encrypted with
Windows DPAPI under `%USERPROFILE%\.dataGrubber\`. Later runs are silent.
When your password rotates (every 3 months), rerun
`.venv/bin/python secrets_store.py` to overwrite the stored values.

## Getting session cookies (needed because UMGC uses Microsoft SSO)

Automatic (preferred — one-time Playwright setup):

```bash
pip install playwright && playwright install chromium
python refresh_cookies.py --headful   # first run each month: enter the SMS
                                      # MFA code, tick "don't ask again"
python refresh_cookies.py             # all other runs: silent, headless
```

The script keeps a persistent Chromium profile in `.pw-profile/` that holds
UMGC's month-long MFA trust — so the SMS code is a once-a-month ritual, not
a per-run one.

Manual fallback: log into https://learn.umgc.edu in Chrome, F12 →
Application → Cookies → copy `d2lSessionVal` and `d2lSecureSessionVal`
into `cookies.json` (see cookies.example.json).

Sessions use sliding expiry — cookies stay alive while used regularly, and
need refreshing after a real logout/timeout.

## Run

One command does everything — checks each prerequisite, repairs what it
can (stale cookies, expired MFA trust, rotated password, missing
playwright), then archives incrementally:

```bash
python pipeline.py             # everything, incrementally
python pipeline.py --week 3    # just week 3
```

The final stage publishes the archive into the Obsidian vault
(`publish_dir` in config, course folders without spaces: `ARIN440`).
Only new/updated files are copied; a file you edited in the vault stays
untouched unless the underlying course item changed after your edit.

Only two things ever need you: the monthly SMS MFA code (a browser window
opens for it) and typing a new password after a rotation. The underlying
scripts still run standalone: `grab_week.py`, `refresh_cookies.py`,
`secrets_store.py`.

## How it works

- The D2L web UI is backed by a REST API at `/d2l/api/` (`le` and `lp`
  products). With valid session cookies, plain GETs return JSON.
- Course content comes from `/content/toc`, files from
  `/content/topics/{id}/file`, assignments from `/dropbox/folders/`
  (instructions, rubric, attachments), quizzes from `/quizzes/`.
- External resources (publisher e-books, videos hosted off-D2L) are recorded
  in `course_content/links.md` rather than downloaded.

`cookies.json` and `config.json` are gitignored — never commit them.

## Running on a server (daily cron)

```bash
git clone git@github.com:tylerzonk/dataGrubber.git && cd dataGrubber
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt playwright
.venv/bin/playwright install --with-deps chromium
```

The MFA SMS code needs a visible browser, which a headless server doesn't
have. The clean pattern: do the monthly `python refresh_cookies.py
--headful` on your desktop, then copy `.pw-profile/` (and `cookies.json`)
to the server — the month of MFA trust travels with the profile, and
headless re-logins on the server then need no credentials at all. If the
trust expires server-side, the pipeline fails fast with `mfa_required` in
the log; refresh on the desktop and re-copy.

Cron (daily at 06:00):

```
0 6 * * * cd $HOME/dataGrubber && .venv/bin/python pipeline.py >> grabber.log 2>&1
```

The publish stage skips itself automatically on machines where the
Obsidian vault path doesn't exist; per-machine `config.json` (gitignored,
auto-created from the example) can also point `publish_dir` somewhere
else, or the server can just serve `output/`.
