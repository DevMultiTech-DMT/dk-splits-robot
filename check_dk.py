"""DraftKings splits reachability test.

READ-ONLY. Opens the public DK Network betting-splits page for each league and
reports whether it loaded and what games it found. Writes nothing anywhere --
no database, no files (except GitHub's run summary).

Exit code 0 = every league's page loaded. 1 = at least one was blocked/failed.
"""
import html
import os
import re
import sys
import time
import urllib.error
import urllib.request

URL = "https://dknetwork.draftkings.com/draftkings-sportsbook-betting-splits/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
# (league, DK event-group id, date window)
LEAGUES = [
    ("MLB", 84240, "today"),
    ("NFL", 88808, "n7days"),
    ("WNBA", 94682, "n7days"),
    ("CFB", 87637, "today"),
    ("NBA", 42648, "n7days"),
]
BLOCK_MARKERS = [
    "Access Denied",
    "Just a moment",
    "cf-browser-verification",
    "Pardon Our Interruption",
    "Request unsuccessful",
    "captcha",
]
MAX_PAGES = 10


def fetch(eg, edate, page):
    q = f"?tb_eg={eg}&tb_edate={edate}&tb_emt=0&tb_page={page}"
    req = urllib.request.Request(URL + q, headers={"User-Agent": UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if e.fp else ""
        return e.code, body
    except Exception as e:  # DNS, timeout, reset...
        return None, f"{type(e).__name__}: {e}"


def parse(page_html):
    """-> list of (event_id, 'AWAY @ HOME', 'M/D, HH:MMPM', first_market, rows)
    rows = [(team_label, handle_pct, bets_pct), ...] for the first market shown."""
    games = []
    for b in page_html.split('<div class="tb-se ')[1:]:
        t = re.search(
            r'tb-se-title.*?<a[^>]*href="[^"]*event/(\d+)[^"]*"[^>]*>(.*?)</a>.*?<span[^>]*>\s*(.*?)\s*</span>',
            b,
            re.S,
        )
        if not t:
            continue
        title = re.sub(r"<[^>]+>", "", html.unescape(t.group(2)))
        title = re.sub(r"\s+", " ", title).strip()
        head = re.search(r'tb-se-head[^>]*>\s*<div class="flex-1">([^<]+)</div>', b)
        rows = re.findall(
            r'tb-slipline[^>]*>([^<]+)</div>\s*<div class="flex-1">\s*<a[^>]*>\s*[^<]+?\s*</a>\s*</div>'
            r'\s*<div class="flex-1">(\d+)%.*?<div class="flex-1">(\d+)%',
            b,
            re.S,
        )
        games.append(
            (
                t.group(1),
                title,
                t.group(3).strip(),
                head.group(1).strip() if head else "?",
                [(r[0].strip(), int(r[1]), int(r[2])) for r in rows[:2]],
            )
        )
    return games


def check_league(name, eg, edate):
    seen, games, pages = set(), [], 0
    for page in range(1, MAX_PAGES + 1):
        t0 = time.time()
        status, body = fetch(eg, edate, page)
        ms = int((time.time() - t0) * 1000)
        blocked = [m for m in BLOCK_MARKERS if m.lower() in body.lower()] if body else []
        if status != 200 or (blocked and "tb-se" not in body):
            return {
                "league": name, "ok": False, "status": status, "ms": ms,
                "why": ", ".join(blocked) or body[:200].replace("\n", " "),
                "games": games, "pages": pages,
            }
        if "tb_eg" not in body and "tb-se" not in body:
            return {
                "league": name, "ok": False, "status": status, "ms": ms,
                "why": "page loaded but it is not the splits page (layout changed?)",
                "games": games, "pages": pages,
            }
        pages += 1
        got = parse(body)
        new = [g for g in got if g[0] not in seen]
        if not new:  # past the last page, DK repeats the last page
            break
        for g in new:
            seen.add(g[0])
        games += new
        if len(got) < 10:
            break
    return {"league": name, "ok": True, "status": 200, "ms": ms, "why": "", "games": games, "pages": pages}


def main():
    results = [check_league(*lg) for lg in LEAGUES]
    lines = ["## DraftKings splits page: can GitHub reach it?", ""]
    lines.append(f"Run at {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    lines.append("")
    lines.append("| League | Result | Games found | Pages | Example |")
    lines.append("|---|---|---|---|---|")
    for r in results:
        if not r["ok"]:
            lines.append(f"| {r['league']} | BLOCKED / FAILED (HTTP {r['status']}) | - | - | {r['why'][:120]} |")
            continue
        ex = "(no games listed right now)"
        for g in r["games"]:
            if len(g[4]) == 2:
                (h, hm, hb), (a, am, ab) = g[4]
                ex = f"{g[1]} ({g[2]}), {g[3]}: {h} {hm}% money / {hb}% bets; {a} {am}% / {ab}%"
                break
        lines.append(f"| {r['league']} | OK | {len(r['games'])} | {r['pages']} | {ex} |")
    lines.append("")
    for r in results:
        if r["ok"] and r["games"]:
            lines.append(f"<details><summary>{r['league']}: all {len(r['games'])} games</summary>")
            lines.append("")
            for g in r["games"]:
                rows = "; ".join(f"{t} {m}%/{b}%" for t, m, b in g[4])
                lines.append(f"- {g[1]} | {g[2]} | {g[3]} | {rows}")
            lines.append("</details>")
            lines.append("")
    report = "\n".join(lines)
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report + "\n")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
