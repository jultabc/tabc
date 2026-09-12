#!/usr/bin/env python3
"""Generate a self-contained tabc inbox dashboard.

Output: TABC_DASHBOARD_OUT, or mailbox.html under the state directory.
Run: python3 scripts/mailbox_dashboard.py

The page contains a snapshot, not live browser requests. tabd does not provide
CORS headers and returns 501 for OPTIONS. This avoids extending browser access
to the local server. Only sender, subject, and pending counts are displayed.
The generator queries /who and /mailbox; it does not acknowledge messages.
Message bodies, keys, and request signatures are not included in the HTML.
"""
import html
import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tabus import nodekey, paths  # noqa: E402

BASE = os.environ.get("TABC_BUS_URL", "http://127.0.0.1:8765")
# 🔴 The dashboard authenticates like any client: it signs each (read-only) request
#    with its own node key. Register this node once so tabd holds its pubkey. There is
#    no shared token any more — and a signature transmits no secret to leak into the HTML.
NODE = os.environ.get("TABC_NODE", "dashboard")
# 🔴 The default follows TABC_HOME rather than naming a directory: a path that
#    exists on the machine it was written on is not a default, it is a local
#    setting that happens to work there. TABC_DASHBOARD_OUT overrides it.
OUT = os.environ.get("TABC_DASHBOARD_OUT") or paths.state("mailbox.html")


def get(path):
    ts = str(int(time.time()))
    sig = nodekey.b58encode(
        nodekey.sign(
            nodekey.canonical_request(NODE, "GET", path, "", ts), nodekey.key_path(NODE)
        )
    )
    req = urllib.request.Request(BASE + path)
    req.add_header("X-Node", NODE)
    req.add_header("X-Node-Ts", ts)
    req.add_header("X-Node-Sig", sig)
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.loads(res.read().decode("utf-8"))


def collect():
    """Return pending counts and unread subjects per node; propagate failures."""
    nodes = get("/who")["nodes"]
    out = []
    for n in sorted(nodes, key=lambda x: (-x.get("pending", 0), x["node_id"])):
        pending = n.get("pending", 0)
        subjects = []
        if pending:
            subjects = get(f"/mailbox?node={n['node_id']}&limit=10").get("unread", [])
        out.append(
            {
                "node": n["node_id"],
                "kind": n.get("kind", ""),
                "pending": pending,
                "beat": (n.get("last_heartbeat_at") or "")[:16].replace("T", " "),
                "subjects": subjects,
            }
        )
    return out


def render(rows, at):
    e = html.escape
    total = sum(r["pending"] for r in rows)
    waiting = [r for r in rows if r["pending"]]

    cards = []
    for r in waiting:
        items = "".join(
            f'<li><span class="subj">{e(s.get("subject", "(no subject)"))}</span>'
            f'<span class="from">{e(s.get("from", "?"))}</span></li>'
            for s in r["subjects"]
        )
        more = r["pending"] - len(r["subjects"])
        if more > 0:
            items += f'<li class="more">… {more} more</li>'
        cards.append(
            f'<section class="card"><h2>{e(r["node"])}'
            f'<span class="badge">{r["pending"]}</span></h2>'
            f'<ul>{items}</ul></section>'
        )
    if not waiting:
        cards.append('<section class="card empty">No unread messages across all nodes</section>')

    quiet = "".join(
        f'<span class="chip">{e(r["node"])}</span>' for r in rows if not r["pending"]
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>tabc dm · tac</title>
<style>
:root {{ --bg:#f7f7f8; --fg:#1a1a1a; --muted:#6b6b70; --line:#e2e2e5; --card:#fff; --accent:#c2410c; }}
@media (prefers-color-scheme:dark) {{
  :root {{ --bg:#141416; --fg:#ececed; --muted:#9a9aa0; --line:#2c2c30; --card:#1d1d20; --accent:#fb923c; }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:2rem 1.25rem; background:var(--bg); color:var(--fg);
  font:15px/1.6 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo",sans-serif; }}
.wrap {{ max-width:760px; margin:0 auto; }}
h1 {{ font-size:1.35rem; margin:0 0 .25rem; }}
.sub {{ color:var(--muted); font-size:.85rem; margin-bottom:1.5rem; }}
.total {{ font-size:2.5rem; font-weight:600; color:var(--accent); line-height:1; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:1rem 1.15rem; margin-bottom:.85rem; }}
.card h2 {{ font-size:1rem; margin:0 0 .6rem; display:flex; align-items:center; gap:.5rem; }}
.badge {{ background:var(--accent); color:#fff; border-radius:999px;
  padding:.05rem .5rem; font-size:.75rem; }}
.card ul {{ list-style:none; margin:0; padding:0; }}
.card li {{ padding:.35rem 0; border-top:1px solid var(--line);
  display:flex; justify-content:space-between; gap:1rem; }}
.card li:first-child {{ border-top:none; }}
.subj {{ flex:1; min-width:0; overflow-wrap:anywhere; }}
.from {{ color:var(--muted); font-size:.8rem; white-space:nowrap; }}
.more, .empty {{ color:var(--muted); font-size:.85rem; }}
.chips {{ margin-top:1.25rem; }}
.chip {{ display:inline-block; border:1px solid var(--line); border-radius:999px;
  padding:.1rem .6rem; margin:0 .3rem .3rem 0; font-size:.8rem; color:var(--muted); }}
footer {{ margin-top:2rem; color:var(--muted); font-size:.78rem;
  border-top:1px solid var(--line); padding-top:.9rem; }}
</style></head><body><div class="wrap">
<h1>tabc dm · tac</h1>
<div class="sub">Generated {at} · {len(rows)} nodes observed</div>
<div class="total">{total}<span style="font-size:1rem;font-weight:400"> pending</span></div>
<div style="height:1.25rem"></div>
{"".join(cards)}
<div class="chips">Quiet nodes {quiet or "<span class='chip'>none</span>"}</div>
<footer>
Read-only snapshot. This page does not call tabd.<br>
Run <code>python3 scripts/mailbox_dashboard.py</code> again to refresh. No automatic refresh.<br>
Message bodies are excluded; only senders, subjects, and pending counts are shown.
</footer>
</div></body></html>
"""


def main():
    at = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M KST")
    rows = collect()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(render(rows, at))
    total = sum(r["pending"] for r in rows)
    print(f"Generated: {OUT}")
    print(f"  {len(rows)} nodes · {total} pending")


if __name__ == "__main__":
    main()
