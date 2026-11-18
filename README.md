# Hotdog TV - YouTube Live channels

The list of live YouTube channels Hotdog TV offers in **Hotdog Channels › YouTube Live**.

| File | What it is |
|---|---|
| `curated.json` | The channels on offer - edit this one |
| `channels.json` | What the apps read: every channel with what's live now and coming up. Written by the job, don't edit |
| `scripts/update.py` | The job that writes `channels.json` (Python, standard library only) |
| `index.html` | A page showing the list and what's live |

The apps read **https://hotdog-tv.github.io/hotdog-youtube/channels.json**. Only the job talks to
YouTube's Data API; the apps need no key and never call it. Videos play in YouTube's own embedded
player (Android) or the YouTube app (Apple TV, desktop browser), never extracted.

## Adding a channel

Add a line to `curated.json`:

```json
{ "id": "sky-news", "name": "Sky News", "handle": "@SkyNews", "channelId": "UCoMdktPbSTixAyNGwb-UYkQ", "category": "news", "country": "GB", "language": "en" }
```

- `id` - ours, never change it once published (devices remember channels by it).
- `channelId` - the `UC...` id. Leave it out and give `handle` and the job looks it up (1 unit).
- `category` - one of the `categories` at the top.
- `match` / `exclude` - optional, case-insensitive patterns on the stream title, for a channel
  with several live streams (see the Lofi Girl entries). Without `match`, the most watched
  embeddable stream is used.
- `videoId` - optional: prefer this stream while it's live.
- `logo` - optional: otherwise the channel's YouTube picture.

Pushing a change to `curated.json` runs the job straight away.

## The job

`.github/workflows/update.yml` runs every 15 minutes and commits `channels.json` only when
something changed (and at least once a day). It needs the repository secret
**`YOUTUBE_API_KEY`** (Settings › Secrets and variables › Actions); without it, it does nothing.

Quota (YouTube gives 10,000 units a day; the job keeps to `DAILY_BUDGET`, 8,000):

- checking streams already known: 1 unit per 50 videos, every run
- RSS feeds for new uploads: free
- searching a channel for live streams: 100 units - each channel once a day, and every 6 hours
  while it has nothing live

That's about 2,500-5,000 units a day for 30 channels. Searches are what grows with the list:
past about 50 channels, raise `SEARCH_ANY_H` in `scripts/update.py`.

What the job has learnt between runs is in the Actions cache (`.state/`), not the repo.

Run it locally: `YOUTUBE_API_KEY=... DRY_RUN=1 python3 scripts/update.py`

## channels.json (schema 1)

```json
{
 "schema": 1, "updated": "2026-11-18T09:00:00Z",
 "attribution": "Channel and programme information from YouTube",
 "terms": { "youtube": "...", "googlePrivacy": "..." },
 "categories": [{ "id": "news", "name": "News" }],
 "channels": [{
  "id": "sky-news", "name": "Sky News", "category": "news", "country": "GB", "language": "en",
  "ytChannelId": "UC...", "logo": "https://yt3.ggpht.com/...",
  "watchUrl": "https://www.youtube.com/channel/UC.../live",
  "live": { "videoId": "...", "title": "...", "since": "...", "viewers": 1234,
            "embeddable": true, "ageRestricted": false,
            "regionsAllowed": [], "regionsBlocked": [], "thumb": "https://i.ytimg.com/..." },
  "upcoming": [{ "videoId": "...", "title": "...", "start": "...", "embeddable": true }]
 }]
}
```

`live` is `null` when the channel has nothing on. When `updated` is more than a few days old the
job has stopped: apps fall back to `watchUrl` / the channel's live embed.
