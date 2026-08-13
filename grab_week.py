"""Incrementally archive UMGC D2L courses into a local tree.

    output/<Course>/
        Week N/                    (only components that week actually has)
            course_content/        files, LTI pages as markdown, links.md
            assignments/<name>/    assignment.md + rubric + attachments
            quizzes/               <name>.md + raw.json
            discussions/           <name>.md + raw.json
            announcements/         <date> <title>.md
        Course Materials/          everything not tied to a week (syllabus,
                                   course resources, general discussions, ...)

What belongs to a week is decided by the content area: each "Week N" /
"Unit N" module's quickLinks claim their assignment/quiz/discussion.
Announcements are filed by post date into UMGC's Wed->Tue week windows
(config "week1_start"). Anything unclaimed lands in Course Materials.

Each course keeps output/<Course>/.manifest.json mapping every item to a
content fingerprint + file paths: re-runs skip verified items, fetch new
ones, and re-save changed ones (e.g. an edited announcement).

Usage:
    python grab_week.py              # everything, incrementally
    python grab_week.py --week 3     # just week 3's content
"""

import argparse
import datetime as dt
import hashlib
import json
import re
from pathlib import Path

import html2text

from d2l_client import D2LClient

WEEK_PAT = re.compile(r"\b(?:week|unit)\s*0?(\d+)\b", re.I)


def to_md(html, baseurl=""):
    h = html2text.HTML2Text(baseurl=baseurl)
    h.body_width = 0
    return h.handle(html or "").strip()


def safe(name):
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip()


def parse_d2l_date(s):
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def fingerprint(obj):
    return hashlib.sha1(
        json.dumps(obj, sort_keys=True, default=str).encode()
    ).hexdigest()


class CourseArchiver:
    def __init__(self, client, label, org_unit, out_root, week1_start, only_week=None):
        self.c = client
        self.ou = org_unit
        self.only_week = only_week
        self.week1 = dt.date.fromisoformat(week1_start) if week1_start else None
        self.dir = out_root / safe(label)
        self.manifest_path = self.dir / ".manifest.json"
        self.manifest = (
            json.loads(self.manifest_path.read_text())
            if self.manifest_path.exists() else {}
        )
        self.stats = {"new": 0, "updated": 0, "verified": 0, "failed": 0}
        self.links = {}       # dest dir -> list of markdown link lines
        self.claimed = {"dropbox": set(), "quiz": set(), "discussion": set()}

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

    # ---------- per-item savers ----------

    def save_assignment(self, folder, dest):
        key, fp = f"dropbox:{folder['Id']}", fingerprint(folder)
        if self.fresh(key, fp):
            return
        adir = dest / safe(folder["Name"])
        adir.mkdir(parents=True, exist_ok=True)
        due = parse_d2l_date(folder.get("DueDate"))
        print(f"    assignment: {folder['Name'][:60]}")

        lines = [f"# {folder['Name']}", ""]
        if due:
            lines.append(f"- Due: {due:%A %Y-%m-%d %H:%M %Z}")
        if (folder.get("Assessment") or {}).get("ScoreDenominator"):
            lines.append(f"- Points: {folder['Assessment']['ScoreDenominator']}")
        lines += ["", "## Instructions", "",
                  to_md((folder.get("CustomInstructions") or {}).get("Html", ""))]
        for r in (folder.get("Assessment") or {}).get("Rubrics") or []:
            lines += ["", f"## Rubric: {r.get('Name', '')}", "",
                      "```json", json.dumps(r, indent=2), "```"]

        paths = [adir / "assignment.md", adir / "raw.json"]
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
        key, fp = f"quiz:{q['QuizId']}", fingerprint(q)
        if self.fresh(key, fp):
            return
        dest.mkdir(parents=True, exist_ok=True)
        print(f"    quiz: {q['Name'][:60]}")
        lines = [f"# {q['Name']}", ""]
        for label, k in [("Due", "DueDate"), ("Start", "StartDate"), ("End", "EndDate")]:
            d = parse_d2l_date(q.get(k))
            if d:
                lines.append(f"- {label}: {d:%A %Y-%m-%d %H:%M %Z}")
        attempts = (q.get("AttemptsAllowed") or {}).get("NumberOfAttemptsAllowed")
        lines.append(f"- Attempts allowed: {attempts if attempts else 'unlimited'}")
        if q.get("TimeLimit"):
            lines.append(f"- Time limit: {q['TimeLimit'].get('TimeLimitValue')} min")
        desc = ((q.get("Description") or {}).get("Text") or {}).get("Html", "")
        instr = ((q.get("Instructions") or {}).get("Text") or {}).get("Html", "")
        if desc:
            lines += ["", "## Description", "", to_md(desc)]
        if instr:
            lines += ["", "## Instructions", "", to_md(instr)]
        name = safe(q["Name"])
        paths = [dest / f"{name}.md", dest / f"{name}.raw.json"]
        paths[0].write_text("\n".join(lines) + "\n", encoding="utf-8")
        paths[1].write_text(json.dumps(q, indent=2), encoding="utf-8")
        self.record(key, fp, paths)

    def save_discussion(self, forum, topic, dest):
        key, fp = f"discussion:{topic['TopicId']}", fingerprint(topic)
        if self.fresh(key, fp):
            return
        dest.mkdir(parents=True, exist_ok=True)
        print(f"    discussion: {topic['Name'][:60]}")
        lines = [f"# {topic['Name']}", "", f"- Forum: {forum.get('Name', '')}"]
        for label, k in [("Due", "DueDate"), ("Start", "StartDate"), ("End", "EndDate")]:
            d = parse_d2l_date(topic.get(k))
            if d:
                lines.append(f"- {label}: {d:%A %Y-%m-%d %H:%M %Z}")
        if topic.get("ScoreOutOf"):
            lines.append(f"- Points: {topic['ScoreOutOf']}")
        if topic.get("MustPostToParticipate"):
            lines.append("- You must post before seeing others' posts")
        desc = (topic.get("Description") or {}).get("Html", "")
        if desc:
            lines += ["", "## Prompt", "", to_md(desc)]
        lines += ["", f"[Open in D2L]({self.c.base}/d2l/le/{self.ou}"
                      f"/discussions/topics/{topic['TopicId']}/View)"]
        name = safe(topic["Name"])
        paths = [dest / f"{name}.md", dest / f"{name}.raw.json"]
        paths[0].write_text("\n".join(lines) + "\n", encoding="utf-8")
        paths[1].write_text(json.dumps(topic, indent=2), encoding="utf-8")
        self.record(key, fp, paths)

    def save_announcement(self, item, dest):
        key, fp = f"news:{item['Id']}", fingerprint(item)
        if self.fresh(key, fp):
            return
        dest.mkdir(parents=True, exist_ok=True)
        posted = parse_d2l_date(item.get("StartDate")) or parse_d2l_date(
            item.get("CreatedDate"))
        print(f"    announcement: {item['Title'][:60]}")
        lines = [f"# {item['Title']}", ""]
        if posted:
            lines.append(f"- Posted: {posted:%A %Y-%m-%d %H:%M %Z}")
        mod = parse_d2l_date(item.get("LastModifiedDate"))
        if mod:
            lines.append(f"- Last edited: {mod:%Y-%m-%d %H:%M}")
        lines += ["", to_md((item.get("Body") or {}).get("Html", ""))]
        stamp = f"{posted:%Y-%m-%d} " if posted else ""
        paths = [dest / f"{stamp}{safe(item['Title'])}.md"]
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
        path.write_text(
            f"# {topic['Title']}\n\nSource: {r.url}\n\n"
            f"{to_md(r.text, baseurl=r.url)}\n",
            encoding="utf-8",
        )
        self.record(key, fp, [path])

    def save_file_topic(self, topic, dest, prefix):
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
            path = dest / f"{prefix}{safe(topic['Title'])}.md"
            path.write_text(
                f"# {topic['Title']}\n\n{to_md(r.text, baseurl=r.url)}\n",
                encoding="utf-8")
        else:
            path = dest / fname
            path.write_bytes(r.content)
        self.record(key, fp, [path])

    # ---------- module walking ----------

    def process_module(self, mod, week_dir, prefix=""):
        """Archive a module's topics into week_dir/{course_content,...},
        claiming linked assignments/quizzes/discussions for this week."""
        title = safe(mod.get("Title", "module"))
        for topic in mod.get("Topics", []):
            url = topic.get("Url") or ""
            if topic.get("TypeIdentifier") == "Link":
                low = url.lower()
                if "type=lti" in low:
                    self.save_lti_page(topic, week_dir / "course_content")
                elif "type=dropbox" in low or "type=quiz" in low or "type=discuss" in low:
                    kind, oid = self.c.resolve_quicklink(url)
                    if kind == "dropbox" and oid in self.dropboxes:
                        self.claimed["dropbox"].add(oid)
                        self.save_assignment(self.dropboxes[oid],
                                             week_dir / "assignments")
                    elif kind == "quiz" and oid in self.quizzes:
                        self.claimed["quiz"].add(oid)
                        self.save_quiz(self.quizzes[oid], week_dir / "quizzes")
                    elif kind == "discussion" and oid in self.discussions:
                        self.claimed["discussion"].add(oid)
                        f, t = self.discussions[oid]
                        self.save_discussion(f, t, week_dir / "discussions")
                    else:
                        self.links.setdefault(week_dir / "course_content", []).append(
                            f"- {topic.get('Title')}: unresolved quickLink {url}")
                else:
                    self.links.setdefault(week_dir / "course_content", []).append(
                        f"- [{topic.get('Title')}]({url})  ({prefix}{title})")
            else:
                self.save_file_topic(topic, week_dir / "course_content",
                                     f"{prefix}{title} - ")
        for sub in mod.get("Modules", []):
            self.process_module(sub, week_dir, prefix=f"{prefix}{title} / ")

    def walk_catchall(self, modules, dest):
        """Archive everything that is not inside a week module."""
        for m in modules:
            if WEEK_PAT.search(m.get("Title", "")):
                continue
            has_week_children = any(
                WEEK_PAT.search(s.get("Title", "")) for s in m.get("Modules", []))
            shallow = dict(m, Modules=[]) if has_week_children else m
            self.process_module(shallow, dest, prefix="")
            if has_week_children:
                self.walk_catchall(m.get("Modules", []), dest)

    # ---------- driver ----------

    def week_window(self, n):
        start = self.week1 + dt.timedelta(days=7 * (n - 1))
        lo = dt.datetime.combine(start, dt.time.min, tzinfo=dt.timezone.utc)
        return lo, lo + dt.timedelta(days=7)

    def run(self):
        self.dropboxes = {f["Id"]: f for f in self.c.dropbox_folders(self.ou)}
        self.quizzes = {q["QuizId"]: q for q in self.c.quizzes(self.ou)}
        self.discussions = {t["TopicId"]: (f, t)
                            for f, t in self.c.discussion_topics(self.ou)}
        toc = self.c.content_toc(self.ou)

        week_mods = {}

        def find_weeks(mods):
            for m in mods:
                match = WEEK_PAT.search(m.get("Title", ""))
                if match:
                    week_mods.setdefault(int(match.group(1)), m)
                else:
                    find_weeks(m.get("Modules", []))
        find_weeks(toc["Modules"])

        for n in sorted(week_mods):
            if self.only_week and n != self.only_week:
                continue
            print(f"  Week {n}:")
            self.process_module(week_mods[n], self.dir / f"Week {n}")

        # announcements, filed into week windows by post date
        if self.week1:
            for item in self.c.news(self.ou):
                posted = parse_d2l_date(item.get("StartDate")) or parse_d2l_date(
                    item.get("CreatedDate"))
                week_n = None
                if posted:
                    for n in week_mods or range(1, 9):
                        lo, hi = self.week_window(n)
                        if lo <= posted < hi:
                            week_n = n
                            break
                if self.only_week and week_n != self.only_week:
                    continue
                dest = (self.dir / f"Week {week_n}" / "announcements"
                        if week_n else self.dir / "Course Materials" / "announcements")
                self.save_announcement(item, dest)

        if not self.only_week:
            print("  Course Materials:")
            cm = self.dir / "Course Materials"
            self.walk_catchall(toc["Modules"], cm)
            for oid, f in self.dropboxes.items():
                if oid not in self.claimed["dropbox"]:
                    self.save_assignment(f, cm / "assignments")
            for oid, q in self.quizzes.items():
                if oid not in self.claimed["quiz"]:
                    self.save_quiz(q, cm / "quizzes")
            for oid, (f, t) in self.discussions.items():
                if oid not in self.claimed["discussion"]:
                    self.save_discussion(f, t, cm / "discussions")

        for dest, lines in self.links.items():
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "links.md").write_text(
                "# External links\n\n" + "\n".join(lines) + "\n", encoding="utf-8")

        self.dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2))
        s = self.stats
        print(f"  -> {s['new']} new, {s['updated']} updated, "
              f"{s['verified']} verified, {s['failed']} failed")


def run(week=None, client=None):
    if client is None:
        client = D2LClient()
        client.login()
    out_root = Path(client.cfg.get("output_dir", "output"))

    for course_name in client.cfg["courses"]:
        course = client.find_course(course_name)
        print(f"\n== {course['Name']} (orgUnitId {course['Id']}) ==")
        CourseArchiver(
            client, course_name, course["Id"], out_root,
            client.cfg.get("week1_start"), only_week=week,
        ).run()

    print("\nDone. Output in", out_root.resolve())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int, help="only this week (default: everything)")
    run(week=ap.parse_args().week)


if __name__ == "__main__":
    main()
