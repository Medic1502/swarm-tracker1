"""
Swarm — automatski update pregleda za campaign tracker (Google Sheets).

Sta radi:
  1. Otvori Google Sheet (tab "Videos")
  2. Procita sve linkove ka videima
  3. Povuce views/likes/comments:
       - YouTube  -> zvanicni YouTube Data API v3 (besplatno)
       - TikTok   -> Apify actor "clockworks/tiktok-scraper"
       - Instagram-> Apify actor "apify/instagram-scraper"
  4. Upise brojeve nazad u sheet + timestamp

Env varijable (u GitHub Actions ih dodajes kao Secrets):
  SHEET_ID         - ID sheeta, deo URL-a izmedju /d/ i /edit
  GOOGLE_SA_JSON   - ceo sadrzaj service-account .json fajla (kao string)
  YOUTUBE_API_KEY  - API key sa console.cloud.google.com
  APIFY_TOKEN      - token sa console.apify.com -> Settings -> API tokens
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
# PODESAVANJE KOLONA — prilagodi ako ti tab "Videos" izgleda drugacije.
# Slova kolona u Google Sheetu (A=1, B=2, ...).
# ---------------------------------------------------------------------------
TAB_NAME = "Videos"
COL_LINK = "D"        # kolona sa linkom ka videu
COL_VIEWS = "F"       # views
COL_LIKES = "G"       # likes
COL_COMMENTS = "H"    # comments
COL_UPDATED = "K"     # "last updated" timestamp
FIRST_DATA_ROW = 2    # red 1 je header

YT_API = "https://www.googleapis.com/youtube/v3/videos"
APIFY_RUN = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
TIKTOK_ACTOR = "clockworks~tiktok-scraper"
IG_ACTOR = "apify~instagram-scraper"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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


def col_to_index(col: str) -> int:
    idx = 0
    for ch in col.upper():
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx


# ---------------------------------------------------------------------------
# Fetcheri
# ---------------------------------------------------------------------------
def fetch_youtube(urls: list[str], api_key: str) -> dict[str, dict]:
    """Vraca {url: {views, likes, comments}}. Batchuje po 50 ID-jeva."""
    id_map = {}  # video_id -> url
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
                "part": "statistics",
                "id": ",".join(batch),
                "key": api_key,
            },
            timeout=30,
        )
        r.raise_for_status()
        for item in r.json().get("items", []):
            stats = item.get("statistics", {})
            out[id_map[item["id"]]] = {
                "views": int(stats.get("viewCount", 0)),
                "likes": int(stats.get("likeCount", 0)),
                "comments": int(stats.get("commentCount", 0)),
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
        # normalizuj: uparuj po video ID-ju iz URL-a
        m = re.search(r"/video/(\d+)", url)
        vid = m.group(1) if m else None
        for original in urls:
            if vid and vid in original:
                out[original] = {
                    "views": int(item.get("playCount", 0)),
                    "likes": int(item.get("diggCount", 0)),
                    "comments": int(item.get("commentCount", 0)),
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
        # uparuj po shortcode-u (/reel/XXXX/ ili /p/XXXX/)
        m = re.search(r"/(?:reel|p)/([A-Za-z0-9_-]+)", url)
        code = m.group(1) if m else None
        for original in urls:
            if code and code in original:
                views = item.get("videoPlayCount") or item.get("videoViewCount") or 0
                out[original] = {
                    "views": int(views),
                    "likes": int(item.get("likesCount", 0)),
                    "comments": int(item.get("commentsCount", 0)),
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

    # GOOGLE_SA_JSON moze biti JSON string ili putanja do fajla
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

    link_col_idx = col_to_index(COL_LINK)
    links = ws.col_values(link_col_idx)  # ukljucuje header

    # {url: row_number}
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

    # Batch upis nazad u sheet
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

    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")

    print(f"Azurirano {len(results)}/{len(rows)} videa u {now}.")
    for e in errors:
        print(f"[greska] {e}")

    # Ne rusimo Action ako je samo deo failovao — brojevi koji su prosli su upisani
    return 0


if __name__ == "__main__":
    sys.exit(main())
