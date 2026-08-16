"""Incrementally archive UMGC D2L courses into a local tree.

    output/<Course>/
        Content/                     the course content area, mirrored in
                                     its own nested module structure
                                     (Module 1/Week 1/...); pages as
                                     markdown, files as-is, links.md for
                                     external resources, toc.json raw tree
        Activities and Assessments/
            Week N/
                assignments/<name>/  assignment.md + rubric + attachments
                quizzes/             <name>.md + raw.json
                discussions/         <name>.md + raw.json
            General/                 items no week module links to
                                     (Introductions, Ask the Instructor...)
        Class Data/                  semester-wide views
            Grades/                  Grades.md: every graded item's points,
                                     % of the 1000-point course, your score
            Announcements/           <date> <title>.md (week noted inside)
            Calendar.md              every due date, taken from the
                                     activities themselves and merged with
                                     the course calendar (deduped)

Week membership is decided by the content area: each "Week N" / "Unit N"
module's quickLinks claim their assignment/quiz/discussion. Anything no
week module links to lands in General. Content itself is NOT split by
week — it keeps the course's own module nesting, so material from earlier
modules stays browsable while working later weeks.

Each course keeps output/<Course>/.manifest.json mapping every item to a
content fingerprint + file paths: re-runs skip verified items, fetch new
ones, and re-save changed ones (e.g. an edited announcement). When the
output layout changes between versions, the course folder is rebuilt.

Usage:
    python grab_week.py              # everything, incrementally
    python grab_week.py --week 3     # just week 3's content
"""

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
from pathlib import Path

import html2text

from d2l_client import D2LClient

WEEK_PAT = re.compile(r"\b(?:week|unit)\s*0?(\d+)\b", re.I)
ANCHOR_PAT = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)

# linked resources are only grabbed from inside the UMGC environment;
# external links (publishers, YouTube, umgc.edu marketing pages) stay links
UMGC_HOSTS = ("learn.umgc.edu", "leocontent.umgc.edu")
# document types worth pulling from a linked page's own links (depth 1)
ASSET_EXTS = (".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
              ".csv", ".txt", ".rtf", ".zip", ".ipynb", ".py", ".r",
              ".sql", ".json")
LAYOUT = 2  # bump when the output tree changes shape -> forces a rebuild

# UMGC runs on US Eastern time: a "Tue 11:59 PM" due date is stored as
# Wed 03:59/04:59 UTC, so week math must happen in local time or every
# deadline lands one week late.
try:
    from zoneinfo import ZoneInfo
    EASTERN = ZoneInfo("America/New_York")
except Exception:  # no tzdata (bare Windows python): fixed offset is
    EASTERN = dt.timezone(dt.timedelta(hours=-5))  # date-accurate enough


def to_md(html, baseurl=""):
    h = html2text.HTML2Text(baseurl=baseurl)
    h.body_width = 0
    return h.handle(html or "").strip()


def relink(md, url_map):
    """Point markdown links whose targets were saved locally (by
    save_linked_pages) at the local copies instead of the web."""
    for url, local in url_map.items():
        md = md.replace(f"]({url})", f"]({local})")
    return md


def safe(name):
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip()


def parse_d2l_date(s):
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# live counters that tick as classmates submit/post; they never affect the
# rendered files, so changes to them must not count as "item updated".
# IsRead/CanRate are per-viewer state on discussion posts: they flip when
# YOU read a post, which must not look like the post changed.
VOLATILE = {"TotalFiles", "TotalUsers", "TotalUsersWithFeedback",
            "TotalUsersWithSubmissions", "UnreadFiles", "FlaggedFiles",
            "RatingsCount", "RatingsSum", "ScoredCount",
            "UnapprovedPostCount", "PinnedPostCount", "LastPostDate",
            "NumThreads", "NumPosts", "LastAccessed", "IsRead", "CanRate"}


def fingerprint(obj):
    def strip(o):
        if isinstance(o, dict):
            return {k: strip(v) for k, v in o.items() if k not in VOLATILE}
        if isinstance(o, list):
            return [strip(v) for v in o]
        return o
    return hashlib.sha1(
        json.dumps(strip(obj), sort_keys=True, default=str).encode()
    ).hexdigest()


def norm(name):
    return re.sub(r"\s+", " ", name).strip().lower()


def canon(title):
    """Normalize a calendar-event title for deduping against activity
    names ("Assignment 1 - Due" should match "Assignment 1")."""
    return re.sub(r"\s*[-–:]*\s*(due( date)?|ends?|closes?|"
                  r"availability ends)\s*$", "", norm(title))


def cell(x):
    return str(x).replace("|", "\\|")


def grade_slim(gi):
    """The grade fields shown inside item files. Scores stay out, so a
    newly posted grade rewrites Grades.md but not every assignment.md."""
    if not gi:
        return None
    return {k: gi[k] for k in ("points", "weight", "category", "bonus", "excluded")}


class CourseArchiver:
    def __init__(self, client, label, org_unit, out_root, week1, only_week=None):
        self.c = client
        self.label = label
        self.ou = org_unit
        self.only_week = only_week
        self.week1 = week1  # date of this course's week-1 Wednesday, or None
        self.dir = out_root / safe(label)
        self.content_dir = self.dir / "Content"
        self.aa_dir = self.dir / "Activities and Assessments"
        self.cd_dir = self.dir / "Class Data"
        self.manifest_path = self.dir / ".manifest.json"
        self.manifest = (
            json.loads(self.manifest_path.read_text())
            if self.manifest_path.exists() else {}
        )
        if self.manifest and self.manifest.get("_layout") != LAYOUT:
            print("  output layout changed -> rebuilding this course from scratch")
            shutil.rmtree(self.dir, ignore_errors=True)
            self.manifest = {}
        self.manifest["_layout"] = LAYOUT
        self.stats = {"new": 0, "updated": 0, "verified": 0, "failed": 0}
        self.links = {}       # dest dir -> list of markdown link lines
        # kind -> {activity id: week number or None}; filled from quickLinks
        self.activity_week = {"dropbox": {}, "quiz": {}, "discussion": {}}

    # ---------- manifest ----------

    def fresh(self, key, fp):
        """True if this item is unchanged and all its files still exist."""
        ent = self.manifest.get(key)
        if ent and ent["fp"] == fp and all(Path(p).exists() for p in ent["paths"]):
            self.stats["verified"] += 1
            return True
        return False

    def record(self, key, fp, paths):
        self.stats["updated" if key in self.manifest else "new"] += 1
        self.manifest[key] = {"fp": fp, "paths": [str(p) for p in paths]}

    # ---------- weeks ----------

    def week_for(self, when):
        """Week number a datetime falls in (UMGC weeks run Wed->Tue)."""
        if not (self.week1 and when):
            return None
        days = (when.astimezone(EASTERN).date() - self.week1).days
        return days // 7 + 1 if days >= 0 else None

    # ---------- gradebook ----------

    def load_grades(self):
        """Fetch the gradebook and precompute each item's share of the
        final grade, for Grades.md and the per-item metadata lines."""
        self.grades, self.grades_by_name, self.gradebook = {}, {}, None
        # UMGC standard is 1000 points per course, but an instructor can
        # deviate (ARIN 440 fall 2026 grades out of 100); the config value
        # may be a number for all courses or a {course label: total} map
        course_total = self.c.cfg.get("course_total_points", 1000)
        if isinstance(course_total, dict):
            course_total = course_total.get(self.label, 1000)
        try:
            objects = [g for g in self.c.grade_objects(self.ou)
                       if g.get("GradeType") != "Category"]
        except Exception as e:
            print(f"  gradebook unavailable: {e}")
            return
        try:
            cats = self.c.grade_categories(self.ou)
        except Exception:
            cats = []
        try:
            values = self.c.my_grade_values(self.ou)
        except Exception:
            values = []
        final = self.c.my_final_grade(self.ou)

        by_id = {}
        for v in values:
            try:
                by_id[int(v["GradeObjectIdentifier"])] = v
            except (KeyError, TypeError, ValueError):
                pass
        cat_of = {g["Id"]: cat for cat in cats for g in cat.get("Grades") or []}
        # every UMGC course is graded out of 1000 points, so an item's share
        # of the final grade is its points / 1000 — immune to the gradebook
        # only showing released items early in the term. D2L's own weighted
        # numbers still win if a course ever uses weighted categories.
        weighted = (any(v.get("WeightedDenominator") is not None
                        for v in by_id.values())
                    or any(c.get("Weight") for c in cats))
        visible = sum((g.get("MaxPoints") or 0) for g in objects
                      if not g.get("IsBonus")
                      and not g.get("ExcludeFromFinalGradeCalculation"))

        for g in objects:
            val = by_id.get(g["Id"])
            cat = cat_of.get(g["Id"])
            counted = (not g.get("IsBonus")
                       and not g.get("ExcludeFromFinalGradeCalculation"))
            weight = None
            if val and val.get("WeightedDenominator") is not None:
                weight = val["WeightedDenominator"]
            elif weighted:
                w = g.get("Weight")  # % of its category (or of the final)
                cw = (cat or {}).get("Weight")
                if w is not None:
                    weight = w * cw / 100.0 if cw is not None else w
            elif counted and g.get("MaxPoints"):
                weight = 100.0 * g["MaxPoints"] / course_total
            info = {
                "id": g["Id"], "name": g.get("Name"),
                "points": g.get("MaxPoints"), "weight": weight,
                "category": (cat or {}).get("Name"),
                "bonus": bool(g.get("IsBonus")),
                "excluded": bool(g.get("ExcludeFromFinalGradeCalculation")),
                "counted": counted,
                "value": val,
            }
            self.grades[g["Id"]] = info
            if g.get("Name"):
                self.grades_by_name.setdefault(norm(g["Name"]), info)
        self.gradebook = {"objects": objects, "categories": cats,
                          "values": values, "final": final,
                          "weighted": weighted, "visible_points": visible,
                          "course_total": course_total}

    def grade_for(self, grade_item_id, name):
        """Gradebook info for an activity: by its GradeItemId when the API
        exposes one (assignments, quizzes), else by name (discussions)."""
        if grade_item_id and grade_item_id in self.grades:
            return self.grades[grade_item_id]
        if name:
            return self.grades_by_name.get(norm(name))
        return None

    def grade_lines(self, gi, have_points=False):
        if not gi:
            return []
        lines = []
        if gi["points"] and not have_points:
            lines.append(f"- Points: {gi['points']:g}")
        if gi["weight"] is not None:
            w = f"- Grade weight: {gi['weight']:.4g}% of final grade"
            if gi["category"]:
                w += f" ({gi['category']})"
            lines.append(w)
        if gi["bonus"]:
            lines.append("- Bonus item (extra credit)")
        if gi["excluded"]:
            lines.append("- Not counted toward the final grade")
        return lines

    def save_grades_summary(self):
        if not self.gradebook:
            return
        key, fp = "grades:summary", fingerprint(self.gradebook)
        if self.fresh(key, fp):
            return
        gb = self.gradebook
        ct = gb["course_total"]
        print("    grades summary")

        earned = graded = ungraded = 0
        for info in self.grades.values():
            if not info["counted"]:
                continue
            val = info["value"] or {}
            if val.get("PointsNumerator") is not None:
                earned += val["PointsNumerator"]
                graded += val.get("PointsDenominator") or info["points"] or 0
            elif info["points"]:
                ungraded += info["points"]

        lines = [f"# Grades — {self.dir.name}", "",
                 f"- Earned so far: {earned:g} / {ct:g}"]
        if graded:
            lines.append(f"- Graded so far: {graded:g} / {ct:g} "
                         f"(average {100 * earned / graded:.1f}%)")
        lines.append(f"- Not yet graded: {ungraded:g} / {ct:g}")
        shown = (gb.get("final") or {}).get("DisplayedGrade")
        if shown:
            lines.append(f"- Final grade so far: {shown}")
        if gb["weighted"]:
            lines.append("- Note: this course uses weighted grade "
                         "categories; weights come from D2L itself")
        elif gb["visible_points"] and gb["visible_points"] != ct:
            lines.append(f"- Note: visible grade items total "
                         f"{gb['visible_points']:g} of {ct:g} — some items "
                         "may not be released yet")
        lines += ["", "| Item | Category | Points | % of final | My grade |",
                  "|---|---|---|---|---|"]

        for g in gb["objects"]:
            info = self.grades.get(g["Id"]) or {}
            val = info.get("value") or {}
            pts = f"{info['points']:g}" if info.get("points") else ""
            w = (f"{info['weight']:.4g}%"
                 if info.get("weight") is not None else "")
            mine = val.get("DisplayedGrade") or ""
            if not mine and val.get("PointsNumerator") is not None:
                mine = (f"{val['PointsNumerator']:g}"
                        f"/{val.get('PointsDenominator') or 0:g}")
            flags = (" (bonus)" if info.get("bonus")
                     else " (not counted)" if info.get("excluded") else "")
            lines.append(f"| {cell(info.get('name') or '')}{flags} "
                         f"| {cell(info.get('category') or '')} "
                         f"| {pts} | {w} | {cell(mine)} |")

        gdir = self.cd_dir / "Grades"
        gdir.mkdir(parents=True, exist_ok=True)
        paths = [gdir / "Grades.md", gdir / "raw.json"]
        paths[0].write_text("\n".join(lines) + "\n", encoding="utf-8")
        paths[1].write_text(json.dumps(gb, indent=2), encoding="utf-8")
        self.record(key, fp, paths)

    # ---------- calendar ----------

    def save_calendar(self):
        """One chronological list of everything due: due dates from the
        activities themselves (authoritative — teachers forget to put
        things in the calendar), merged with calendar-only events."""
        entries = []  # [datetime, week, title, kind]
        for f in self.dropboxes.values():
            d = parse_d2l_date(f.get("DueDate"))
            if d:
                entries.append([d, self.week_for(d), f["Name"], "assignment"])
        for q in self.quizzes.values():
            d = parse_d2l_date(q.get("DueDate")) or parse_d2l_date(q.get("EndDate"))
            if d:
                entries.append([d, self.week_for(d), q["Name"], "quiz"])
        for _, t in self.discussions.values():
            d = parse_d2l_date(t.get("DueDate")) or parse_d2l_date(t.get("EndDate"))
            if d:
                entries.append([d, self.week_for(d), t["Name"], "discussion"])
        seen = {canon(e[2]) for e in entries}

        if self.week1:
            lo = dt.datetime.combine(self.week1 - dt.timedelta(days=14),
                                     dt.time.min, tzinfo=dt.timezone.utc)
            hi = lo + dt.timedelta(weeks=20)
            fmt = "%Y-%m-%dT%H:%M:%S.000Z"
            try:
                events = self.c.calendar_events(
                    self.ou, lo.strftime(fmt), hi.strftime(fmt))
            except Exception as e:
                print(f"  calendar unavailable: {e}")
                events = []
            for ev in events:
                title = ev.get("Title") or ""
                ct = canon(title)
                if any(ct in s or s in ct for s in seen):
                    continue
                d = parse_d2l_date(ev.get("EndDateTime")
                                   or ev.get("StartDateTime"))
                if d:
                    entries.append([d, self.week_for(d), title,
                                    "calendar event"])
                    seen.add(ct)

        if not entries:
            return
        entries.sort(key=lambda e: e[0])
        key, fp = "calendar:summary", fingerprint(entries)
        if self.fresh(key, fp):
            return
        print("    calendar")
        lines = [f"# Calendar — {self.dir.name}", "",
                 "| Due | Week | What | Type |", "|---|---|---|---|"]
        for d, wk, title, kind in entries:
            lines.append(f"| {d:%a %Y-%m-%d %H:%M %Z} | {wk or ''} "
                         f"| {cell(title)} | {kind} |")
        self.cd_dir.mkdir(parents=True, exist_ok=True)
        path = self.cd_dir / "Calendar.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.record(key, fp, [path])

    # ---------- per-item savers ----------

    def save_linked_pages(self, html, dest, prefix="", base_url=None,
                          assets_only=False):
        """Teachers link out to resources that live inside the UMGC
        environment: LTI quickLinks and leocontent.umgc.edu pages carrying
        the real instructions, plus documents (pdf/docx/ipynb/...) on
        leocontent or learn. Fetch each such link and save it beside the
        item — pages as markdown, documents as files. A fetched page gets
        one more pass for its own document links (assets_only), so an
        instructions page's PDFs come along without crawling site nav.
        Returns (md note lines, saved paths, {url: local name} for relink).
        """
        import html as htmllib
        from urllib.parse import urljoin, urlparse, quote, unquote
        notes, paths, seen, url_map = [], [], set(), {}
        for href, text in ANCHOR_PAT.findall(html or ""):
            url = urljoin(base_url or self.c.base + "/",
                          htmllib.unescape(href))
            if url in seen:
                continue
            seen.add(url)
            if urlparse(url).netloc.lower() not in UMGC_HOSTS:
                continue
            low = url.lower()
            is_lti = "quicklink" in low and "type=lti" in low
            if "quicklink" in low and not is_lti:
                continue  # dropbox/quiz/discussion/content: archived already
            if "/discussions/topics/" in low or "/dropbox/" in low.split("?")[0]:
                continue  # direct links to activities we archive elsewhere
            if assets_only and not (not is_lti and
                                    low.split("?")[0].endswith(ASSET_EXTS)):
                continue  # depth 1: documents only, never follow more pages
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text)).strip()
            title = safe(title) or "Linked page"
            try:
                r = self.c.follow_lti(url) if is_lti else self.c.get_raw(url)
            except Exception as e:
                print(f"      linked resource {title} failed: {e}")
                self.stats["failed"] += 1
                notes.append(f"- Linked resource could not be fetched: "
                             f"{title} ({url})")
                continue
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype.startswith("image/"):
                continue  # decorative banners; images stay as inline links
            dest.mkdir(parents=True, exist_ok=True)
            if "html" in ctype:
                p = dest / f"{prefix}{title}.md"
                sub_notes, sub_paths, sub_map = ([], [], {}) if assets_only \
                    else self.save_linked_pages(r.text, dest, prefix,
                                                base_url=r.url,
                                                assets_only=True)
                p.write_text(
                    f"# {title}\n\nSource: {r.url}\n\n"
                    f"{relink(to_md(r.text, baseurl=r.url), sub_map)}\n"
                    + ("\n" + "\n".join(sub_notes) + "\n" if sub_notes else ""),
                    encoding="utf-8")
                notes.append(f"- Saved linked page: [{title}]({quote(p.name)})")
                paths += [p] + sub_paths
            else:
                fname = safe(Path(unquote(urlparse(r.url).path)).name) or title
                p = dest / fname
                p.write_bytes(r.content)
                notes.append(f"- Saved linked file: [{title}]({quote(p.name)})")
                paths.append(p)
            # both the raw href and its absolute form can appear in the
            # rendered markdown, depending on whether to_md got a baseurl
            url_map[url] = url_map[htmllib.unescape(href)] = quote(p.name)
            print(f"      linked resource: {p.name[:70]}")
        return notes, paths, url_map

    def save_assignment(self, folder, dest):
        gi = self.grade_for(folder.get("GradeItemId"), folder.get("Name"))
        key, fp = f"dropbox:{folder['Id']}", fingerprint([folder, grade_slim(gi)])
        if self.fresh(key, fp):
            return
        adir = dest / safe(folder["Name"])
        adir.mkdir(parents=True, exist_ok=True)
        due = parse_d2l_date(folder.get("DueDate"))
        print(f"    assignment: {folder['Name'][:60]}")

        lines = [f"# {folder['Name']}", ""]
        if due:
            lines.append(f"- Due: {due:%A %Y-%m-%d %H:%M %Z}")
        pts = (folder.get("Assessment") or {}).get("ScoreDenominator")
        if pts:
            lines.append(f"- Points: {pts}")
        lines += self.grade_lines(gi, have_points=bool(pts))
        instr = (folder.get("CustomInstructions") or {}).get("Html", "")
        notes, linked, lmap = self.save_linked_pages(instr, adir)
        lines += ["", "## Instructions", "", relink(to_md(instr), lmap)]
        if notes:
            lines += [""] + notes
        for r in (folder.get("Assessment") or {}).get("Rubrics") or []:
            lines += ["", f"## Rubric: {r.get('Name', '')}", "",
                      "```json", json.dumps(r, indent=2), "```"]

        paths = [adir / "assignment.md", adir / "raw.json"] + linked
        paths[0].write_text("\n".join(lines) + "\n", encoding="utf-8")
        paths[1].write_text(json.dumps(folder, indent=2), encoding="utf-8")

        for att in folder.get("Attachments", []):
            p = adir / safe(att["FileName"])
            try:
                r = self.c.get_raw(
                    f"/d2l/api/le/{self.c.le_ver}/{self.ou}/dropbox/folders/"
                    f"{folder['Id']}/attachments/{att['FileId']}"
                )
                p.write_bytes(r.content)
                paths.append(p)
            except Exception as e:
                print(f"      attachment {att.get('FileName')} failed: {e}")
                self.stats["failed"] += 1
        self.record(key, fp, paths)

    def save_quiz(self, q, dest):
        gi = self.grade_for(q.get("GradeItemId"), q.get("Name"))
        key, fp = f"quiz:{q['QuizId']}", fingerprint([q, grade_slim(gi)])
        if self.fresh(key, fp):
            return
        dest.mkdir(parents=True, exist_ok=True)
        print(f"    quiz: {q['Name'][:60]}")
        lines = [f"# {q['Name']}", ""]
        for label, k in [("Due", "DueDate"), ("Start", "StartDate"), ("End", "EndDate")]:
            d = parse_d2l_date(q.get(k))
            if d:
                lines.append(f"- {label}: {d:%A %Y-%m-%d %H:%M %Z}")
        lines += self.grade_lines(gi)
        attempts = (q.get("AttemptsAllowed") or {}).get("NumberOfAttemptsAllowed")
        lines.append(f"- Attempts allowed: {attempts if attempts else 'unlimited'}")
        if q.get("TimeLimit"):
            lines.append(f"- Time limit: {q['TimeLimit'].get('TimeLimitValue')} min")
        desc = ((q.get("Description") or {}).get("Text") or {}).get("Html", "")
        instr = ((q.get("Instructions") or {}).get("Text") or {}).get("Html", "")
        name = safe(q["Name"])
        notes, linked, lmap = self.save_linked_pages(
            desc + instr, dest, prefix=f"{name} - ")
        if desc:
            lines += ["", "## Description", "", relink(to_md(desc), lmap)]
        if instr:
            lines += ["", "## Instructions", "", relink(to_md(instr), lmap)]
        if notes:
            lines += [""] + notes
        paths = [dest / f"{name}.md", dest / f"{name}.raw.json"] + linked
        paths[0].write_text("\n".join(lines) + "\n", encoding="utf-8")
        paths[1].write_text(json.dumps(q, indent=2), encoding="utf-8")
        self.record(key, fp, paths)

    def save_discussion(self, forum, topic, ddir):
        """The topic itself, as <Name>/<Name>.md (folder-note style: the
        folder also holds linked resources and discussion_posts/)."""
        gi = self.grade_for(topic.get("GradeItemId"), topic.get("Name"))
        key, fp = (f"discussion:{topic['TopicId']}",
                   fingerprint([topic, grade_slim(gi)]))
        if self.fresh(key, fp):
            return
        ddir.mkdir(parents=True, exist_ok=True)
        print(f"    discussion: {topic['Name'][:60]}")
        lines = [f"# {topic['Name']}", "", f"- Forum: {forum.get('Name', '')}"]
        for label, k in [("Due", "DueDate"), ("Start", "StartDate"), ("End", "EndDate")]:
            d = parse_d2l_date(topic.get(k))
            if d:
                lines.append(f"- {label}: {d:%A %Y-%m-%d %H:%M %Z}")
        if topic.get("ScoreOutOf"):
            lines.append(f"- Points: {topic['ScoreOutOf']}")
        lines += self.grade_lines(gi, have_points=bool(topic.get("ScoreOutOf")))
        if topic.get("MustPostToParticipate"):
            lines.append("- You must post before seeing others' posts")
        desc = (topic.get("Description") or {}).get("Html", "")
        name = safe(topic["Name"])
        notes, linked, lmap = self.save_linked_pages(desc, ddir)
        if desc:
            lines += ["", "## Prompt", "", relink(to_md(desc), lmap)]
        if notes:
            lines += [""] + notes
        lines += ["", f"[Open in D2L]({self.c.base}/d2l/le/{self.ou}"
                      f"/discussions/topics/{topic['TopicId']}/View)"]
        paths = [ddir / f"{name}.md", ddir / "raw.json"] + linked
        paths[0].write_text("\n".join(lines) + "\n", encoding="utf-8")
        paths[1].write_text(json.dumps(topic, indent=2), encoding="utf-8")
        self.record(key, fp, paths)

    def save_discussion_posts(self, forum, topic, ddir):
        """Everyone's posts for a topic — one markdown per top-level post
        with its replies nested inside, in <Name>/discussion_posts/, plus
        a _summary.md with the ranges (word counts, replies) that show
        what a typical post in this class looks like. Attachments are
        downloaded to discussion_posts/attachments/. Re-rendered whenever
        anyone posts or edits; your own reads don't count as changes."""
        from urllib.parse import quote
        try:
            posts = self.c.discussion_posts(
                self.ou, forum["ForumId"], topic["TopicId"])
        except Exception as e:
            print(f"    posts unavailable: {topic['Name'][:40]}: {e}")
            return
        posts = [p for p in posts if not p.get("IsDeleted")]
        if not posts:
            return
        key, fp = f"discussionposts:{topic['TopicId']}", fingerprint(posts)
        if self.fresh(key, fp):
            return
        print(f"    posts: {topic['Name'][:50]} ({len(posts)})")
        pdir = ddir / "discussion_posts"
        pdir.mkdir(parents=True, exist_ok=True)
        for old in pdir.glob("*.md"):  # thread files are renamed on edits
            old.unlink()

        me = self.c.my_user_id()
        by_id = {p["PostId"]: p for p in posts}
        children = {}
        for p in posts:
            children.setdefault(p.get("ParentPostId"), []).append(p)
        for v in children.values():
            v.sort(key=lambda p: p.get("DatePosted") or "")
        # top-level posts, plus orphans whose parent was deleted
        roots = children.get(None, []) + [
            p for p in posts
            if p.get("ParentPostId") and p["ParentPostId"] not in by_id]

        def author(p):
            if p.get("IsAnonymous"):
                return "Anonymous"
            nm = p.get("PostingUserDisplayName") or "Unknown"
            return f"{nm} (me)" if str(p.get("PostingUserId")) == me else nm

        def descendants(p):
            kids = children.get(p["PostId"], [])
            return len(kids) + sum(descendants(k) for k in kids)

        paths = []

        def grab_attachments(p, lines):
            for att in p.get("Attachments") or []:
                fn = safe(att.get("FileName") or "file")
                fpath = pdir / "attachments" / fn
                if fpath.exists() and fpath in paths:  # name clash between
                    fn = f"{p['PostId']} {fn}"        # different posts
                    fpath = pdir / "attachments" / fn
                try:
                    r = self.c.get_raw(
                        f"/d2l/api/le/{self.c.le_ver}/{self.ou}/discussions/"
                        f"forums/{forum['ForumId']}/topics/{topic['TopicId']}"
                        f"/posts/{p['PostId']}/attachments/{att['FileId']}")
                    fpath.parent.mkdir(parents=True, exist_ok=True)
                    fpath.write_bytes(r.content)
                    paths.append(fpath)
                    lines.append(f"- Attachment: [{fn}](attachments/{quote(fn)})")
                except Exception as e:
                    print(f"      attachment {fn} failed: {e}")
                    self.stats["failed"] += 1
                    lines.append(f"- Attachment (fetch failed): {fn}")

        used, rows = set(), []
        for top in roots:
            date = parse_d2l_date(top.get("DatePosted"))
            when = f"{date:%Y-%m-%d} " if date else ""
            stem = safe(when + (top.get("Subject") or author(top)))[:70]
            fname, n = f"{stem}.md", 2
            while fname in used:
                fname, n = f"{stem} {n}.md", n + 1
            used.add(fname)
            reps = descendants(top)
            lines = [f"# {top.get('Subject') or 'Post'}", "",
                     f"- Author: {author(top)}"]
            if date:
                lines.append(f"- Posted: {date:%A %Y-%m-%d %H:%M %Z}")
            lines.append(f"- Words: {top.get('WordCount')}")
            lines.append(f"- Replies: {reps}")
            grab_attachments(top, lines)
            lines += ["", to_md((top.get("Message") or {}).get("Html", ""))]

            def walk(pid, depth):
                for ch in children.get(pid, []):
                    d = parse_d2l_date(ch.get("DatePosted"))
                    when = f"{d:%Y-%m-%d %H:%M}" if d else "?"
                    lines.append("")
                    lines.append(f"{'#' * min(depth, 6)} Reply — {author(ch)}"
                                 f" ({when}, {ch.get('WordCount')} words)")
                    grab_attachments(ch, lines)
                    lines.extend(
                        ["", to_md((ch.get("Message") or {}).get("Html", ""))])
                    walk(ch["PostId"], depth + 1)

            walk(top["PostId"], 2)
            path = pdir / fname
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            paths.append(path)
            rows.append((date, author(top), top.get("WordCount") or 0,
                         reps, fname))

        wc = sorted(r[2] for r in rows)
        rep_wc = sorted(p.get("WordCount") or 0 for p in posts
                        if p.get("ParentPostId"))
        lines = [f"# Posts summary — {topic['Name']}", "",
                 f"- Total posts: {len(posts)} "
                 f"({len(roots)} top-level, {len(posts) - len(roots)} replies)",
                 f"- Top-level word counts: min {wc[0]}, "
                 f"median {wc[len(wc) // 2]}, max {wc[-1]}"]
        if rep_wc:
            lines.append(f"- Reply word counts: min {rep_wc[0]}, median "
                         f"{rep_wc[len(rep_wc) // 2]}, max {rep_wc[-1]}")
        lines += ["", "| Posted | Author | Words | Replies | Thread |",
                  "|---|---|---|---|---|"]
        for date, auth, words, reps, fname in rows:
            when = f"{date:%Y-%m-%d}" if date else ""
            lines.append(f"| {when} | {cell(auth)} | {words} | {reps} "
                         f"| [{cell(Path(fname).stem)}]({quote(fname)}) |")
        spath = pdir / "_summary.md"
        spath.write_text("\n".join(lines) + "\n", encoding="utf-8")
        paths.append(spath)
        self.record(key, fp, paths)

    def save_announcement(self, item, dest, week_n=None):
        key, fp = f"news:{item['Id']}", fingerprint([item, week_n])
        if self.fresh(key, fp):
            return
        dest.mkdir(parents=True, exist_ok=True)
        posted = parse_d2l_date(item.get("StartDate")) or parse_d2l_date(
            item.get("CreatedDate"))
        print(f"    announcement: {item['Title'][:60]}")
        lines = [f"# {item['Title']}", ""]
        if posted:
            lines.append(f"- Posted: {posted:%A %Y-%m-%d %H:%M %Z}")
        if week_n:
            lines.append(f"- Week: {week_n}")
        mod = parse_d2l_date(item.get("LastModifiedDate"))
        if mod:
            lines.append(f"- Last edited: {mod:%Y-%m-%d %H:%M}")
        body = (item.get("Body") or {}).get("Html", "")
        stamp = f"{posted:%Y-%m-%d} " if posted else ""
        notes, linked, lmap = self.save_linked_pages(
            body, dest, prefix=f"{stamp}{safe(item['Title'])} - ")
        lines += ["", relink(to_md(body), lmap)]
        if notes:
            lines += [""] + notes
        paths = [dest / f"{stamp}{safe(item['Title'])}.md"] + linked
        paths[0].write_text("\n".join(lines) + "\n", encoding="utf-8")
        for att in item.get("Attachments", []):
            p = dest / safe(att["FileName"])
            try:
                r = self.c.get_raw(
                    f"/d2l/api/le/{self.c.le_ver}/{self.ou}/news/"
                    f"{item['Id']}/attachments/{att['FileId']}"
                )
                p.write_bytes(r.content)
                paths.append(p)
            except Exception as e:
                print(f"      attachment {att.get('FileName')} failed: {e}")
                self.stats["failed"] += 1
        self.record(key, fp, paths)

    def save_lti_page(self, topic, dest):
        key, fp = f"topic:{topic['TopicId']}", fingerprint(topic)
        if self.fresh(key, fp):
            return
        try:
            r = self.c.follow_lti(topic["Url"])
        except Exception as e:
            # Record the failure so it isn't retried every run; the topic's
            # fingerprint changing (or deleting the stub) triggers a retry.
            print(f"    LTI unreachable, stubbed: {topic.get('Title')}: {e}")
            dest.mkdir(parents=True, exist_ok=True)
            stub = dest / f"{safe(topic['Title'])}.md"
            stub.write_text(
                f"# {topic['Title']}\n\nExternal tool could not be reached "
                f"({e}).\nLaunch it in a browser instead: "
                f"{self.c.base}{topic['Url']}\n", encoding="utf-8")
            self.record(key, fp, [stub])
            return
        dest.mkdir(parents=True, exist_ok=True)
        print(f"    page: {topic['Title'][:60]}")
        path = dest / f"{safe(topic['Title'])}.md"
        notes, linked, lmap = self.save_linked_pages(
            r.text, dest, base_url=r.url, assets_only=True)
        path.write_text(
            f"# {topic['Title']}\n\nSource: {r.url}\n\n"
            f"{relink(to_md(r.text, baseurl=r.url), lmap)}\n"
            + ("\n" + "\n".join(notes) + "\n" if notes else ""),
            encoding="utf-8",
        )
        self.record(key, fp, [path] + linked)

    def save_file_topic(self, topic, dest):
        key, fp = f"topic:{topic['TopicId']}", fingerprint(topic)
        if self.fresh(key, fp):
            return
        if topic.get("IsBroken"):
            self.links.setdefault(dest, []).append(
                f"- {topic.get('Title')}: broken topic in the course itself")
            self.record(key, fp, [])
            return
        try:
            r = self.c.topic_file(self.ou, topic["TopicId"])
        except Exception as e:
            self.links.setdefault(dest, []).append(
                f"- {topic.get('Title')}: FAILED ({e})")
            self.stats["failed"] += 1
            return
        dest.mkdir(parents=True, exist_ok=True)
        print(f"    file: {topic['Title'][:60]}")
        fname = safe(Path(topic.get("Url") or "").name or topic["Title"])
        if fname.lower().endswith((".html", ".htm")):
            path = dest / f"{safe(topic['Title'])}.md"
            notes, linked, lmap = self.save_linked_pages(
                r.text, dest, base_url=r.url)
            path.write_text(
                f"# {topic['Title']}\n\n"
                f"{relink(to_md(r.text, baseurl=r.url), lmap)}\n"
                + ("\n" + "\n".join(notes) + "\n" if notes else ""),
                encoding="utf-8")
        else:
            path = dest / fname
            linked = []
            path.write_bytes(r.content)
        self.record(key, fp, [path] + linked)

    # ---------- content walking ----------

    def save_module_desc(self, mod, mdir):
        """A module's own text — what D2L shows when you click the module
        itself. Courses put real content here (reading lists, weekly to-do
        lists, module welcome pages), sometimes with no topics at all.
        Saved as <Module Title>.md inside the module's folder (Obsidian's
        folder-note convention)."""
        html = ((mod.get("Description") or {}).get("Html") or "").strip()
        if not html:
            return
        key, fp = f"moduledesc:{mod['ModuleId']}", fingerprint(html)
        if self.fresh(key, fp):
            return
        title = mod.get("Title", "module")
        print(f"    module text: {title[:60]}")
        mdir.mkdir(parents=True, exist_ok=True)
        notes, linked, lmap = self.save_linked_pages(html, mdir)
        p = mdir / f"{safe(title)}.md"
        p.write_text(
            f"# {title}\n\n{relink(to_md(html), lmap)}\n"
            + ("\n" + "\n".join(notes) + "\n" if notes else ""),
            encoding="utf-8")
        self.record(key, fp, [p] + linked)

    def process_module(self, mod, dest, week_n=None):
        """Mirror a module into dest/<title>/, keeping the course's own
        nesting. QuickLinks to assignments/quizzes/discussions claim the
        item for the week module (if any) they appear under."""
        m = WEEK_PAT.search(mod.get("Title", ""))
        if m:
            week_n = int(m.group(1))
        if self.only_week and week_n and week_n != self.only_week:
            return
        mdir = dest / safe(mod.get("Title", "module"))
        do_topics = not self.only_week or week_n == self.only_week
        if do_topics:
            self.save_module_desc(mod, mdir)
        for topic in mod.get("Topics", []) if do_topics else []:
            url = topic.get("Url") or ""
            if topic.get("TypeIdentifier") == "Link":
                low = url.lower()
                if "type=lti" in low:
                    self.save_lti_page(topic, mdir)
                elif "type=dropbox" in low or "type=quiz" in low or "type=discuss" in low:
                    kind, oid = self.c.resolve_quicklink(url)
                    if kind and oid in self.catalogs[kind]:
                        if self.activity_week[kind].get(oid) is None:
                            self.activity_week[kind][oid] = week_n
                        self.links.setdefault(mdir, []).append(
                            f"- {topic.get('Title')}: see Activities and "
                            f"Assessments/"
                            f"{f'Week {week_n}' if week_n else 'General'}/")
                    else:
                        self.links.setdefault(mdir, []).append(
                            f"- {topic.get('Title')}: unresolved quickLink {url}")
                else:
                    self.links.setdefault(mdir, []).append(
                        f"- [{topic.get('Title')}]({url})")
            else:
                self.save_file_topic(topic, mdir)
        for sub in mod.get("Modules", []):
            self.process_module(sub, mdir, week_n)

    # ---------- driver ----------

    def run(self):
        self.load_grades()
        self.dropboxes = {f["Id"]: f for f in self.c.dropbox_folders(self.ou)}
        self.quizzes = {q["QuizId"]: q for q in self.c.quizzes(self.ou)}
        self.discussions = {t["TopicId"]: (f, t)
                            for f, t in self.c.discussion_topics(self.ou)}
        self.catalogs = {"dropbox": self.dropboxes, "quiz": self.quizzes,
                         "discussion": self.discussions}
        toc = self.c.content_toc(self.ou)

        print("  Content:")
        for m in toc["Modules"]:
            self.process_module(m, self.content_dir)
        key, fp = "content:toc", fingerprint(toc)
        if not self.fresh(key, fp):
            self.content_dir.mkdir(parents=True, exist_ok=True)
            p = self.content_dir / "toc.json"
            p.write_text(json.dumps(toc, indent=2), encoding="utf-8")
            self.record(key, fp, [p])

        print("  Activities and Assessments:")
        for kind, saver in [("dropbox", self.save_assignment),
                            ("quiz", self.save_quiz)]:
            sub = "assignments" if kind == "dropbox" else "quizzes"
            for oid, obj in self.catalogs[kind].items():
                wk = self.activity_week[kind].get(oid)
                if self.only_week and wk != self.only_week:
                    continue
                saver(obj, self.aa_dir
                      / (f"Week {wk}" if wk else "General") / sub)
        for oid, (f, t) in self.discussions.items():
            wk = self.activity_week["discussion"].get(oid)
            if self.only_week and wk != self.only_week:
                continue
            ddir = (self.aa_dir / (f"Week {wk}" if wk else "General")
                    / "discussions" / safe(t["Name"]))
            self.save_discussion(f, t, ddir)
            self.save_discussion_posts(f, t, ddir)

        print("  Class Data:")
        for item in self.c.news(self.ou):
            posted = parse_d2l_date(item.get("StartDate")) or parse_d2l_date(
                item.get("CreatedDate"))
            week_n = self.week_for(posted)
            if self.only_week and week_n != self.only_week:
                continue
            self.save_announcement(item, self.cd_dir / "Announcements", week_n)
        self.save_grades_summary()
        self.save_calendar()

        for dest, lines in self.links.items():
            dest.mkdir(parents=True, exist_ok=True)
            text = "# External links\n\n" + "\n".join(lines) + "\n"
            p = dest / "links.md"
            # only touch the file when it changed, or its fresh mtime makes
            # the publish stage re-copy every links.md every run
            if not p.exists() or p.read_text(encoding="utf-8") != text:
                p.write_text(text, encoding="utf-8")

        self.dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2))
        s = self.stats
        print(f"  -> {s['new']} new, {s['updated']} updated, "
              f"{s['verified']} verified, {s['failed']} failed")


def derive_week1(course):
    """This course's week-1 Wednesday, from the enrollment's own start
    date (the /courses/{id} offering route is 403 for students). UMGC
    weeks run Wed->Tue and courses open at most a few days before week 1,
    so snap the start date forward to the next Wednesday."""
    start = parse_d2l_date((course.get("Access") or {}).get("StartDate"))
    if not start:
        print("  course start date unavailable -> announcements and "
              "Calendar.md will have no week numbers")
        return None
    d = start.astimezone(EASTERN).date()
    return d + dt.timedelta(days=(2 - d.weekday()) % 7)


def run(week=None, client=None):
    if client is None:
        client = D2LClient()
        client.login()
    out_root = Path(client.cfg.get("output_dir", "output"))
    override = client.cfg.get("week1_start")  # optional; normally derived

    for course_name in client.cfg["courses"]:
        course = client.find_course(course_name)
        week1 = (dt.date.fromisoformat(override) if override
                 else derive_week1(course))
        print(f"\n== {course['Name']} (orgUnitId {course['Id']}, "
              f"week 1: {week1 or 'unknown'}) ==")
        CourseArchiver(
            client, course_name, course["Id"], out_root,
            week1, only_week=week,
        ).run()

    print("\nDone. Output in", out_root.resolve())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int, help="only this week (default: everything)")
    run(week=ap.parse_args().week)


if __name__ == "__main__":
    main()
