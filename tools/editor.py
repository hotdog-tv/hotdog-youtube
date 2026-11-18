#!/usr/bin/env python3
"""
Channel editor for curated.json: a page in your browser, served from this PC only.

Paste a YouTube link (a channel, an @handle, or a live stream) and it checks with YouTube's Data
API what the channel is, what it has live, and whether those streams may be embedded; then add it
to a category, reorder or remove channels, and Publish (commits curated.json and pushes it, which
runs the job that rebuilds channels.json).

Start it with "Edit Channels.bat" in the repo folder (or: python tools/editor.py).
The API key is asked for once and kept in %APPDATA%/HotdogYouTube/key.txt - never in the repo.
Standard library only.
"""
import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CURATED = os.path.join(REPO, "curated.json")
PAGE = os.path.join(HERE, "editor.html")
PUBLISHED = "https://raw.githubusercontent.com/hotdog-tv/hotdog-youtube/main/channels.json"
KEY_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "HotdogYouTube")
KEY_FILE = os.path.join(KEY_DIR, "key.txt")
API = "https://www.googleapis.com/youtube/v3/"
RSS = "https://www.youtube.com/feeds/videos.xml?channel_id="
YT_NS = "{http://www.youtube.com/xml/schemas/2015}"
UA = "hotdog-youtube-editor/1.0"


# ── Key, files, git ──────────────────────────────────────────────────────────

def read_key():
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def write_key(key):
    os.makedirs(KEY_DIR, exist_ok=True)
    with open(KEY_FILE, "w", encoding="utf-8") as f:
        f.write(key.strip())


def load_curated():
    with open(CURATED, encoding="utf-8") as f:
        return json.load(f)


def dump_curated(data):
    """curated.json as it's kept: categories one per line, channels one per line."""
    def line(o):
        inner = json.dumps(o, ensure_ascii=False, separators=(", ", ": "))[1:-1]
        return "{ " + inner + " }"
    out = ['{', '  "categories": [']
    out += [("    " + line(c) + ("," if i < len(data["categories"]) - 1 else "")) for i, c in enumerate(data["categories"])]
    out += ['  ],', '  "channels": [']
    chans = data["channels"]
    for i, c in enumerate(chans):
        if i > 0 and c.get("category") != chans[i - 1].get("category"):
            out.append("")   # (a blank line between categories)
        out.append("    " + line(c) + ("," if i < len(chans) - 1 else ""))
    out += ['  ]', '}', '']
    return "\n".join(out)


def save_curated(data):
    tmp = CURATED + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(dump_curated(data))
    os.replace(tmp, CURATED)


def git(*args, check=False):
    r = subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()}")
    return r


def has_changes():
    return bool(git("status", "--porcelain", "--", "curated.json").stdout.strip())


# ── YouTube ──────────────────────────────────────────────────────────────────

class ApiError(Exception):
    pass


def api(path, **params):
    key = read_key()
    if not key:
        raise ApiError("No API key yet")
    url = API + path + "?" + urllib.parse.urlencode({**params, "key": key})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(body)["error"]["message"]
        except Exception:  # noqa: BLE001
            msg = body[:200]
        raise ApiError(f"YouTube said: {msg}")


def parse_link(text):
    """What a pasted link points at: ('video', id) | ('channel', UC...) | ('handle', @x) | ('user', name) | ('custom', name)."""
    t = text.strip()
    if re.fullmatch(r"UC[\w-]{22}", t):
        return "channel", t
    if re.fullmatch(r"@[\w.-]+", t):
        return "handle", t
    if re.fullmatch(r"[\w-]{11}", t):
        return "video", t
    if not re.match(r"https?://", t):
        t = "https://" + t
    u = urllib.parse.urlparse(t)
    host = u.netloc.lower().removeprefix("www.").removeprefix("m.")
    parts = [p for p in u.path.split("/") if p]
    q = urllib.parse.parse_qs(u.query)
    if host == "youtu.be" and parts:
        return "video", parts[0][:11]
    if "youtube.com" not in host:
        return None, None
    if "v" in q:
        return "video", q["v"][0][:11]
    if parts:
        if parts[0] in ("live", "shorts", "embed") and len(parts) > 1:
            return "video", parts[1][:11]
        if parts[0].startswith("@"):
            return "handle", parts[0]
        if parts[0] == "channel" and len(parts) > 1:
            return "channel", parts[1]
        if parts[0] == "user" and len(parts) > 1:
            return "user", parts[1]
        if parts[0] == "c" and len(parts) > 1:
            return "custom", parts[1]
    return None, None


def video_info(item):
    sn, st, ls, cd = item.get("snippet", {}), item.get("status", {}), item.get("liveStreamingDetails", {}), item.get("contentDetails", {})
    rr = cd.get("regionRestriction", {})
    v = ls.get("concurrentViewers")
    return {
        "videoId": item["id"], "title": sn.get("title", ""), "channelId": sn.get("channelId", ""),
        "state": sn.get("liveBroadcastContent", "none"),
        "viewers": int(v) if v and str(v).isdigit() else 0,
        "scheduled": ls.get("scheduledStartTime", ""),
        "embeddable": bool(st.get("embeddable", True)) and st.get("privacyStatus") != "private",
        "ageRestricted": cd.get("contentRating", {}).get("ytRating") == "ytAgeRestricted",
        "regionsAllowed": rr.get("allowed", []), "regionsBlocked": rr.get("blocked", []),
        "thumb": ((sn.get("thumbnails") or {}).get("medium") or {}).get("url", ""),
    }


def videos(ids):
    out = []
    ids = list(dict.fromkeys(ids))
    for i in range(0, len(ids), 50):
        data = api("videos", part="snippet,liveStreamingDetails,status,contentDetails", id=",".join(ids[i:i + 50]))
        out += [video_info(it) for it in data.get("items", [])]
    return out


def rss_ids(channel_id):
    try:
        with urllib.request.urlopen(urllib.request.Request(RSS + channel_id, headers={"User-Agent": UA}), timeout=20) as r:
            root = ET.fromstring(r.read())
        return [el.text for el in root.iter(YT_NS + "videoId") if el.text]
    except Exception:  # noqa: BLE001
        return []


def slug(s):
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s or "channel"


def check(link):
    """Everything the page shows about a pasted link."""
    kind, value = parse_link(link)
    if not kind:
        return {"ok": False, "error": "That doesn't look like a YouTube link. Paste a channel page, an @handle, or a live stream's link."}
    units = 0
    pasted = None
    channel_id = None
    if kind == "video":
        found = videos([value]); units += 1
        if not found:
            return {"ok": False, "error": "YouTube has no video with that link (it may be private or removed)."}
        pasted = found[0]
        channel_id = pasted["channelId"]
    elif kind == "channel":
        channel_id = value
    else:
        if kind == "handle":
            data = api("channels", part="id", forHandle=value); units += 1
        elif kind == "user":
            data = api("channels", part="id", forUsername=value); units += 1
        else:  # an old /c/ custom address: found by searching (100 units)
            data = api("search", part="id", type="channel", q=value, maxResults=1); units += 100
            data = {"items": [{"id": it["id"]["channelId"]} for it in data.get("items", [])]}
        items = data.get("items", [])
        if not items:
            return {"ok": False, "error": f"YouTube has no channel called {value}."}
        channel_id = items[0]["id"]

    data = api("channels", part="snippet", id=channel_id); units += 1
    if not data.get("items"):
        return {"ok": False, "error": "YouTube has no channel with that id."}
    sn = data["items"][0]["snippet"]
    th = sn.get("thumbnails", {})
    channel = {
        "id": channel_id, "title": sn.get("title", ""), "handle": sn.get("customUrl", ""),
        "logo": (th.get("high") or th.get("medium") or th.get("default") or {}).get("url", ""),
        "country": sn.get("country", ""), "language": sn.get("defaultLanguage", "") or "",
    }
    if channel["handle"] and not channel["handle"].startswith("@"):
        channel["handle"] = "@" + channel["handle"]

    # Live and upcoming: the latest uploads first (free), a search only if nothing's live there
    ids = rss_ids(channel_id)
    if pasted:
        ids.insert(0, pasted["videoId"])
    vids = videos(ids) if ids else []
    units += (len(ids) + 49) // 50
    lives = [v for v in vids if v["state"] == "live"]
    searched = False
    if not lives:
        data = api("search", part="id", channelId=channel_id, eventType="live", type="video", maxResults=25); units += 100
        found = [it["id"]["videoId"] for it in data.get("items", []) if it.get("id", {}).get("videoId")]
        if found:
            lives = [v for v in videos(found) if v["state"] == "live"]; units += 1
        searched = True
    lives.sort(key=lambda v: -v["viewers"])
    upcoming = sorted([v for v in vids if v["state"] == "upcoming"], key=lambda v: v["scheduled"])[:5]

    curated = load_curated()
    in_list = [c for c in curated["channels"] if c.get("channelId") == channel_id]
    warnings = []
    if pasted and pasted["state"] == "none":
        warnings.append("The link is an ordinary video, not a live stream. YouTube Live only shows a channel's live streams; the channel can still be added.")
    if not lives:
        warnings.append("Nothing live on this channel right now. Add it only if it streams regularly - it shows as Off air until it does." +
                        (" (Upcoming streams are listed below.)" if upcoming else ""))
    elif not any(v["embeddable"] and not v["ageRestricted"] for v in lives):
        warnings.append("None of its live streams may be embedded. On Android it would only open in the YouTube app.")
    if len(lives) > 1:
        warnings.append(f"It has {len(lives)} streams live. Without a title filter the app shows the most watched; pick one below to keep it on that stream.")
    if in_list:
        warnings.append("Already in the list as: " + ", ".join(c.get("name", c["id"]) for c in in_list))

    base = slug(channel["title"])
    taken = {c["id"] for c in curated["channels"]}
    sid, n = base, 2
    while sid in taken:
        sid, n = f"{base}-{n}", n + 1
    return {
        "ok": True, "channel": channel, "lives": lives, "upcoming": upcoming, "pasted": pasted,
        "suggestId": sid, "inList": [c["id"] for c in in_list], "warnings": warnings,
        "units": units, "searched": searched,
    }


# ── The page's requests ──────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path == "/":
            with open(PAGE, "rb") as f:
                return self.send(200, f.read(), "text/html")
        if self.path == "/api/state":
            return self.send(200, {"curated": load_curated(), "keySet": bool(read_key()), "changed": has_changes()})
        if self.path == "/api/published":
            try:
                req = urllib.request.Request(PUBLISHED, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    return self.send(200, json.load(r))
            except Exception as e:  # noqa: BLE001
                return self.send(200, {"error": str(e)})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        # Only this page may ask (a page elsewhere on the web can't reach 127.0.0.1 with JSON and this header)
        if self.headers.get("X-Editor") != "1":
            return self.send(403, {"error": "forbidden"})
        try:
            b = self.body()
            if self.path == "/api/key":
                write_key(b.get("key", ""))
                try:
                    api("channels", part="id", forHandle="@YouTube")
                except ApiError as e:
                    write_key("")
                    return self.send(200, {"ok": False, "error": str(e)})
                return self.send(200, {"ok": True})
            if self.path == "/api/check":
                return self.send(200, check(b.get("link", "")))
            if self.path == "/api/save":
                data = b["curated"]
                ids = [c["id"] for c in data["channels"]]
                if len(ids) != len(set(ids)) or not all(re.fullmatch(r"[a-z0-9-]+", i) for i in ids):
                    return self.send(200, {"ok": False, "error": "Each channel needs its own id: lower-case letters, digits and hyphens."})
                cats = {c["id"] for c in data["categories"]}
                if any(c.get("category") not in cats for c in data["channels"]):
                    return self.send(200, {"ok": False, "error": "A channel is in a category that doesn't exist."})
                save_curated(data)
                return self.send(200, {"ok": True, "changed": has_changes()})
            if self.path == "/api/publish":
                if not has_changes():
                    return self.send(200, {"ok": True, "message": "Nothing to publish."})
                msg = (b.get("message") or "").strip() or "Channels updated"
                git("add", "curated.json", check=True)
                git("commit", "-m", msg, check=True)
                # (the job commits channels.json every so often: take its commits first)
                git("pull", "--rebase", "--quiet", check=True)
                git("push", "--quiet", check=True)
                return self.send(200, {"ok": True, "message": "Published. The job is rebuilding channels.json - the apps have it within a few minutes."})
            if self.path == "/api/revert":
                git("checkout", "--", "curated.json", check=True)
                return self.send(200, {"ok": True})
            self.send(404, {"error": "not found"})
        except ApiError as e:
            self.send(200, {"ok": False, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            self.send(200, {"ok": False, "error": str(e)})


def main():
    # Up to date before editing (only when there's nothing of ours waiting)
    if not has_changes():
        r = git("pull", "--ff-only", "--quiet")
        if r.returncode != 0:
            print("Couldn't get the latest from GitHub:", (r.stderr or r.stdout).strip())
    # (EDITOR_PORT / EDITOR_NO_BROWSER: for trying it without a browser opening)
    # (no address reuse: on Windows it would let this share a port another program is listening on)
    class Server(ThreadingHTTPServer):
        allow_reuse_address = False
    server = Server(("127.0.0.1", int(os.environ.get("EDITOR_PORT") or 0)), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Channel editor: {url}\nClose this window to stop it.", flush=True)
    if not os.environ.get("EDITOR_NO_BROWSER"):
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
