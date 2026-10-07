"""AI-fetch external readings — Claude-optimized, but model-agnostic.

Teachers link out to articles, videos, and interactive resources that
live outside UMGC and cannot be captured with plain HTTP (paywalls,
JavaScript apps, video transcripts). When config "ai_scrape" is true,
this stage turns every external link found in the archive into a
digestible markdown study file right next to the file that linked it —
the same adjacency rule the internal linked-resource grabber follows.
AI files are named "<title> (web).md" and open with a header marking
them as auto-retrieved.

Three providers (config "ai_scrape_provider"):

  "claude" (default, the optimized case) — hands batches of links to a
    headless Claude Code session (`claude -p`) with WebFetch/WebSearch/
    Write tools. The agent fetches each page itself, falls back to a web
    search when a source is paywalled (and says so in the file), and
    writes the files directly. Needs the Claude Code CLI, logged in once
    (run `claude` interactively). Auto-detected on PATH; when the
    pipeline runs on Windows and claude lives inside WSL, `wsl claude`
    is used automatically. "ai_scrape_command" overrides detection.

  "openai" — any OpenAI-compatible chat-completions endpoint: OpenAI
    itself, or a local model server (Ollama, LM Studio, vLLM all speak
    this protocol). dataGrubber fetches each page's HTML itself, then
    asks the model to digest it. Config: "ai_scrape_api_base" (e.g.
    https://api.openai.com/v1 or http://localhost:11434/v1),
    "ai_scrape_model", and "ai_scrape_api_key" (or the OPENAI_API_KEY /
    AI_SCRAPE_API_KEY env var; local servers usually need none).
    Limitation vs the claude case: no browsing — a page the plain fetch
    cannot reach (paywall, JS-only app) is recorded as failed instead of
    substituted.

  "command" — escape hatch for any other agentic CLI. "ai_scrape_command"
    is a template whose "{prompt}" placeholder receives the same task
    prompt the claude case uses; it runs with the destination folder as
    cwd and must write the files itself (e.g. "codex exec {prompt}").

Incremental: output/.ai_manifest.json records every (folder, url)
handled. A link is reprocessed only if its saved file is deleted; failed
links are recorded and skipped until retried with
`python pipeline.py --retry-failed`. A batch where the agent wrote
nothing at all (usage limit, outage, Ctrl+C) is NOT recorded: those
links stay pending, and the stage stops early so the rest aren't burned
through; the next run picks them all up.
"ai_scrape_limit" caps how many links one run processes (0 = no cap).
"""

import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

# hosts the deterministic grabber (grab_week) already handles
INTERNAL_HOSTS = ("learn.umgc.edu", "leocontent.umgc.edu")
AI_SUFFIX = " (web).md"
BATCH = 6          # links per agent invocation: keeps sessions focused
TIMEOUT = 1800     # seconds per agent invocation
FETCH_CHARS = 60000  # page text handed to an API model, at most

# markdown links, excluding images: [text](http...)
MD_LINK = re.compile(r"(?<!\!)\[([^\]]*)\]\((https?://[^)\s]+)\)")


def safe(name):
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip()


def header(title, url):
    return (f"# {title}\n"
            f"> AI-retrieved from {url} on {dt.date.today().isoformat()} — "
            "auto-generated study copy, verify against the original.\n")


# ---------- link collection (provider-independent) ----------

def collect_links(out_root, courses):
    """Every external link in the archive: {(folder, url): task}."""
    tasks = {}
    for course in courses:
        for f in sorted((out_root / course).rglob("*.md")):
            if f.name.endswith(AI_SUFFIX):
                continue  # never re-scrape our own AI output
            if "discussion_posts" in f.parts:
                continue  # classmates' links are peer content, not the
                          # teacher's resources; leave them as plain URLs
            text = f.read_text(encoding="utf-8", errors="replace")
            for title, url in MD_LINK.findall(text):
                if urlparse(url).netloc.lower() in INTERNAL_HOSTS:
                    continue
                key = (f.parent, url)
                if key in tasks:
                    continue
                title = re.sub(r"[*_`#>\s]+", " ", title).strip() or \
                    urlparse(url).netloc + urlparse(url).path
                fname = f"{safe(title)[:70].strip()}{AI_SUFFIX}"
                n = 2  # same title, different url, same folder -> suffix
                while any(t["fname"] == fname and k[0] == f.parent
                          for k, t in tasks.items()):
                    fname = f"{safe(title)[:70].strip()} {n}{AI_SUFFIX}"
                    n += 1
                tasks[key] = {"title": title, "fname": fname, "src": f.name}
    return tasks


# ---------- the agentic providers (claude / command) ----------

def find_claude(cfg):
    """The command (as argv prefix) that runs Claude Code, or None."""
    custom = cfg.get("ai_scrape_command")
    if custom:
        return shlex.split(custom)
    if shutil.which("claude"):
        return ["claude"]
    if os.name == "nt" and shutil.which("wsl"):
        def works(base):
            try:
                return subprocess.run(base + ["--version"], capture_output=True,
                                      timeout=60).returncode == 0
            except Exception:
                return False
        if works(["wsl", "-e", "claude"]):
            return ["wsl", "-e", "claude"]
        # non-login WSL shells miss the user's PATH; ask a login shell
        # where claude lives and call it by absolute path
        try:
            out = subprocess.run(["wsl", "-e", "bash", "-lc", "which claude"],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=60)
            path = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
        except Exception:
            path = ""
        if path and works(["wsl", "-e", path]):
            return ["wsl", "-e", path]
    return None


def build_prompt(folder_rel, batch):
    today = dt.date.today().isoformat()
    lines = [
        "You are collecting external course readings as offline study "
        "notes for a university course archive.",
        "Write files ONLY in the current working directory, using EXACTLY "
        "the filenames given below. No subdirectories.",
        "",
        "For each link:",
        "1. Fetch it. For videos (e.g. YouTube), capture the transcript "
        "or a detailed account of the content; if the page itself gives "
        "too little, search the web for the title for more.",
        "2. If the source is inaccessible (paywall, login wall, dead "
        "link), search the web for the closest substitute covering the "
        "same topic, and state at the top of the file that it is a "
        "substitute and why.",
        "3. Write one markdown file per link:",
        '   line 1: "# <title>"',
        f'   line 2: "> AI-retrieved from <url> on {today} — '
        'auto-generated study copy, verify against the original."',
        "   then the substantive content, organized with headings, "
        "complete enough to study from without opening the link. Prefer "
        "the source's own wording and structure. Do not pad; do not "
        "invent content the source or your search results do not support.",
        "",
        f"These links appear in course material under: {folder_rel}",
        "",
        "Links:",
    ]
    for i, (key, t) in enumerate(batch, 1):
        lines.append(f'{i}. "{t["title"]}" — {key[1]}')
        lines.append(f'   (linked from {t["src"]}) -> write to "{t["fname"]}"')
    return "\n".join(lines)


def agent_cmd(provider, cfg, prompt):
    """argv for one agent invocation, or (None, why)."""
    if provider == "claude":
        base = find_claude(cfg)
        if not base:
            return None, ("Claude Code CLI not found. Install it "
                          "(https://claude.com/claude-code), run `claude` "
                          "once to log in — or set ai_scrape_provider to "
                          "\"openai\" or \"command\" for another model.")
        return base + ["-p", prompt,
                       "--allowedTools", "WebFetch,WebSearch,Write"], None
    # provider == "command": the user's own agent CLI template
    template = cfg.get("ai_scrape_command") or ""
    if not template:
        return None, ("ai_scrape_provider is \"command\" but "
                      "ai_scrape_command is empty; set it to your agent "
                      "CLI with a {prompt} placeholder.")
    argv = [a.replace("{prompt}", prompt) for a in shlex.split(template)]
    if "{prompt}" not in template:
        argv.append(prompt)
    return argv, None


def agent_run(provider, cfg, out_root, pending, manifest, manifest_path):
    by_folder = {}
    for key, val in pending.items():
        by_folder.setdefault(key[0], []).append((key, val))
    done = failed = stalls = 0
    for folder, items in by_folder.items():
        rel = folder.relative_to(out_root).as_posix()
        for i in range(0, len(items), BATCH):
            batch = [(key, t) for key, (mkey, t) in items[i:i + BATCH]]
            print(f"  {provider} <- {rel} ({len(batch)} link(s))")
            prompt = build_prompt(rel, batch)
            cmd, why = agent_cmd(provider, cfg, prompt)
            if not cmd:
                print("  " + why)
                return done, failed
            blurb = ""
            try:
                r = subprocess.run(cmd, cwd=folder, capture_output=True,
                                   text=True, encoding="utf-8",
                                   errors="replace", timeout=TIMEOUT)
                blurb = (r.stdout or "") + (r.stderr or "")
                if r.returncode != 0 and re.search(
                        r"log ?in|authenticat|api key", blurb, re.I):
                    print("  the agent is not logged in — authenticate it "
                          "once, then rerun the pipeline.")
                    manifest_path.write_text(json.dumps(manifest, indent=2))
                    return done, failed
            except subprocess.TimeoutExpired:
                print(f"    timed out after {TIMEOUT}s; whatever was "
                      "written is kept")
            if not any((folder / t["fname"]).exists() for _, t in batch):
                # The prompt has the agent write a file for every link,
                # even a substitute for a paywalled one, so an empty batch
                # means the agent itself is down (usage limit, outage,
                # crash), not the links. Leave them unrecorded so the next
                # run retries them.
                if blurb.strip():
                    print("    agent said: " + blurb.strip()[-300:])
                print("    nothing written -> left pending for the next run")
                stalls += 1
                if stalls >= 2 or re.search(
                        r"limit|quota|credit|overloaded|execution error|"
                        r"try again|capacity", blurb, re.I):
                    print("  the agent looks unavailable (usage limit or "
                          "outage) -> stopping the AI scrape here. Every "
                          "link not yet fetched stays pending; rerun the "
                          "pipeline once it's back.")
                    manifest_path.write_text(json.dumps(manifest, indent=2))
                    return done, failed
                continue
            stalls = 0
            for key, (mkey, t) in items[i:i + BATCH]:
                ok = (folder / t["fname"]).exists()
                manifest[mkey] = {
                    "status": "done" if ok else "failed",
                    "file": t["fname"],
                    "when": dt.date.today().isoformat(),
                }
                done += ok
                failed += not ok
                if not ok:
                    print(f"    no file produced for: {t['title'][:60]}")
            manifest_path.write_text(json.dumps(manifest, indent=2))
    return done, failed


# ---------- the API provider (OpenAI-compatible, key or local) ----------

def api_digest(cfg, url, title):
    """Fetch the page ourselves, ask the model to digest it. Returns
    markdown body (sans header) or raises with a reason."""
    import requests

    r = requests.get(url, timeout=60, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "personal-course-archiver"})
    r.raise_for_status()
    ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
    if ctype.startswith("image/"):
        raise RuntimeError("image link, nothing to digest")
    if "html" in ctype or ctype.startswith("text/"):
        try:
            import html2text
            h = html2text.HTML2Text()
            h.ignore_links = False
            h.body_width = 0
            text = h.handle(r.text)
        except ImportError:
            text = re.sub(r"<[^>]+>", " ", r.text)
    else:
        raise RuntimeError(f"cannot digest {ctype or 'binary'} without an "
                           "agentic provider")
    text = text[:FETCH_CHARS]

    base = (cfg.get("ai_scrape_api_base")
            or "https://api.openai.com/v1").rstrip("/")
    model = cfg.get("ai_scrape_model")
    if not model:
        raise RuntimeError("ai_scrape_model is not set in config")
    key = (cfg.get("ai_scrape_api_key")
           or os.environ.get("AI_SCRAPE_API_KEY")
           or os.environ.get("OPENAI_API_KEY") or "")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    resp = requests.post(f"{base}/chat/completions", timeout=600,
                         headers=headers, json={
        "model": model,
        "messages": [
            {"role": "system", "content":
             "You turn a fetched web page into offline study notes for a "
             "university course archive. Output markdown only — no "
             "preamble, no code fences around the whole answer. Organize "
             "with headings; keep it complete enough to study from "
             "without opening the page; prefer the source's own wording "
             "and structure; do not pad or invent content the page does "
             "not support."},
            {"role": "user", "content":
             f'Page title: "{title}"\nPage URL: {url}\n\n'
             f"Page content (converted to text):\n\n{text}"},
        ],
    })
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def api_run(cfg, out_root, pending, manifest, manifest_path):
    done = failed = 0
    for (folder, url), (mkey, t) in pending.items():
        rel = folder.relative_to(out_root).as_posix()
        print(f"  api <- {rel}: {t['title'][:55]}")
        try:
            body = api_digest(cfg, url, t["title"])
            folder.mkdir(parents=True, exist_ok=True)
            (folder / t["fname"]).write_text(
                header(t["title"], url) + "\n" + body + "\n",
                encoding="utf-8")
            ok = True
        except Exception as e:
            print(f"    failed: {e}")
            ok = False
        manifest[mkey] = {"status": "done" if ok else "failed",
                          "file": t["fname"],
                          "when": dt.date.today().isoformat()}
        done += ok
        failed += not ok
        manifest_path.write_text(json.dumps(manifest, indent=2))
    return done, failed


# ---------- driver ----------

def run(cfg, retry_failed=False):
    if not cfg.get("ai_scrape"):
        print("  disabled (config \"ai_scrape\": false)")
        return
    provider = (cfg.get("ai_scrape_provider") or "claude").lower()
    if provider not in ("claude", "openai", "command"):
        print(f"  unknown ai_scrape_provider {provider!r} "
              "(use \"claude\", \"openai\", or \"command\")")
        return

    out_root = Path(cfg.get("output_dir", "output"))
    manifest_path = out_root / ".ai_manifest.json"
    manifest = (json.loads(manifest_path.read_text())
                if manifest_path.exists() else {})
    tasks = collect_links(out_root, cfg["courses"])
    if retry_failed:
        courses = {c.replace("/", "_") for c in cfg["courses"]}
        drop = [k for k, v in manifest.items() if v.get("status") == "failed"
                and k.split("/", 1)[0] in courses]
        for k in drop:
            del manifest[k]
        manifest_path.write_text(json.dumps(manifest, indent=2))
        print(f"  --retry-failed: {len(drop)} failed link(s) queued again")

    pending = {}
    for (folder, url), t in tasks.items():
        mkey = f"{folder.relative_to(out_root).as_posix()}|{url}"
        ent = manifest.get(mkey)
        if ent and (ent["status"] == "failed" or (folder / ent["file"]).exists()):
            continue  # done, or failed (remove its entry to retry)
        pending[(folder, url)] = (mkey, t)
    print(f"  {len(tasks)} external link(s) in the archive, "
          f"{len(pending)} to fetch")
    if not pending:
        return

    limit = cfg.get("ai_scrape_limit", 0)
    if limit:
        pending = dict(list(pending.items())[:limit])
        print(f"  capped at {limit} this run (config ai_scrape_limit)")

    if provider == "openai":
        done, failed = api_run(cfg, out_root, pending, manifest, manifest_path)
    else:
        done, failed = agent_run(provider, cfg, out_root, pending,
                                 manifest, manifest_path)
    print(f"  ai scrape: {done} fetched, {failed} failed "
          f"(retry failures with: python pipeline.py --retry-failed)")
