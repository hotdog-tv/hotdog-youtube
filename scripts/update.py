#!/usr/bin/env python3
"""
Builds channels.json: the curated YouTube channels (curated.json) with what each has live now
and coming up. Run by .github/workflows/update.yml every 15 minutes; the apps only ever read
the finished channels.json, so they need no YouTube key and make no API calls.

How a channel's live streams are found, cheapest first:
  1. Streams already known to be live or coming up are checked again (videos.list, 1 unit per 50).
  2. Each channel's RSS feed of its latest uploads (free) finds new streams as they're scheduled.
  3. A channel with nothing live is searched (search.list, 100 units) at most every
     SEARCH_OFFLINE_H hours, and every channel every SEARCH_ANY_H hours (a 24/7 stream that
     started months ago is long gone from its RSS feed).
What it learns between runs is kept in .state/state.json (an Actions cache, not committed); if
that's lost, the streams in the last channels.json are picked up again.

Standard library only. Environment:
  YOUTUBE_API_KEY  the Data API v3 key (a repository secret). Without it nothing is changed.
  DAILY_BUDGET     units a day this may use (default 8000 of YouTube's 10,000).
  STATE_FILE       where the state is kept (default .state/state.json).
  DRY_RUN=1        print what would be written instead of writing channels.json.
"""
import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CURATED = os.path.join(ROOT, "curated.json")
OUT = os.path.join(ROOT, "channels.json")
STATE_FILE = os.environ.get("STATE_FILE") or os.path.join(ROOT, ".state", "state.json")
API = "https://www.googleapis.com/youtube/v3/"
RSS = "https://www.youtube.com/feeds/videos.xml?channel_id="
KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
DAILY_BUDGET = int(os.environ.get("DAILY_BUDGET") or 8000)
DRY_RUN = os.environ.get("DRY_RUN") == "1"

SCHEMA = 1
SEARCH_OFFLINE_H = 6          # a channel with nothing live: searched again after this long
SEARCH_ANY_H = 24             # every channel: searched again after this long (finds extra streams)
DETAILS_H = 24                # channel names and logos looked up again after this long
UPCOMING_DAYS = 7             # upcoming streams further off than this aren't listed
UPCOMING_PAST_H = 6           # an "upcoming" stream this late is left out (often a placeholder)
UPCOMING_MAX = 5              # upcoming streams listed per channel
HEARTBEAT_H = 24              # channels.json is written at least this often, changed or not
PLAIN_KEEP = 300              # ordinary uploads remembered per channel (so they aren't looked up again)
SEARCH_COST = 100
UA = "hotdog-youtube/1.0 (+https://github.com/hotdog-tv/hotdog-youtube)"
YT_NS = "{http://www.youtube.com/xml/schemas/2015}"


class OutOfQuota(Exception):
    pass


def now_s():
    return int(time.time())


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    if not s:
        return 0
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, data, compact=False):
    if os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        if compact:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write("\n")
    os.replace(tmp, path)


class Quota:
    """Units used today. YouTube's day starts at midnight Pacific time."""

    def __init__(self, state):
        day = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
        q = state.setdefault("quota", {})
        if q.get("day") != day:
            q.clear()
            q.update(day=day, used=0)
        self.q = q
        self.blocked = False   # YouTube said the quota is used up

    @property
    def used(self):
        return self.q["used"]

    def can(self, cost, reserve=0):
        return not self.blocked and self.used + cost + reserve <= DAILY_BUDGET

    def spend(self, cost):
        self.q["used"] += cost


def api(quota, path, params, cost=1, reserve=0):
    if not quota.can(cost, reserve):
        raise OutOfQuota(f"{path}: daily budget reached ({quota.used}/{DAILY_BUDGET})")
    url = API + path + "?" + urllib.parse.urlencode({**params, "key": KEY})
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    quota.spend(cost)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        if e.code == 403 and ("quotaExceeded" in body or "dailyLimitExceeded" in body):
            quota.blocked = True
            raise OutOfQuota(f"{path}: YouTube says the quota is used up")
        raise RuntimeError(f"{path}: HTTP {e.code} {body[:300]}")


def rss_ids(channel_id):
    """The latest uploads (about 15) from a channel's RSS feed: free, no key."""
    req = urllib.request.Request(RSS + channel_id, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            root = ET.fromstring(r.read())
    except Exception as e:  # noqa: BLE001 - a feed that fails is just skipped this run
        print(f"  RSS {channel_id}: {e}", file=sys.stderr)
        return []
    return [el.text for el in root.iter(YT_NS + "videoId") if el.text]


def video_info(item):
    """What's kept about a video from a videos.list item."""
    sn = item.get("snippet", {})
    ls = item.get("liveStreamingDetails", {})
    st = item.get("status", {})
    cd = item.get("contentDetails", {})
    rr = cd.get("regionRestriction", {})
    thumbs = sn.get("thumbnails", {})
    thumb = (thumbs.get("maxres") or thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
    viewers = ls.get("concurrentViewers")
    return {
        "channelId": sn.get("channelId", ""),
        "title": sn.get("title", ""),
        "state": sn.get("liveBroadcastContent", "none"),   # live | upcoming | none
        "start": ls.get("actualStartTime") or "",
        "scheduled": ls.get("scheduledStartTime") or "",
        "viewers": int(viewers) if viewers and str(viewers).isdigit() else 0,
        "embeddable": bool(st.get("embeddable", True)) and st.get("privacyStatus", "public") != "private",
        "ageRestricted": cd.get("contentRating", {}).get("ytRating") == "ytAgeRestricted",
        "regionsAllowed": rr.get("allowed", []),
        "regionsBlocked": rr.get("blocked", []),
        "thumb": thumb,
        "checked": now_s(),
    }


def check_videos(quota, ids):
    """Looks up [ids] (any number); returns {videoId: info} for those that still exist."""
    found = {}
    ids = list(dict.fromkeys(ids))
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        data = api(quota, "videos", {
            "part": "snippet,liveStreamingDetails,status,contentDetails",
            "id": ",".join(chunk), "maxResults": 50,
        })
        for item in data.get("items", []):
            found[item["id"]] = video_info(item)
    return found


def search_live(quota, channel_id):
    data = api(quota, "search", {
        "part": "id", "channelId": channel_id, "eventType": "live", "type": "video", "maxResults": 50,
    }, cost=SEARCH_COST, reserve=200)
    return [it["id"]["videoId"] for it in data.get("items", []) if it.get("id", {}).get("videoId")]


def resolve_handles(quota, entries, state):
    handles = state.setdefault("handles", {})
    for e in entries:
        if e.get("channelId"):
            continue
        h = e.get("handle", "")
        if not h:
            print(f"  {e['id']}: no channelId or handle - skipped", file=sys.stderr)
            continue
        if h not in handles:
            data = api(quota, "channels", {"part": "id", "forHandle": h})
            items = data.get("items", [])
            if not items:
                print(f"  {e['id']}: handle {h} not found", file=sys.stderr)
                continue
            handles[h] = items[0]["id"]
        e["channelId"] = handles[h]


def refresh_details(quota, channel_ids, state):
    """Channel names and logos, once a day."""
    chans = state.setdefault("channels", {})
    due = [c for c in channel_ids if now_s() - chans.get(c, {}).get("detailsAt", 0) > DETAILS_H * 3600]
    for i in range(0, len(due), 50):
        chunk = due[i:i + 50]
        data = api(quota, "channels", {"part": "snippet", "id": ",".join(chunk), "maxResults": 50})
        for item in data.get("items", []):
            sn = item.get("snippet", {})
            th = sn.get("thumbnails", {})
            logo = (th.get("high") or th.get("medium") or th.get("default") or {}).get("url", "")
            c = chans.setdefault(item["id"], {})
            c.update(title=sn.get("title", ""), logo=logo, detailsAt=now_s())


def pick_live(entry, videos):
    """The live stream an entry shows: matching its title rules, embeddable first, then most watched."""
    match = re.compile(entry["match"], re.I) if entry.get("match") else None
    exclude = re.compile(entry["exclude"], re.I) if entry.get("exclude") else None
    lives = [(vid, v) for vid, v in videos
             if v["state"] == "live" and (not match or match.search(v["title"]))
             and not (exclude and exclude.search(v["title"]))]
    if entry.get("videoId"):
        pinned = [x for x in lives if x[0] == entry["videoId"]]
        if pinned:
            return pinned[0]
    lives.sort(key=lambda x: (not x[1]["embeddable"], -x[1]["viewers"]))
    return lives[0] if lives else None


def build(curated, state):
    tracked = state.get("tracked", {})
    chans = state.get("channels", {})
    by_channel = {}
    for vid, v in tracked.items():
        by_channel.setdefault(v["channelId"], []).append((vid, v))
    now = now_s()
    out_channels = []
    for e in curated["channels"]:
        cid = e.get("channelId")
        if not cid:
            continue
        videos = by_channel.get(cid, [])
        live = pick_live(e, videos)
        match = re.compile(e["match"], re.I) if e.get("match") else None
        upcoming = []
        for vid, v in videos:
            if v["state"] != "upcoming" or (match and not match.search(v["title"])):
                continue
            at = parse_iso(v["scheduled"])
            if not at or at < now - UPCOMING_PAST_H * 3600 or at > now + UPCOMING_DAYS * 86400:
                continue
            upcoming.append((at, vid, v))
        upcoming.sort()
        info = chans.get(cid, {})
        ch = {
            "id": e["id"],
            "name": e.get("name") or info.get("title") or e["id"],
            "category": e.get("category", ""),
            "country": e.get("country", ""),
            "language": e.get("language", ""),
            "ytChannelId": cid,
            "logo": e.get("logo") or info.get("logo", ""),
            "watchUrl": f"https://www.youtube.com/channel/{cid}/live",
            "live": None,
            "upcoming": [{
                "videoId": vid, "title": v["title"], "start": v["scheduled"], "embeddable": v["embeddable"],
            } for at, vid, v in upcoming[:UPCOMING_MAX]],
        }
        if live:
            vid, v = live
            ch["live"] = {
                "videoId": vid, "title": v["title"], "since": v["start"], "viewers": v["viewers"],
                "embeddable": v["embeddable"], "ageRestricted": v["ageRestricted"],
                "regionsAllowed": v["regionsAllowed"], "regionsBlocked": v["regionsBlocked"],
                "thumb": v["thumb"],
            }
        out_channels.append(ch)
    return {
        "schema": SCHEMA,
        "updated": iso(now),
        "attribution": "Channel and programme information from YouTube",
        "terms": {
            "youtube": "https://www.youtube.com/t/terms",
            "googlePrivacy": "https://policies.google.com/privacy",
        },
        "categories": curated.get("categories", []),
        "channels": out_channels,
    }


def seed_from_previous(state):
    """No state (first run, or the cache was lost): the streams in the last channels.json are tracked again."""
    if state.get("tracked"):
        return
    prev = load(OUT, None)
    if not prev:
        return
    tracked = state.setdefault("tracked", {})
    for ch in prev.get("channels", []):
        cid = ch.get("ytChannelId", "")
        if ch.get("live"):
            tracked[ch["live"]["videoId"]] = {"channelId": cid, "title": ch["live"].get("title", ""), "state": "live",
                                              "start": "", "scheduled": "", "viewers": 0, "embeddable": True,
                                              "ageRestricted": False, "regionsAllowed": [], "regionsBlocked": [],
                                              "thumb": "", "checked": 0}
        for u in ch.get("upcoming", []):
            tracked[u["videoId"]] = {"channelId": cid, "title": u.get("title", ""), "state": "upcoming",
                                     "start": "", "scheduled": u.get("start", ""), "viewers": 0,
                                     "embeddable": u.get("embeddable", True), "ageRestricted": False,
                                     "regionsAllowed": [], "regionsBlocked": [], "thumb": "", "checked": 0}


def run():
    if not KEY:
        print("::warning::YOUTUBE_API_KEY isn't set (Settings > Secrets and variables > Actions) - nothing updated")
        return 0
    curated = load(CURATED, None)
    if not curated:
        print("curated.json is missing or isn't valid JSON", file=sys.stderr)
        return 1
    state = load(STATE_FILE, {})
    quota = Quota(state)
    start_used = quota.used
    entries = curated["channels"]
    seed_from_previous(state)
    tracked = state.setdefault("tracked", {})
    plain = state.setdefault("plain", {})
    chans = state.setdefault("channels", {})
    errors = []

    try:
        resolve_handles(quota, entries, state)
        channel_ids = list(dict.fromkeys(e["channelId"] for e in entries if e.get("channelId")))
        refresh_details(quota, channel_ids, state)

        # New uploads from the RSS feeds (free), less those already known
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            feeds = dict(zip(channel_ids, pool.map(rss_ids, channel_ids)))
        fresh = []
        owner = {}   # video -> channel, for those that turn out deleted or private
        for cid, ids in feeds.items():
            known = set(plain.get(cid, []))
            for i in ids:
                if i not in known and i not in tracked:
                    fresh.append(i)
                    owner[i] = cid

        def apply(found, asked):
            for vid in asked:
                v = found.get(vid)
                if v and v["state"] in ("live", "upcoming"):
                    tracked[vid] = v
                    continue
                gone = tracked.pop(vid, None)
                cid = owner.get(vid) or (v or gone or {}).get("channelId")
                if cid:
                    lst = plain.setdefault(cid, [])
                    if vid not in lst:
                        lst.append(vid)
                    del lst[:-PLAIN_KEEP]

        asked = list(tracked) + fresh
        apply(check_videos(quota, asked), asked)

        # Searches: channels with nothing live (every SEARCH_OFFLINE_H) and every channel now and then
        live_channels = {v["channelId"] for v in tracked.values() if v["state"] == "live"}
        found_ids = []
        due = []
        for cid in channel_ids:
            last = chans.setdefault(cid, {}).get("searchedAt", 0)
            age_h = (now_s() - last) / 3600
            if (cid not in live_channels and age_h >= SEARCH_OFFLINE_H) or age_h >= SEARCH_ANY_H:
                due.append((last, cid))
        for _, cid in sorted(due):   # longest since searched first, in case the budget runs out
            try:
                ids = search_live(quota, cid)
            except OutOfQuota as e:
                print(f"  search stopped: {e}")
                break
            chans[cid]["searchedAt"] = now_s()
            found_ids += [i for i in ids if i not in tracked]
        if found_ids:
            apply(check_videos(quota, found_ids), found_ids)
    except OutOfQuota as e:
        errors.append(str(e))
        print(f"::warning::{e} - channels.json built from what's known")
    except Exception as e:  # noqa: BLE001 - keep what we have, report it
        errors.append(str(e))
        print(f"::error::{e}")

    out = build(curated, state)
    prev = load(OUT, None)
    same = prev is not None and {**prev, "updated": None} == {**out, "updated": None}
    stale = prev is None or time.time() - parse_iso(prev.get("updated", "")) > HEARTBEAT_H * 3600
    live_n = sum(1 for c in out["channels"] if c["live"])
    print(f"{live_n}/{len(out['channels'])} channels live, {len(tracked)} streams tracked, "
          f"{quota.used - start_used} units this run, {quota.used}/{DAILY_BUDGET} today")
    if DRY_RUN:
        print(json.dumps(out, indent=1, ensure_ascii=False))
    elif not same or stale:
        save(OUT, out)
        print("channels.json written")
    else:
        print("No change")
    save(STATE_FILE, state, compact=True)
    # A failure with nothing at all to show is an error; anything else carries on
    return 1 if errors and not tracked else 0


if __name__ == "__main__":
    sys.exit(run())
