#!/usr/bin/env python3
"""
AIOS dashboard generator.

Reads accessible state across the AIOS surfaces and emits:
  ~/Desktop/dashboard/index.html
  ~/Desktop/dashboard/_data/snapshot-YYYY-MM-DDTHHMM.json

Five sections, mapped to the five questions Mike asks at a glance:
  Q1 status      | rolled-up traffic light + reasons
  Q2 schedule    | per-task fired/due/health for the critical scheduled tasks
  Q3 action      | items waiting on Mike (approvals, audits, todos)
  Q4 mote ops    | what is happening in the business today
  Q5 trends      | small numeric strip

Empty panels collapse to a single zero-line.

This script is intentionally dependency-free (stdlib only). It runs anywhere
Python 3.9+ is available. It is invoked by the dashboard-publish-hourly
scheduled task.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from html import escape

# Anchor all paths on the script's own location rather than expanduser("~").
# In the Cowork sandbox, $HOME is /sessions/<id> (NOT the mounted Desktop), so
# "~/Desktop/dashboard" silently resolved to a phantom in-sandbox directory and
# every automated run wrote index.html/snapshots to the wrong filesystem. The
# script always lives in <Desktop>/dashboard/, so deriving DASH from __file__
# is correct on both the host and the sandbox.
DASH = pathlib.Path(__file__).resolve().parent
DESKTOP = DASH.parent
HOME = DESKTOP.parent
DATA_DIR = DASH / "_data"
INDEX = DASH / "index.html"

PDT = timezone(timedelta(hours=-7))   # PDT; PST swap not handled, fine for now


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_pdt() -> datetime:
    return now_utc().astimezone(PDT)


def read_text(path: pathlib.Path, limit: int | None = None) -> str | None:
    try:
        s = path.read_text(encoding="utf-8", errors="replace")
        return s if limit is None else s[:limit]
    except FileNotFoundError:
        return None
    except OSError:
        return None


def read_json(path: pathlib.Path) -> dict | None:
    s = read_text(path)
    if s is None:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


def latest(dir_path: pathlib.Path, glob: str) -> pathlib.Path | None:
    try:
        items = sorted(dir_path.glob(glob))
    except OSError:
        return None
    return items[-1] if items else None


def rel_age(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    delta = now_utc() - ts
    secs = delta.total_seconds()
    if secs < 0:
        return f"in {fmt_secs(-secs)}"
    return f"{fmt_secs(secs)} ago"


def fmt_secs(secs: float) -> str:
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs}h"
    days = hrs // 24
    return f"{days}d"


# ---------- data collectors ----------

def collect_scheduled_tasks() -> dict:
    """
    Prefer the sidecar JSON written by the publish skill itself, which calls
    mcp__scheduled-tasks__list_scheduled_tasks before invoking this generator.
    If absent (running build.py standalone), fall back to enumerating
    SKILL.md files under ~/Documents/Claude/Scheduled with no timing info.
    """
    sidecar = DATA_DIR / "scheduled-tasks.json"
    sidecar_data = read_json(sidecar)
    if isinstance(sidecar_data, list):
        tasks = []
        for t in sidecar_data:
            if not isinstance(t, dict):
                continue
            tasks.append({
                "taskId": t.get("taskId"),
                "lastRunAt": t.get("lastRunAt"),
                "nextRunAt": t.get("nextRunAt"),
                "enabled": t.get("enabled", True),
                "description": t.get("description"),
            })
        return {"available": True, "tasks": tasks, "source": "mcp-sidecar"}

    sched_root = HOME / "Documents" / "Claude" / "Scheduled"
    if not sched_root.exists():
        return {"available": False, "tasks": [], "source": "none"}
    tasks = []
    for d in sorted(sched_root.iterdir()):
        if not d.is_dir():
            continue
        if not (d / "SKILL.md").exists():
            continue
        tasks.append({
            "taskId": d.name,
            "lastRunAt": None, "nextRunAt": None, "enabled": True,
        })
    return {"available": True, "tasks": tasks, "source": "fs-walk"}


# Critical tasks — name -> human label, target cadence in seconds (used to
# flag stale fires).
CRITICAL = [
    ("news-scout",                   "news-scout",               60 * 60 * 30),
    ("intel-weekly",                 "intel-weekly",             60 * 60 * 24 * 9),
    ("dashboard-publish-hourly",     "dashboard-publish",        60 * 60 * 2),
    ("trading-bot-tool-audit-daily", "trading-bot tool audit",   60 * 60 * 30),
    ("plan-mode-compliance-daily",   "plan-mode compliance",     60 * 60 * 30),
    ("memory-tool-write-audit-daily","memory-tool write audit",  60 * 60 * 30),
    ("budget-dashboard-sync",        "budget-dashboard sync",    60 * 60 * 14),
    ("moteops-vp-tracking",          "VP tracking",              60 * 60 * 30),
    ("moteops-vp-content",           "VP content",               60 * 60 * 24 * 4),
    ("moteops-vp-briefing",          "VP briefing",              60 * 60 * 24 * 9),
    ("moteops-director-weekly",      "marketing director",       60 * 60 * 24 * 9),
    ("audit-learning-report",        "learning-report audit",    60 * 60 * 26),
    ("checkpoint",                   "checkpoint",               60 * 60 * 30),
    ("runtime-state-scan",           "runtime-state scan",       60 * 60 * 30),
]


def collect_trading_health() -> dict:
    h = read_json(DESKTOP / "mikes-trading-bot" / "data" / "health.json") or {}
    stats = h.get("stats", {}) if isinstance(h, dict) else {}
    return {
        "status": h.get("status"),
        "timestamp": h.get("timestamp"),
        "total_pnl_pct": stats.get("total_pnl_pct"),
        "num_positions": stats.get("num_positions"),
    }


def collect_marketing_health() -> dict:
    mk = DESKTOP / "mote-ops" / "marketing"
    today = now_pdt().strftime("%Y-%m-%d")
    hb = mk / f"heartbeat-{today}.jsonl"
    if not hb.exists():
        # latest heartbeat we have
        hb = latest(mk, "heartbeat-*.jsonl")
    agents = {}
    last_ts = None
    if hb and hb.exists():
        for line in hb.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            a = row.get("agent")
            ts = row.get("ts")
            if not a:
                continue
            prev = agents.get(a)
            if not prev or (ts and ts > prev.get("ts", "")):
                agents[a] = row
                if ts and (not last_ts or ts > last_ts):
                    last_ts = ts

    # action-inbox today (any priority)
    inbox = mk / "vps" / "tracking" / f"action-inbox-{today}.json"
    if not inbox.exists():
        inbox = latest(mk / "vps" / "tracking", "action-inbox-*.json")
    inbox_items = []
    if inbox and inbox.exists():
        data = read_json(inbox) or {}
        inbox_items = data.get("items", []) if isinstance(data, dict) else []

    p0 = sum(1 for i in inbox_items if i.get("priority") == "P0")
    p1 = sum(1 for i in inbox_items if i.get("priority") == "P1")

    return {
        "heartbeat_path": str(hb) if hb else None,
        "agents_seen": sorted(agents.keys()),
        "agent_count": len(agents),
        "last_heartbeat_ts": last_ts,
        "inbox_p0": p0,
        "inbox_p1": p1,
        "inbox_items": inbox_items,
    }


def collect_findings() -> dict:
    findings_dir = DESKTOP / "_audit" / "findings"
    items = []
    if findings_dir.exists():
        for f in sorted(findings_dir.glob("*.md")):
            text = read_text(f, limit=4000) or ""
            # status defaults open; honour 'status: flag' / '**Status:** FLAG'
            m = re.search(r"^[\s*_]*status[\s*_]*[:=][\s*]*(\w+)",
                          text, re.I | re.M)
            status = (m.group(1).lower() if m else "open")
            verdict_m = re.search(r"^[\s*_]*verdict[\s*_]*[:=][\s*]*(.+)$",
                                  text, re.I | re.M)
            verdict = verdict_m.group(1).strip().rstrip("*").strip() if verdict_m else ""
            summary_m = re.search(r"^[\s*_]*summary[\s*_]*[:=][\s*]*(.+)$",
                                  text, re.I | re.M)
            summary = summary_m.group(1).strip().rstrip("*").strip() if summary_m else ""
            # if no summary header, take first non-heading line after status
            if not summary:
                for line in text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("**"):
                        continue
                    if line.startswith("---"):
                        continue
                    summary = line[:140]
                    break
            items.append({
                "name": f.stem,
                "path": f"file://{f}",
                "status": status,
                "verdict": verdict,
                "summary": summary,
            })
    open_count = sum(1 for i in items if i["status"] == "open")
    flag_count = sum(1 for i in items if i["status"] == "flag")
    return {"items": items, "open": open_count, "flag": flag_count}


def collect_news_headline() -> dict:
    p = DESKTOP / "news-scout" / "last_digest.txt"
    s = read_text(p)
    if not s:
        return {"headline": None, "ts": None}
    headline = None
    # find first numbered line: "1) ..."
    for line in s.splitlines():
        m = re.match(r"^\s*\d+\)\s*(.+)$", line)
        if m:
            headline = m.group(1).strip()
            break
    try:
        ts = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        ts = None
    return {"headline": headline, "ts": ts}


def collect_action_list() -> dict:
    p = DESKTOP / "AIOS-action-list.md"
    s = read_text(p)
    if not s:
        return {"total": 0, "urgent": 0, "items": []}
    # count numbered items
    nums = re.findall(r"^\s*\d+\.\s+(.+)$", s, re.M)
    urgent_match = re.search(
        r"##\s*Tonight or tomorrow[^\n]*\n(.*?)(?=^##|\Z)",
        s, re.M | re.S,
    )
    urgent_lines = []
    if urgent_match:
        urgent_lines = re.findall(r"^\s*\d+\.\s+(.+)$", urgent_match.group(1), re.M)
    return {
        "total": len(nums),
        "urgent": len(urgent_lines),
        "urgent_items": urgent_lines[:5],
    }


def collect_checkpoint() -> dict:
    audit = DESKTOP / "_audit"
    cp = latest(audit, "checkpoint-*.log")
    rs = latest(audit, "runtime-state-scan-*.log")
    out = {"checkpoint_path": None, "checkpoint_summary": None,
           "runtime_path": None, "runtime_summary": None}
    if cp:
        out["checkpoint_path"] = str(cp)
        m = re.search(r"Repos scanned:\s*(\d+)", cp.read_text(errors="replace"))
        out["checkpoint_summary"] = (
            f"{m.group(1)} repos" if m else cp.name
        )
    if rs:
        out["runtime_path"] = str(rs)
        txt = rs.read_text(errors="replace")
        miss = re.search(r"🔴 MISSING \((\d+)\)", txt)
        extra = re.search(r"🟠 EXTRA \((\d+)\)", txt)
        mis = re.search(r"🟡 MISMATCH \((\d+)\)", txt)
        stale = re.search(r"STALE \((\d+)\)", txt)
        healthy = re.search(r"HEALTHY \((\d+)\)", txt)
        bits = []
        for label, m in [("missing", miss), ("extra", extra), ("mismatch", mis),
                         ("stale", stale), ("healthy", healthy)]:
            if m:
                bits.append(f"{m.group(1)} {label}")
        out["runtime_summary"] = ", ".join(bits) if bits else rs.name
    return out


# ---------- status roll-up ----------

def roll_up(trading, marketing, findings, sched) -> tuple[str, str]:
    reasons = []
    worst = "green"
    rank = {"green": 0, "yellow": 1, "red": 2}

    if trading.get("status") and trading["status"].upper() not in ("OK", "HEALTHY"):
        reasons.append(f"trading {trading['status'].lower()}")
        worst = max(worst, "yellow", key=rank.get)
    elif not trading.get("status"):
        reasons.append("trading health missing")
        worst = max(worst, "yellow", key=rank.get)

    if marketing["inbox_p0"]:
        reasons.append(f"{marketing['inbox_p0']} marketing P0")
        worst = max(worst, "red", key=rank.get)
    elif marketing["inbox_p1"]:
        reasons.append(f"{marketing['inbox_p1']} marketing P1")
        worst = max(worst, "yellow", key=rank.get)

    if findings["open"]:
        reasons.append(f"{findings['open']} open findings")
        worst = max(worst, "red", key=rank.get)
    elif findings["flag"]:
        reasons.append(f"{findings['flag']} flag")
        worst = max(worst, "yellow", key=rank.get)

    # stale scheduled tasks
    stale_tasks = []
    for tid, _, max_age in CRITICAL:
        entry = next((t for t in sched["tasks"] if t["taskId"] == tid), None)
        if not entry or not entry.get("enabled", True):
            continue
        last = entry.get("lastRunAt")
        if not last:
            stale_tasks.append(tid)
            continue
        try:
            ts = datetime.fromisoformat(last.replace("Z", "+00:00"))
        except ValueError:
            continue
        if (now_utc() - ts).total_seconds() > max_age:
            stale_tasks.append(tid)
    if stale_tasks:
        reasons.append(f"{len(stale_tasks)} task(s) stale")
        worst = max(worst, "yellow", key=rank.get)

    if not reasons:
        reasons.append("all systems nominal")
    return worst, "; ".join(reasons)


# ---------- HTML ----------

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:#0b0d10;color:#e7e9ec;font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;-webkit-text-size-adjust:100%}
body{max-width:580px;margin:0 auto;padding:12px 12px 60px}
header{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;padding-top:4px}
header .date{font-size:11px;color:#9aa3ad;text-align:right;font-variant-numeric:tabular-nums}
h1{font-size:15px;font-weight:600;letter-spacing:0.02em;text-transform:uppercase;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
section{background:#13171c;border:1px solid #1f262e;border-radius:10px;padding:10px 12px 8px;margin-bottom:10px}
section h2{font-size:10px;text-transform:uppercase;letter-spacing:0.12em;color:#7a838d;font-weight:700;margin-bottom:8px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.row{display:flex;justify-content:space-between;align-items:baseline;padding:4px 0;border-bottom:1px solid #1c232c;gap:8px;min-height:24px}
.row:last-child{border-bottom:none}
.row .k{color:#9aa3ad;font-size:12px;flex:0 0 auto}
.row .v{font-variant-numeric:tabular-nums;text-align:right;font-size:13px}
.row .v.wide{flex:1 1 auto}
ul{list-style:none}
ul li{padding:6px 0;border-bottom:1px solid #1c232c;font-size:13px;display:flex;justify-content:space-between;gap:8px;align-items:baseline}
ul li:last-child{border-bottom:none}
ul li .label{color:#cfd4d9;flex:1}
ul li .meta{color:#7a838d;font-size:11px;font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;white-space:nowrap}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-weight:700;font-size:11px;letter-spacing:0.06em;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;text-transform:uppercase}
.pill.green{background:#0c3520;color:#5ee29a}
.pill.yellow{background:#3d2f0a;color:#fbbf24}
.pill.red{background:#3f0d12;color:#fb7185}
.muted{color:#6b7480}
.zero{color:#6b7480;font-size:12px}
.tag{display:inline-block;font-size:10px;padding:1px 5px;border-radius:4px;background:#1f2937;color:#9ca3af;margin-left:4px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;text-transform:uppercase;letter-spacing:0.06em}
.tag.green{background:#0c3520;color:#5ee29a}
.tag.yellow{background:#3d2f0a;color:#fbbf24}
.tag.red{background:#3f0d12;color:#fb7185}
.banner{padding:10px 12px;border-radius:10px;margin-bottom:10px;font-size:13px;display:flex;align-items:center;gap:10px;border:1px solid transparent}
.banner.green{background:#0c1f15;border-color:#16432a;color:#a7f0c3}
.banner.yellow{background:#221a07;border-color:#5a4515;color:#fde68a}
.banner.red{background:#250b0e;border-color:#5a1a22;color:#fda4af}
.banner .reason{font-size:12px;color:#cfd4d9;flex:1}
.gap{font-size:11px;color:#6b7480;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;line-height:1.6;padding:8px 12px}
.gap b{color:#9aa3ad}
a{color:#7ca7ff;text-decoration:none}
a:active{opacity:0.7}
.foot{text-align:center;font-size:10px;color:#525a64;margin-top:14px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
"""


def render(snapshot: dict) -> str:
    status = snapshot["status"]
    reason = snapshot["status_reason"]
    g = snapshot["generated_local"]

    # Q2 rows
    sched_rows = []
    sched_map = {t["taskId"]: t for t in snapshot["scheduled_tasks"]["tasks"]}
    for tid, label, max_age in CRITICAL:
        t = sched_map.get(tid)
        if not t:
            sched_rows.append({
                "label": label, "tone": "red",
                "last": "absent", "next": "n/a"})
            continue
        last = t.get("lastRunAt")
        nxt = t.get("nextRunAt")
        last_rel = rel_age(last)
        next_rel = rel_age(nxt)
        tone = "green"
        if last:
            try:
                ts = datetime.fromisoformat(last.replace("Z", "+00:00"))
                age = (now_utc() - ts).total_seconds()
                if age > max_age * 1.5:
                    tone = "red"
                elif age > max_age:
                    tone = "yellow"
            except ValueError:
                tone = "yellow"
        else:
            tone = "yellow"; last_rel = "never"
        sched_rows.append({
            "label": label, "tone": tone,
            "last": last_rel, "next": next_rel})

    # Build HTML pieces
    parts = []
    parts.append(f"<!DOCTYPE html><html lang=en><head><meta charset=utf-8>")
    parts.append('<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">')
    parts.append(f'<meta name=theme-color content="#0b0d10">')
    parts.append(f"<title>AIOS {escape(snapshot['date'])}</title>")
    parts.append(f"<style>{CSS}</style></head><body>")

    parts.append(f"""
    <header>
      <h1>AIOS</h1>
      <div class=date>{escape(g)}<br><span style="font-size:10px">gen {escape(snapshot['generated_utc'])}</span></div>
    </header>
    """)

    # Banner (Q1)
    parts.append(f"""
    <div class="banner {status}">
      <span class="pill {status}">{status}</span>
      <span class=reason>{escape(reason)}</span>
    </div>
    """)

    # Q1 detail roll-up
    t = snapshot["trading"]
    mk = snapshot["marketing"]
    fn = snapshot["findings"]
    parts.append("<section><h2>SYSTEM ROLL-UP</h2>")
    parts.append(_row("trading bot",
                      f"{(t.get('status') or 'unknown').lower()} · {fmt_pct(t.get('total_pnl_pct'))} pnl · {t.get('num_positions') or 0} pos"))
    parts.append(_row("marketing VPs",
                      _marketing_summary(mk)))
    parts.append(_row("recurrence findings",
                      f"{fn['open']} open · {fn['flag']} flag"))
    stale = sum(1 for r in sched_rows if r["tone"] != "green")
    parts.append(_row("scheduled tasks",
                      f"{len(sched_rows) - stale}/{len(sched_rows)} healthy"))
    parts.append("</section>")

    # Q2 scheduled tasks
    parts.append("<section><h2>SCHEDULED TASKS</h2><ul>")
    for r in sched_rows:
        meta = f"{r['last']} / next {r['next']}"
        parts.append(
            f'<li><span class=label>{escape(_clean(r["label"]))}'
            f'<span class="tag {r["tone"]}">{r["tone"]}</span></span>'
            f'<span class=meta>{escape(_clean(meta))}</span></li>'
        )
    parts.append("</ul></section>")

    # Q3 action inbox
    parts.append("<section><h2>ACTION INBOX</h2>")
    inbox_lines = []
    al = snapshot["action_list"]
    if al["urgent"]:
        inbox_lines.append((f"AIOS action list: {al['urgent']} urgent of {al['total']} open",
                            "see AIOS-action-list.md"))
    if mk["inbox_p0"] or mk["inbox_p1"]:
        inbox_lines.append((
            f"marketing P0/P1: {mk['inbox_p0']}/{mk['inbox_p1']}",
            "VP tracking inbox",
        ))
    for f in fn["items"]:
        if f["status"] in ("open", "flag"):
            tag = f["status"]
            short = (f["summary"][:80] + "…") if len(f["summary"]) > 82 else f["summary"]
            inbox_lines.append((f"{f['name']}", f"{tag} · {short or 'review'}"))
    if not inbox_lines:
        parts.append('<div class=zero>0 items waiting</div>')
    else:
        parts.append("<ul>")
        for label, meta in inbox_lines:
            parts.append(
                f'<li><span class=label>{escape(_clean(label))}</span>'
                f'<span class=meta>{escape(_clean(meta))}</span></li>'
            )
        parts.append("</ul>")
    parts.append("</section>")

    # Q4 Mote Ops today
    parts.append("<section><h2>MOTE OPS TODAY</h2>")
    news = snapshot["news"]
    rows4 = []
    if news.get("headline"):
        h = news["headline"]
        if len(h) > 160:
            h = h[:158].rstrip() + "..."
        rows4.append(("news-scout", h))
    if mk.get("last_heartbeat_ts"):
        rows4.append(("VP heartbeat",
                      f"{rel_age(mk['last_heartbeat_ts'])} · {mk['agent_count']} agent(s) reporting"))
    # P0/P1 surface here too: it tells Mike what's going on in the business
    if mk["inbox_p0"] or mk["inbox_p1"]:
        for it in mk["inbox_items"][:3]:
            prio = it.get("priority", "")
            title = it.get("title", "")
            rows4.append((f"{prio} {title}", "via VP tracking"))
    if not rows4:
        parts.append('<div class=zero>no business signal today</div>')
    else:
        for k, v in rows4:
            parts.append(_row(k, v, wide=True))
    parts.append("</section>")

    # Q5 trends
    parts.append("<section><h2>TRENDS</h2>")
    parts.append(_row("calls this week", _calls(snapshot)))
    parts.append(_row("trading total pnl", fmt_pct(t.get("total_pnl_pct"))))
    parts.append(_row("checkpoint", snapshot["checkpoint"]["checkpoint_summary"] or "no log"))
    parts.append(_row("runtime-state", snapshot["checkpoint"]["runtime_summary"] or "no log"))
    parts.append("</section>")

    # Data gaps footer
    gaps = snapshot["data_gaps"]
    if gaps:
        gap_html = " · ".join(escape(g) for g in gaps)
        parts.append(f'<div class=gap><b>data gaps:</b> {gap_html}</div>')

    parts.append(f"""
    <div class=foot>
      generated by dashboard-publish · {escape(snapshot['generated_utc'])}
    </div>
    </body></html>
    """)
    return "".join(parts)


def _clean(s):
    """Strip em/en-dashes and other noise from text we did not author."""
    if s is None:
        return ""
    return str(s).replace("—", " - ").replace("–", "-").replace("…", "...")


def _row(k, v, wide=False):
    cls = "v wide" if wide else "v"
    return f'<div class=row><span class=k>{escape(k)}</span><span class="{cls}">{escape(_clean(v))}</span></div>'


def _marketing_summary(mk):
    if mk["inbox_p0"]:
        return f"{mk['inbox_p0']} P0 · {mk['inbox_p1']} P1"
    if mk["inbox_p1"]:
        return f"{mk['inbox_p1']} P1"
    if mk["last_heartbeat_ts"]:
        return f"heartbeat {rel_age(mk['last_heartbeat_ts'])}"
    return "no heartbeat today"


def _calls(snapshot):
    mk = snapshot["marketing"]
    # vp-tracking heartbeat carries calls_this_week (may be null)
    for it in (mk.get("inbox_items") or []):
        pass
    # The heartbeat itself is the source — pulled separately
    calls = snapshot.get("calls_this_week")
    if calls is None:
        return "unavailable · Calendly not connected"
    return f"{calls} / target 3"


def fmt_pct(x):
    if x is None:
        return "n/a"
    sign = "+" if x >= 0 else ""
    return f"{sign}{x:.2f}%"


# ---------- main ----------

def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    sched   = collect_scheduled_tasks()
    trading = collect_trading_health()
    mk      = collect_marketing_health()
    fn      = collect_findings()
    news    = collect_news_headline()
    al      = collect_action_list()
    cp      = collect_checkpoint()

    # Pull calls_this_week from latest heartbeat (vp-tracking row)
    calls_this_week = None
    mk_dir = DESKTOP / "mote-ops" / "marketing"
    hb = latest(mk_dir, "heartbeat-*.jsonl")
    if hb:
        for line in hb.read_text(errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("agent") == "vp-tracking":
                v = row.get("calls_this_week")
                if v is not None:
                    calls_this_week = v

    worst, reason = roll_up(trading, mk, fn, sched)

    now_p = now_pdt()
    now_u = now_utc()
    snapshot = {
        "generated_utc": now_u.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_local": now_p.strftime("%a %H:%M PDT"),
        "date": now_p.strftime("%Y-%m-%d"),
        "status": worst,
        "status_reason": reason,
        "trading": trading,
        "marketing": mk,
        "findings": fn,
        "news": news,
        "action_list": al,
        "checkpoint": cp,
        "scheduled_tasks": sched,
        "calls_this_week": calls_this_week,
        "data_gaps": [
            "pipeline state (~/mote-ops/pipeline/data/command_state.json) for next call, active prospects, intake verdict",
            "intel feed (~/mote-ops/ops/.claude/skills/intel/dashboard-feed.json) for weekly observation",
            "client staged SOWs (~/mote-ops/clients/*/staged_replies/) for approvals queue",
            "Hermes audit findings (intel-feed repo) for audit-ready items",
            "Calendly source for calls-this-week metric (vp-tracking reports null)",
        ],
    }

    html = render(snapshot)
    INDEX.write_text(html, encoding="utf-8")

    snap_name = "snapshot-" + now_p.strftime("%Y-%m-%dT%H%M") + ".json"
    (DATA_DIR / snap_name).write_text(
        json.dumps(snapshot, indent=2, default=str), encoding="utf-8"
    )

    print(
        f"dashboard published {snapshot['generated_utc']}, "
        f"status {worst}, {fn['open']} findings open"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
