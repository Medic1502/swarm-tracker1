"""
Swarm — automatski update pregleda za campaign tracker (Google Sheets). v2

Novo u v2: povlaci i PRAVI datum objave videa i upisuje ga u kolonu Posted.
(YouTube: snippet.publishedAt, TikTok: createTimeISO, Instagram: timestamp)

Env varijable (GitHub Secrets):
  SHEET_ID, GOOGLE_SA_JSON, YOUTUBE_API_KEY, APIFY_TOKEN
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

import gspread
import requests
from google.oauth2.service_account import Credentials

# ---------------------------------------------------------------------------
# PODESAVANJE KOLONA — uskladjeno sa tvojim sheetom:
# A=Status, B=Creator, C=Platform, D=Video Link, E=Posted,
# F=Views, G=Likes, H=Comments, I=Engagement %, J=Earned, K=Last Updated
# ---------------------------------------------------------------------------
TAB_NAME = "Videos"
COL_LINK = "D"
COL_POSTED = "E"
COL_VIEWS = "F"
COL_LIKES = "G"
COL_COMMENTS = "H"
COL_UPDATED = "K"
FIRST_DATA_ROW = 2

YT_API = "https://www.googleapis.com/youtube/v3/videos"
APIFY_RUN = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
TIKTOK_ACTOR = "clockworks~tiktok-scraper"
IG_ACTOR = "apify~instagram-scraper"


def detect_platform(url: str) -> str | None:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "tiktok.com" in u:
        return "tiktok"
    if "instagram.com" in u:
        return "instagram"
    return None


def yt_video_id(url: str) -> str | None:
    patterns = [
        r"youtu\.be/([A-Za-z0-9_-]{6,})",
        r"youtube\.com/shorts/([A-Za-z0-9_-]{6,})",
        r"[?&]v=([A-Za-z0-9_-]{6,})",
        r"youtube\.com/embed/([A-Za-z0-9_-]{6,})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1).split("?")[0].split("&")[0]
    return None


def iso_to_date(value: str) -> str:
    """'2026-07-01T12:34:56.000Z' -> '2026-07-01' (prazno ako ne moze)."""
    if not value:
        return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(value))
    return m.group(1) if m else ""


def col_to_index(col: str) -> int:
    idx = 0
    for ch in col.upper():
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx


# ---------------------------------------------------------------------------
# Fetcheri — svaki vraca {url: {views, likes, comments, posted}}
# ---------------------------------------------------------------------------
def fetch_youtube(urls: list[str], api_key: str) -> dict[str, dict]:
    id_map = {}
    for u in urls:
        vid = yt_video_id(u)
        if vid:
            id_map[vid] = u

    out = {}
    ids = list(id_map.keys())
    for i in range(0, len(ids), 50):
        batch = ids[i : i + 50]
        r = requests.get(
            YT_API,
            params={
                "part": "snippet,statistics",
                "id": ",".join(batch),
                "key": api_key,
            },
            timeout=30,
        )
        r.raise_for_status()
        for item in r.json().get("items", []):
            stats = item.get("statistics", {})
            snippet = item.get("snippet", {})
            out[id_map[item["id"]]] = {
                "views": int(stats.get("viewCount", 0)),
                "likes": int(stats.get("likeCount", 0)),
                "comments": int(stats.get("commentCount", 0)),
                "posted": iso_to_date(snippet.get("publishedAt", "")),
            }
    return out


def fetch_tiktok(urls: list[str], token: str) -> dict[str, dict]:
    if not urls:
        return {}
    r = requests.post(
        APIFY_RUN.format(actor=TIKTOK_ACTOR),
        params={"token": token},
        json={"postURLs": urls, "resultsPerPage": 1},
        timeout=600,
    )
    r.raise_for_status()
    out = {}
    for item in r.json():
        url = item.get("webVideoUrl") or item.get("postPage") or ""
        m = re.search(r"/video/(\d+)", url)
        vid = m.group(1) if m else None
        posted = iso_to_date(item.get("createTimeISO", ""))
        if not posted and item.get("createTime"):
            try:
                posted = datetime.fromtimestamp(
                    int(item["createTime"]), tz=timezone.utc
                ).strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                posted = ""
        for original in urls:
            if vid and vid in original:
                out[original] = {
                    "views": int(item.get("playCount", 0)),
                    "likes": int(item.get("diggCount", 0)),
                    "comments": int(item.get("commentCount", 0)),
                    "posted": posted,
                }
    return out


def fetch_instagram(urls: list[str], token: str) -> dict[str, dict]:
    if not urls:
        return {}
    r = requests.post(
        APIFY_RUN.format(actor=IG_ACTOR),
        params={"token": token},
        json={"directUrls": urls, "resultsType": "posts", "resultsLimit": 1},
        timeout=600,
    )
    r.raise_for_status()
    out = {}
    for item in r.json():
        url = item.get("url", "")
        m = re.search(r"/(?:reel|p)/([A-Za-z0-9_-]+)", url)
        code = m.group(1) if m else None
        for original in urls:
            if code and code in original:
                views = item.get("videoPlayCount") or item.get("videoViewCount") or 0
                out[original] = {
                    "views": int(views),
                    "likes": int(item.get("likesCount", 0)),
                    "comments": int(item.get("commentsCount", 0)),
                    "posted": iso_to_date(item.get("timestamp", "")),
                }
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    sheet_id = os.environ["SHEET_ID"]
    sa_raw = os.environ["GOOGLE_SA_JSON"]
    yt_key = os.environ.get("YOUTUBE_API_KEY", "")
    apify_token = os.environ.get("APIFY_TOKEN", "")

    if sa_raw.strip().startswith("{"):
        sa_info = json.loads(sa_raw)
    else:
        with open(sa_raw) as f:
            sa_info = json.load(f)

    creds = Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(sheet_id).worksheet(TAB_NAME)

    links = ws.col_values(col_to_index(COL_LINK))

    rows = {}
    for i, val in enumerate(links, start=1):
        if i < FIRST_DATA_ROW:
            continue
        val = (val or "").strip()
        if val.startswith("http"):
            rows[val] = i

    if not rows:
        print("Nema linkova u tabu Videos — nista za update.")
        return 0

    by_platform: dict[str, list[str]] = {"youtube": [], "tiktok": [], "instagram": []}
    for url in rows:
        p = detect_platform(url)
        if p:
            by_platform[p].append(url)
        else:
            print(f"[skip] Nepoznata platforma: {url}")

    results: dict[str, dict] = {}
    errors = []

    if by_platform["youtube"]:
        if yt_key:
            try:
                results.update(fetch_youtube(by_platform["youtube"], yt_key))
            except Exception as e:
                errors.append(f"YouTube: {e}")
        else:
            errors.append("YouTube linkovi postoje ali YOUTUBE_API_KEY nije podesen")

    if by_platform["tiktok"]:
        if apify_token:
            try:
                results.update(fetch_tiktok(by_platform["tiktok"], apify_token))
            except Exception as e:
                errors.append(f"TikTok: {e}")
        else:
            errors.append("TikTok linkovi postoje ali APIFY_TOKEN nije podesen")

    if by_platform["instagram"]:
        if apify_token:
            try:
                results.update(fetch_instagram(by_platform["instagram"], apify_token))
            except Exception as e:
                errors.append(f"Instagram: {e}")
        else:
            errors.append("Instagram linkovi postoje ali APIFY_TOKEN nije podesen")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    updates = []
    for url, row in rows.items():
        if url not in results:
            continue
        d = results[url]
        updates.append({"range": f"{COL_VIEWS}{row}", "values": [[d["views"]]]})
        updates.append({"range": f"{COL_LIKES}{row}", "values": [[d["likes"]]]})
        updates.append({"range": f"{COL_COMMENTS}{row}", "values": [[d["comments"]]]})
        updates.append({"range": f"{COL_UPDATED}{row}", "values": [[now]]})
        if d.get("posted"):
            updates.append({"range": f"{COL_POSTED}{row}", "values": [[d["posted"]]]})

    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")

    print(f"Azurirano {len(results)}/{len(rows)} videa u {now}.")
    for e in errors:
        print(f"[greska] {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
