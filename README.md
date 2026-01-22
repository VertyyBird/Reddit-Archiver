# Reddit RSS Archiver + LAN Dashboard

A small personal service that:
- watches one or more subreddits via RSS
- detects new Reddit post permalinks (the post URL, not an external article link)
- submits both www.reddit.com and old.reddit.com versions to archiving services
- stores everything in a SQLite DB
- serves a simple LAN dashboard so you can browse results from another device

Built for set-and-forget home use on Linux.

## What it does

For each new post on a subreddit’s /new.rss feed, the script:
1. extracts the Reddit post ID (from /comments/<id>/)
2. constructs two viewing URLs:
   - https://www.reddit.com/...
   - https://old.reddit.com/...
3. submits both URLs to:
   - Wayback Machine (web.archive.org)
   - Archive.today variant (archive.vn, best-effort)
4. writes rows into SQLite with:
   - title, timestamps, original URLs
   - archive URLs (if obtained)
   - status + errors
5. periodically performs verification for Wayback:
   - calls the Wayback Availability API
   - only marks ok=1 if it finds an archived snapshot whose timestamp is at or after the submission timestamp

The script can also run a lightweight dashboard web server in the same process, so you can check what’s archived, what’s pending, and what failed.

## Features

- Multiple subreddits from a config file, one per line
- RSS-only (no Reddit OAuth needed)
- Archives both www and old Reddit views for preference
- SQLite storage with automatic schema migration
- Wayback verification via availability lookups
- LAN dashboard (HTML) + JSON API endpoint
- Exports a small latest_archives.json snapshot for easy integrations

## Requirements

- Python 3.10+
- Packages:
  - requests
  - feedparser

On Debian/Ubuntu/Mint:
```bash
sudo apt update
sudo apt install -y python3 python3-pip
pip3 install requests feedparser
````

## Config file

The config file is coded as `subreddits.conf`

You can make it as simple as:

```text
ChatGPT
OhNoConsequences
Wellthatsucks
```

Or add optional settings using `key=value` anywhere:

```text
# Dashboard
dashboard_enabled=true
dashboard_host=0.0.0.0
dashboard_port=8080

# Polling
interval=180
scan_limit=25

# Output
out_json=latest_archives.json
json_limit=50

# Services
do_wayback=true
do_archive_today=true

# Rate limiting
delay_wayback=5
delay_atoday=8

# Wayback verification tuning
verify_batch=40
verify_min_age=60
verify_recheck_interval=900

# Subreddits (one per line)
ChatGPT
Wellthatsucks
```

### Key settings explained

* `interval`: seconds between polling cycles
* `scan_limit`: how many RSS entries to scan per subreddit per cycle
* `delay_wayback`, `delay_atoday`: delays between service submissions
* `verify_min_age`: wait this many seconds after submitting before checking availability
* `verify_recheck_interval`: minimum seconds between verification attempts per leg
* `dashboard_host`:

  * 127.0.0.1 = local only
  * 0.0.0.0 = accessible on LAN (firewall permitting)

## Running this script

One cycle then exit:

```bash
python3 reddit_archiver.py --config subreddits.conf --once
```

Run continuously:

```bash
python3 reddit_archiver.py --config subreddits.conf
```

If the dashboard is enabled, you’ll see something like:

```text
[dashboard] Serving on http://0.0.0.0:8080/
```

Open on your LAN device:

* http://<your-PC-LAN-IP>:8080/

## DB migrate only command

The script auto-migrates on startup, but if you added a dedicated flag, you can run migrations and exit:

```bash
python3 reddit_archiver.py --db reddit_archiver.sqlite --db-migrate
```

You can also inspect the schema manually:

```bash
sqlite3 reddit_archiver.sqlite "PRAGMA table_info(posts);"
```

## Dashboard endpoints

* GET /
  Human-readable HTML dashboard.

* GET /api/latest.json?limit=200
  A JSON list of latest rows.

The HTML dashboard shows:

* post title and links (www + old)
* per-service status
* archive links if available
* error strings


## How Wayback verification works

Wayback Save Page Now is a submission. It does not always immediately give you a final snapshot URL.

So after submit, we periodically call:

* [https://archive.org/wayback/available?url=](https://archive.org/wayback/available?url=)<url>&timestamp=<submit_ts>

We treat it as verified ok only if:

* available is true, and
* the returned snapshot timestamp is greater than or equal to submit_ts

This avoids false positives where the API returns an old archived capture from months ago.

## Running as a systemd service

Example service file: /etc/systemd/system/reddit-archiver.service

```ini
[Unit]
Description=Reddit RSS Archiver + Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/your/project
ExecStart=/usr/bin/python3 /path/to/your/project/reddit_archiver.py --config /path/to/your/project/subreddits.conf
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now reddit-archiver.service
sudo systemctl status reddit-archiver.service
```

Logs:

```bash
journalctl -u reddit-archiver.service -f
```

## Troubleshooting

Dashboard not reachable from another device:

* set dashboard_host=0.0.0.0
* allow the TCP port (8080 by default) in your firewall
* confirm your LAN IP:

  ```bash
  ip a
  ```

Wayback shows pending for a while:

* normal. Wayback can take time to process. The script will keep rechecking.

Archive.today blocked:

* normal. The script will record captcha/Cloudflare blocks as errors.

Nothing is being archived:

* check that RSS is reachable and not blocked by network/DNS
* try:

  ```bash
  curl -A "reddit-rss-archiver" https://www.reddit.com/r/ChatGPT/new.rss
  ```

## Roadmap ideas

* search/filter in dashboard (by subreddit, title keywords)
* retry logic for failed legs with backoff
* per-subreddit enable/disable without deleting lines
* export CSV endpoint
* make the dashboard refresh automaticall
* make the dashboard able to edit the config, especially to add or remove subreddits
