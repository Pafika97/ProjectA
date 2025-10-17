import asyncio
import hashlib
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import httpx
import yaml
import feedparser
from pydantic import BaseModel
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from dotenv import load_dotenv

load_dotenv(dotenv_path=".env", override=True)

# -----------------------------
# Config models
# -----------------------------
class TwitterCfg(BaseModel):
    enabled: bool = False
    users: List[str] = []
    max_per_user: int = 5

class FacebookCfg(BaseModel):
    enabled: bool = False
    pages: List[str] = []
    max_per_page: int = 5

class TruthCfg(BaseModel):
    enabled: bool = False
    users: List[str] = []
    max_per_user: int = 5

class RSSFeed(BaseModel):
    name: str
    url: str
    include_keywords: List[str] = []

class RSSCfg(BaseModel):
    enabled: bool = True
    feeds: List[RSSFeed] = []
    max_per_feed: int = 10
    initial_max_age_minutes: int = 1440

class RootCfg(BaseModel):
    interval_seconds: int = 120
    twitter: TwitterCfg = TwitterCfg()
    facebook: FacebookCfg = FacebookCfg()
    truth_social: TruthCfg = TruthCfg()
    rss: RSSCfg = RSSCfg()


# -----------------------------
# Storage
# -----------------------------
class Storage:
    def __init__(self, path="data.db"):
        self.conn = sqlite3.connect(path)
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS seen (
          source TEXT NOT NULL,
          item_id TEXT NOT NULL,
          ts INTEGER NOT NULL,
          PRIMARY KEY (source, item_id)
        )
        """)
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS offsets (
          source TEXT PRIMARY KEY,
          since_id TEXT
        )
        """)
        self.conn.commit()

    def seen(self, source: str, item_id: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM seen WHERE source=? AND item_id=?", (source, item_id))
        return cur.fetchone() is not None

    def mark_seen(self, source: str, item_id: str):
        self.conn.execute(
            "INSERT OR IGNORE INTO seen(source, item_id, ts) VALUES (?, ?, ?)",
            (source, item_id, int(time.time()))
        )
        self.conn.commit()

    def get_since_id(self, source: str) -> Optional[str]:
        cur = self.conn.execute("SELECT since_id FROM offsets WHERE source=?", (source,))
        row = cur.fetchone()
        return row[0] if row and row[0] else None

    def set_since_id(self, source: str, since_id: str):
        self.conn.execute(
            "INSERT INTO offsets(source, since_id) VALUES(?, ?) "
            "ON CONFLICT(source) DO UPDATE SET since_id=excluded.since_id",
            (source, since_id)
        )
        self.conn.commit()


# -----------------------------
# Telegram
# -----------------------------
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TG_TOKEN or not TG_CHAT_ID:
    raise SystemExit("Please set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in your .env")

bot = Bot(token=TG_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

async def tg_send(title: str, url: str, source: str, preview: Optional[str] = None):
    preview = (preview or "").strip()
    if preview:
        preview = preview[:500]
    text = f"<b>{title}</b>\\n{preview}\\n\\n<a href='{url}'>Open</a> — <i>{source}</i>"
    await bot.send_message(chat_id=TG_CHAT_ID, text=text, disable_web_page_preview=False)


# -----------------------------
# Helpers
# -----------------------------
def normalize_id(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()

def matches_keywords(text: str, keywords: List[str]) -> bool:
    t = text.lower()
    return any(kw.lower() in t for kw in keywords) if keywords else True

def parse_datetime(dt_str: str) -> Optional[datetime]:
    try:
        # feedparser already parses dates; we attempt a fallback here.
        return datetime(*feedparser._parse_date(dt_str)[:6], tzinfo=timezone.utc)
    except Exception:
        return None

# -----------------------------
# Pollers
# -----------------------------
async def poll_twitter(cfg: TwitterCfg, store: Storage):
    bearer = os.getenv("TWITTER_BEARER_TOKEN")
    if not cfg.enabled or not bearer:
        return
    headers = {"Authorization": f"Bearer {bearer}"}
    async with httpx.AsyncClient(timeout=20) as client:
        for handle in cfg.users:
            # 1) get user id
            r = await client.get(
                f"https://api.twitter.com/2/users/by/username/{handle}",
                headers=headers,
                params={"user.fields": "id"}
            )
            if r.status_code != 200:
                continue
            user_id = r.json().get("data", {}).get("id")
            if not user_id:
                continue

            since_key = f"twitter:{handle}"
            since_id = store.get_since_id(since_key)

            params = {
                "max_results": min(cfg.max_per_user, 100),
                "tweet.fields": "created_at",
                "exclude": "replies"
            }
            if since_id:
                params["since_id"] = since_id

            r2 = await client.get(
                f"https://api.twitter.com/2/users/{user_id}/tweets",
                headers=headers, params=params
            )
            if r2.status_code != 200:
                continue
            data = r2.json().get("data", [])
            # update since_id: highest id
            if data:
                newest_id = data[0]["id"]
                store.set_since_id(since_key, newest_id)

            for tw in reversed(data):  # oldest first to newest
                tid = tw["id"]
                url = f"https://x.com/{handle}/status/{tid}"
                uniq = normalize_id(url)
                if store.seen("twitter", uniq):
                    continue
                title = f"New post from @{handle}"
                await tg_send(title, url, "Twitter/X", preview=tw.get("text", ""))
                store.mark_seen("twitter", uniq)


async def poll_facebook(cfg: FacebookCfg, store: Storage):
    if not cfg.enabled:
        return
    token = os.getenv("FB_ACCESS_TOKEN")
    if not token:
        app_id = os.getenv("FB_APP_ID")
        app_secret = os.getenv("FB_APP_SECRET")
        if app_id and app_secret:
            token = f"{app_id}|{app_secret}"
    if not token:
        return

    async with httpx.AsyncClient(timeout=20) as client:
        for page in cfg.pages:
            params = {
                "access_token": token,
                "fields": "message,permalink_url,created_time",
                "limit": min(cfg.max_per_page, 25)
            }
            r = await client.get(f"https://graph.facebook.com/v19.0/{page}/posts", params=params)
            if r.status_code != 200:
                continue
            items = r.json().get("data", [])
            for it in reversed(items):
                url = it.get("permalink_url")
                if not url:
                    continue
                uniq = normalize_id(url)
                if store.seen("facebook", uniq):
                    continue
                msg = it.get("message", "")
                await tg_send(f"New post from Facebook/{page}", url, "Facebook", preview=msg)
                store.mark_seen("facebook", uniq)


async def poll_truth_social(cfg: TruthCfg, store: Storage):
    # Best-effort Mastodon-compatible polling. May *not* work for Truth Social.
    if not cfg.enabled:
        return
    base = os.getenv("MASTODON_BASE_URL")
    token = os.getenv("MASTODON_ACCESS_TOKEN")
    if not base or not token:
        return
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=20) as client:
        for user in cfg.users:
            # resolve account (Mastodon-style)
            r = await client.get(f"{base}/api/v1/accounts/lookup", params={"acct": user}, headers=headers)
            if r.status_code != 200:
                continue
            acct = r.json()
            acct_id = acct.get("id")
            if not acct_id:
                continue
            r2 = await client.get(f"{base}/api/v1/accounts/{acct_id}/statuses", params={"limit": min(cfg.max_per_user, 40)}, headers=headers)
            if r2.status_code != 200:
                continue
            statuses = r2.json()
            for st in reversed(statuses):
                url = st.get("url") or st.get("uri")
                if not url:
                    continue
                uniq = normalize_id(url)
                if store.seen("truth", uniq):
                    continue
                content = st.get("content", "")
                # strip simple HTML tags
                preview = content.replace("<p>", " ").replace("</p>", " ").replace("<br>", " ").replace("<br/>", " ")
                await tg_send(f"New post from Truth Social/{user}", url, "Truth Social*", preview=preview)
                store.mark_seen("truth", uniq)


async def poll_rss(cfg: RSSCfg, store: Storage):
    if not cfg.enabled:
        return
    now = datetime.now(timezone.utc)
    initial_cutoff = now - timedelta(minutes=cfg.initial_max_age_minutes)
    for feed in cfg.feeds:
        try:
            d = feedparser.parse(feed.url)
        except Exception:
            continue
        # Newest last
        entries = d.entries[: cfg.max_per_feed]
        for entry in reversed(entries):
            url = entry.get("link") or entry.get("id") or ""
            title = entry.get("title", "Untitled")
            summary = entry.get("summary", "")
            if not url:
                # create a stable id from title+published
                url = f"{feed.url}#fallback-{hashlib.md5(title.encode()).hexdigest()}"
            uniq = normalize_id(url)
            if store.seen(feed.name, uniq):
                continue
            # keyword filter
            blob = f"{title}\\n{summary}"
            if not matches_keywords(blob, feed.include_keywords):
                continue
            # age filter on first run
            pubdt = None
            for key in ("published", "updated", "created"):
                if getattr(entry, key, None):
                    pubdt = parse_datetime(getattr(entry, key))
                    if pubdt:
                        break
            if pubdt and pubdt < initial_cutoff and store.get_since_id(f"rss:{feed.name}") is None:
                # Ignore very old items only on the first run
                continue
            await tg_send(f"{title}", url, feed.name, preview=summary)
            store.mark_seen(feed.name, uniq)
        # mark that we've done at least one run
        store.set_since_id(f"rss:{feed.name}", datetime.utcnow().isoformat())


# -----------------------------
# Main loop
# -----------------------------
async def run():
    # Load config
    with open("sources.yaml", "r", encoding="utf-8") as f:
        cfg = RootCfg(**yaml.safe_load(f))
    store = Storage()
    interval = max(30, cfg.interval_seconds)  # guard lower bound

    while True:
        try:
            tasks = []
            if cfg.twitter.enabled:
                tasks.append(poll_twitter(cfg.twitter, store))
            if cfg.facebook.enabled:
                tasks.append(poll_facebook(cfg.facebook, store))
            if cfg.truth_social.enabled:
                tasks.append(poll_truth_social(cfg.truth_social, store))
            if cfg.rss.enabled:
                tasks.append(poll_rss(cfg.rss, store))
            if tasks:
                await asyncio.gather(*tasks)
        except Exception as e:
            # Optional: log to Telegram
            try:
                await tg_send("Bot error", "https://localhost/", "System", preview=str(e))
            except Exception:
                pass
        await asyncio.sleep(interval)

if __name__ == "__main__":
    asyncio.run(run())