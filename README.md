# Telegram News Bot (Twitter/X, Facebook, Truth Social*, RSS for Reuters/Bloomberg/etc.)

**Goal:** push the newest posts about your topic into Telegram.

## Quick start

1) Python 3.10+ recommended.
2) `pip install -r requirements.txt`
3) Copy `.env.example` → `.env` and fill tokens.
4) Edit `sources.yaml` (enable/disable sources, add users/feeds/keywords).
5) Run: `python main.py`

### Notes on sources

- **Twitter/X:** Requires a paid API v2 token. Set `TWITTER_BEARER_TOKEN` and enable in `sources.yaml`.
- **Facebook:** Use a Page access token (long-lived) if possible. Put it in `FB_ACCESS_TOKEN` and enable.
- **Truth Social:** There is no stable public API. If your account/server is Mastodon-compatible and you have an access token, set `MASTODON_BASE_URL` and `MASTODON_ACCESS_TOKEN` and try enabling. This may or may not work.
- **Reuters/Bloomberg/aggregators:** Use their RSS feeds with keyword filters. Add the exact RSS URLs you need.

### Dedup & persistence
Uses a SQLite DB (`data.db`) to store seen IDs and since_ids (per source). Safe to restart.

### Disclaimer
Respect each platform's Terms of Service and developer policies. This repo avoids scraping.
