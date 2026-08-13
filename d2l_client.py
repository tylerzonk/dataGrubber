"""Minimal Brightspace (D2L) client using a logged-in web session.

Auth strategy: cookie replay. UMGC logs in through Microsoft SSO, which we
do not script; instead, log in normally in Chrome and copy the session
cookies (d2lSessionVal, d2lSecureSessionVal) into cookies.json. Sessions
use sliding expiry, so cookies stay valid as long as they are used often.

Once the session is authenticated, the same REST API the web UI uses is
available under /d2l/api/. All calls here are read-only GETs.
"""

import json
import time
from pathlib import Path

import requests


class D2LClient:
    def __init__(self, config_path="config.json"):
        cfg = json.loads(Path(config_path).read_text())
        self.base = cfg["base_url"].rstrip("/")
        self.cfg = cfg
        self.delay = cfg.get("request_delay_seconds", 0.5)
        self.s = requests.Session()
        self.s.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) personal-course-archiver"
        )
        self.lp_ver = None  # Learning Platform API version, e.g. "1.31"
        self.le_ver = None  # Learning Environment API version

    # ---------- auth ----------

    def login(self):
        cookies_file = Path("cookies.json")
        if not cookies_file.exists():
            raise RuntimeError(
                "cookies.json not found. Log into learn.umgc.edu in Chrome, "
                "then copy d2lSessionVal and d2lSecureSessionVal from "
                "DevTools > Application > Cookies into cookies.json "
                "(see cookies.example.json)."
            )
        for name, value in json.loads(cookies_file.read_text()).items():
            self.s.cookies.set(name, value, domain="learn.umgc.edu")
        if not self._check_auth():
            raise RuntimeError(
                "Session cookies are stale or wrong. Re-export them from "
                "Chrome (you must be logged into learn.umgc.edu)."
            )
        print("Authenticated via cookies.json")

    def _check_auth(self):
        self._discover_versions()
        r = self.s.get(f"{self.base}/d2l/api/lp/{self.lp_ver}/users/whoami")
        if r.ok:
            me = r.json()
            print(f"Logged in as {me.get('FirstName')} {me.get('LastName')}")
            return True
        return False

    def _discover_versions(self):
        if self.lp_ver:
            return
        r = self.s.get(f"{self.base}/d2l/api/versions/")
        r.raise_for_status()
        for product in r.json():
            if product["ProductCode"] == "lp":
                self.lp_ver = product["LatestVersion"]
            elif product["ProductCode"] == "le":
                self.le_ver = product["LatestVersion"]

    # ---------- generic helpers ----------

    def get_json(self, path):
        time.sleep(self.delay)
        r = self.s.get(f"{self.base}{path}")
        r.raise_for_status()
        return r.json()

    def get_raw(self, url_or_path):
        time.sleep(self.delay)
        url = url_or_path if url_or_path.startswith("http") else f"{self.base}{url_or_path}"
        r = self.s.get(url)
        r.raise_for_status()
        return r

    # ---------- courses ----------

    def my_courses(self):
        """Active course-offering enrollments, paged."""
        items, bookmark = [], None
        while True:
            path = f"/d2l/api/lp/{self.lp_ver}/enrollments/myenrollments/?orgUnitTypeId=3"
            if bookmark:
                path += f"&bookmark={bookmark}"
            page = self.get_json(path)
            items += page["Items"]
            if not page["PagingInfo"]["HasMoreItems"]:
                return items
            bookmark = page["PagingInfo"]["Bookmark"]

    def find_course(self, name_fragment):
        frag = name_fragment.lower().replace(" ", "")
        for e in self.my_courses():
            ou = e["OrgUnit"]
            hay = (ou["Name"] + ou.get("Code", "")).lower().replace(" ", "")
            if frag in hay:
                return ou  # {"Id": orgUnitId, "Name": ..., "Code": ...}
        raise LookupError(f"No enrolled course matching {name_fragment!r}")

    # ---------- content / assignments / quizzes ----------

    def content_toc(self, org_unit_id):
        return self.get_json(f"/d2l/api/le/{self.le_ver}/{org_unit_id}/content/toc")

    def topic_file(self, org_unit_id, topic_id):
        return self.get_raw(
            f"/d2l/api/le/{self.le_ver}/{org_unit_id}/content/topics/{topic_id}/file"
        )

    def dropbox_folders(self, org_unit_id):
        return self.get_json(f"/d2l/api/le/{self.le_ver}/{org_unit_id}/dropbox/folders/")

    def rubric(self, org_unit_id, object_type, object_id):
        return self.get_json(
            f"/d2l/api/le/{self.le_ver}/{org_unit_id}/rubrics"
            f"?objectType={object_type}&objectId={object_id}"
        )

    def follow_lti(self, quicklink_url):
        """Launch an LTI topic and return the final content response.

        The quickLink resolves to a wrapper page whose <d2l-lti-launch>
        element holds the real launch URL; from there the LTI 1.3 handshake
        is a chain of auto-submitting hidden forms (OIDC init -> id_token
        post) that we replay until a response has no such form.
        """
        import re
        from urllib.parse import urljoin
        from bs4 import BeautifulSoup

        r = self.get_raw(quicklink_url)
        m = re.search(r'lti-launch-url="([^"]+)"', r.text)
        if m:
            r = self.get_raw(m.group(1))
        for _ in range(6):
            soup = BeautifulSoup(r.text, "html.parser")
            forms = soup.find_all("form")
            if len(forms) != 1:
                return r
            form = forms[0]
            action = urljoin(r.url, form.get("action", ""))
            data = {i.get("name"): i.get("value", "")
                    for i in form.find_all("input") if i.get("name")}
            time.sleep(self.delay)
            r = self.s.post(action, data=data)
            r.raise_for_status()
        return r

    def quizzes(self, org_unit_id):
        items, url = [], f"/d2l/api/le/{self.le_ver}/{org_unit_id}/quizzes/"
        while url:
            page = self.get_json(url)
            items += page.get("Objects", page.get("Items", []))
            nxt = page.get("Next")
            url = nxt.replace(self.base, "") if nxt else None
        return items

    def news(self, org_unit_id):
        return self.get_json(f"/d2l/api/le/{self.le_ver}/{org_unit_id}/news/")

    def discussion_topics(self, org_unit_id):
        """All discussion topics as (forum, topic) pairs."""
        pairs = []
        for forum in self.get_json(
            f"/d2l/api/le/{self.le_ver}/{org_unit_id}/discussions/forums/"
        ):
            for topic in self.get_json(
                f"/d2l/api/le/{self.le_ver}/{org_unit_id}"
                f"/discussions/forums/{forum['ForumId']}/topics/"
            ):
                pairs.append((forum, topic))
        return pairs

    def resolve_quicklink(self, url):
        """Resolve a quickLink to ('dropbox'|'quiz'|'discussion', id) via its
        redirect Location, or (None, None) if it doesn't resolve."""
        import re
        time.sleep(self.delay)
        r = self.s.get(
            url if url.startswith("http") else f"{self.base}{url}",
            allow_redirects=False,
        )
        loc = r.headers.get("Location", "")
        for kind, pat in (("dropbox", r"[?&]db=(\d+)"),
                          ("quiz", r"[?&]qi=(\d+)"),
                          ("discussion", r"/topics/(\d+)")):
            m = re.search(pat, loc)
            if m:
                return kind, int(m.group(1))
        return None, None
