#!/usr/bin/env python3
import sqlite3
from datetime import datetime, timezone, timedelta
from flask import Flask, g, request

APP_TITLE = "Reddit Archive Dashboard"
DEFAULT_DB = "reddit_archiver.sqlite"

app = Flask(__name__)

def db_path():
    return request.args.get("db", DEFAULT_DB)

def get_db():
    if "db" not in g:
        # Read-only open (safer): requires sqlite URI mode
        path = db_path()
        uri = f"file:{path}?mode=ro"
        g.db = sqlite3.connect(uri, uri=True, timeout=5)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def count_where(db, where_sql="", params=()):
    q = "SELECT COUNT(*) AS n FROM posts"
    if where_sql:
        q += " WHERE " + where_sql
    return db.execute(q, params).fetchone()["n"]

@app.get("/")
def index():
    db = get_db()

    # Simple stats
    total = count_where(db)
    wayback_ok = count_where(db, "(wayback_www IS NOT NULL OR wayback_old IS NOT NULL)")
    atoday_ok = count_where(db, "(atoday_www IS NOT NULL OR atoday_old IS NOT NULL)")
    both_services_ok = count_where(
        db,
        "((wayback_www IS NOT NULL OR wayback_old IS NOT NULL) AND (atoday_www IS NOT NULL OR atoday_old IS NOT NULL))"
    )

    # “Last 24h” based on created_utc if present, else inserted_at
    since = int((datetime.now(timezone.utc) - timedelta(hours=24)).timestamp())
    last24 = count_where(db, "(created_utc IS NOT NULL AND created_utc >= ?) OR (created_utc IS NULL AND inserted_at >= ?)",
                         (since, (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()))

    # Pagination
    page = max(int(request.args.get("page", "1")), 1)
    per_page = min(max(int(request.args.get("per_page", "50")), 10), 200)
    offset = (page - 1) * per_page

    rows = db.execute(
        """SELECT
             reddit_id, subreddit, created_utc, inserted_at,
             title, reddit_url, url_www, url_old,
             wayback_www, wayback_old, atoday_www, atoday_old,
             last_error
           FROM posts
           ORDER BY inserted_at DESC
           LIMIT ? OFFSET ?""",
        (per_page, offset),
    ).fetchall()

    def fmt_time(r):
        if r["created_utc"]:
            return datetime.fromtimestamp(r["created_utc"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return (r["inserted_at"] or "")[:16].replace("T", " ")

    def status_for(url):
        return "ok" if url else "pending"

    html = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>{APP_TITLE}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 18px; }}
    .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; margin-bottom: 14px; }}
    .card {{ border: 1px solid #ddd; border-radius: 10px; padding: 10px 12px; }}
    .k {{ color: #666; font-size: 12px; }}
    .v {{ font-size: 20px; font-weight: 700; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; padding: 8px; border-bottom: 1px solid #eee; vertical-align: top; }}
    th {{ position: sticky; top: 0; background: #fff; }}
    .title a {{ text-decoration: none; }}
    .pill {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; border: 1px solid #ddd; }}
    .ok {{ border-color: #2e7d32; color: #2e7d32; }}
    .pending {{ border-color: #999; color: #666; }}
    .err {{ color: #b71c1c; font-size: 12px; max-width: 540px; }}
    .muted {{ color: #666; font-size: 12px; }}
    .nav {{ margin: 12px 0; display: flex; gap: 10px; align-items: center; }}
    .nav a {{ text-decoration: none; border: 1px solid #ddd; padding: 6px 10px; border-radius: 8px; }}
  </style>
</head>
<body>
  <h2 style="margin: 0 0 10px 0;">{APP_TITLE}</h2>
  <div class="muted">LAN dashboard. Data is read-only from <code>{db_path()}</code>.</div>

  <div class="stats">
    <div class="card"><div class="k">Total posts tracked</div><div class="v">{total}</div></div>
    <div class="card"><div class="k">Wayback success (any view)</div><div class="v">{wayback_ok}</div></div>
    <div class="card"><div class="k">Archive.today success (any view)</div><div class="v">{atoday_ok}</div></div>
    <div class="card"><div class="k">Both services got it</div><div class="v">{both_services_ok}</div></div>
    <div class="card"><div class="k">Seen in last 24h</div><div class="v">{last24}</div></div>
  </div>

  <div class="nav">
    <a href="/?page={max(page-1,1)}&per_page={per_page}">◀ Prev</a>
    <div class="muted">Page {page} (showing {per_page}/page)</div>
    <a href="/?page={page+1}&per_page={per_page}">Next ▶</a>
  </div>

  <table>
    <thead>
      <tr>
        <th>Time</th>
        <th>Post</th>
        <th>Wayback</th>
        <th>Archive.today</th>
        <th>Notes</th>
      </tr>
    </thead>
    <tbody>
    """

    for r in rows:
        time_str = fmt_time(r)
        title = (r["title"] or "(no title)").replace("<", "&lt;").replace(">", "&gt;")
        post_link = r["reddit_url"] or r["url_www"]
        sub = r["subreddit"] or ""

        def link_or_dash(u):
            if not u:
                return "—"
            safe = u.replace("<", "&lt;").replace(">", "&gt;")
            return f'<a href="{safe}" target="_blank" rel="noreferrer">open</a>'

        wb_www = r["wayback_www"]
        wb_old = r["wayback_old"]
        at_www = r["atoday_www"]
        at_old = r["atoday_old"]

        wb_status = "ok" if (wb_www or wb_old) else "pending"
        at_status = "ok" if (at_www or at_old) else "pending"

        err = (r["last_error"] or "").replace("<", "&lt;").replace(">", "&gt;")

        html += f"""
        <tr>
          <td class="muted">{time_str}<br><span class="muted">r/{sub}</span></td>
          <td class="title">
            <a href="{post_link}" target="_blank" rel="noreferrer"><strong>{title}</strong></a><br>
            <span class="muted">views:</span>
            <a href="{r["url_www"]}" target="_blank" rel="noreferrer">www</a> ·
            <a href="{r["url_old"]}" target="_blank" rel="noreferrer">old</a>
          </td>
          <td>
            <span class="pill {wb_status}">{'✓' if wb_status=='ok' else '…'} {wb_status}</span><br>
            <span class="muted">www:</span> {link_or_dash(wb_www)} ·
            <span class="muted">old:</span> {link_or_dash(wb_old)}
          </td>
          <td>
            <span class="pill {at_status}">{'✓' if at_status=='ok' else '…'} {at_status}</span><br>
            <span class="muted">www:</span> {link_or_dash(at_www)} ·
            <span class="muted">old:</span> {link_or_dash(at_old)}
          </td>
          <td>
            {"<div class='err'>" + err + "</div>" if err else "<span class='muted'>—</span>"}
          </td>
        </tr>
        """

    html += """
    </tbody>
  </table>
</body>
</html>
"""
    return html

if __name__ == "__main__":
    # Dev server is fine on LAN if you firewall it.
    app.run(host="0.0.0.0", port=8080, debug=False)
