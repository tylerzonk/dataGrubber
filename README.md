# dataGrubber

Archives each week's UMGC D2L (Brightspace) course material to local folders:

```
output/
  ARIN 440/
    Content/                     the course content area, mirrored in its
                                 own nested module structure (Module 1/
                                 Week 1/...); pages as markdown, files
                                 as-is, links.md for external resources,
                                 toc.json for the raw tree
    Activities and Assessments/
      Week 5/
        assignments/             <name>/assignment.md + rubric + attachments
        quizzes/                 <name>.md + raw.json
        discussions/             <name>.md + raw.json
      General/                   items no week links to (Introductions, ...)
    Class Data/                  semester-wide views
      Grades/                    Grades.md: points, % of the 1000-point
                                 course, your scores, earned/ungraded totals
      Announcements/             <date> <title>.md (week noted inside)
      Calendar.md                every due date from the activities
                                 themselves, merged with the course
                                 calendar and deduped
  ARIN 460/
    ...
```

Content deliberately keeps the course's own nesting instead of being split
by week — module 1 material stays browsable while you work later weeks.
Every assignment/quiz/discussion file lists its points and its share of
the final grade next to its due date.

## Which classes get grabbed

`config.json` is the only thing you touch between semesters:

```json
{
  "courses": ["ARIN 440", "ARIN 460"]
}
```

One entry per class; any fragment of the course's name or code works
("ARIN 440", "DATA 300", ...). Each entry is matched against your live
D2L enrollments at run time, so nothing else is hardcoded. If a code
matches offerings from more than one semester (a retake, an old
enrollment still listed), the currently accessible / newest one wins.

New semester = replace the `courses` list and run `python pipeline.py`.
Old course folders in `output/` are left untouched.

Each course's week-1 Wednesday (which drives the week numbers on
announcements and Calendar.md) is derived per course from your
enrollment's start date — UMGC weeks run Wed→Tue, so the start date is
snapped forward to the next Wednesday. That also handles 8-week and 16-week sessions
starting on different dates. An optional `"week1_start": "YYYY-MM-DD"` in
config overrides the derivation for every course if it ever guesses wrong
(the run prints the date it used per course).

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
  (instructions, rubric, attachments), quizzes from `/quizzes/`, the
  gradebook from `/grades/` + `/grades/categories/` +
  `/grades/values/myGradeValues/`.
- Every UMGC course is graded out of 1000 points, so an item's share of
  the final grade is its points / 1000 (a 60-point assignment is 6%) —
  immune to the gradebook only showing released items early in the term.
  If a course ever used weighted categories instead, D2L's own weight
  numbers win. Override the total with `"course_total_points"` in config.
- Calendar.md trusts the activities' own due dates first (teachers forget
  to put things in the calendar); calendar-only events are merged in and
  duplicates dropped.
- The archiver rebuilds a course folder from scratch when the output
  layout version changes; after such a rebuild, delete the course's old
  folders from the Obsidian vault once (publish never deletes).
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
