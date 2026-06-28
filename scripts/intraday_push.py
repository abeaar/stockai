"""Daily intraday push: generate the report + push to Discord/Telegram.

Two modes:
  - "report"  (default) - generate today's intraday plan and push it
  - "evaluate"          - backfill outcomes for yesterday's plans and push stats

Reads webhook / bot credentials from env:
  STOCKAI_DISCORD_WEBHOOK_URL  - Discord incoming webhook (preferred)
  STOCKAI_TELEGRAM_BOT_TOKEN   - Telegram bot token (alternative)
  STOCKAI_TELEGRAM_CHAT_ID     - Telegram chat ID to send to

If neither is set, prints the report to stdout only (still useful for cron
that just wants a log file).

Idempotent: safe to run multiple times per day.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only — no extra deps)
# ---------------------------------------------------------------------------

def _http_post(url: str, payload: dict, *, headers: dict | None = None) -> tuple[int, str]:
    """POST JSON to url. Returns (status_code, body)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def push_discord(webhook_url: str, markdown: str, title: str | None = None) -> bool:
    """Push markdown content to a Discord webhook as an embed."""
    # Discord embeds have a 4096-char description limit. Chunk if needed.
    chunks: list[str] = []
    s = markdown
    while len(s) > 3500:
        chunks.append(s[:3500])
        s = s[3500:]
    chunks.append(s)
    ok = True
    for i, chunk in enumerate(chunks):
        embed = {
            "title": (title or "StockAI Intraday") + (f" (cont. {i+1})" if len(chunks) > 1 else ""),
            "description": chunk,
            "color": 0x3B82F6,  # blue
            "footer": {"text": f"Generated {datetime.utcnow().isoformat(timespec='seconds')}Z"},
        }
        status, body = _http_post(webhook_url, {"embeds": [embed]})
        if status not in (200, 204):
            print(f"[discord] HTTP {status}: {body[:200]}", file=sys.stderr)
            ok = False
    return ok


def push_telegram(bot_token: str, chat_id: str, markdown: str) -> bool:
    """Push markdown content to a Telegram bot chat."""
    # Telegram message limit is 4096 chars; markdown is supported.
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    chunks: list[str] = []
    s = markdown
    while len(s) > 3800:
        chunks.append(s[:3800])
        s = s[3800:]
    chunks.append(s)
    ok = True
    for chunk in chunks:
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True,
        }
        # MarkdownV2 is strict; down-convert to plain text for safety.
        payload["parse_mode"] = "HTML"
        payload["text"] = _md_to_telegram_html(chunk)
        status, body = _http_post(url, payload)
        if status != 200:
            print(f"[telegram] HTTP {status}: {body[:200]}", file=sys.stderr)
            ok = False
    return ok


def _md_to_telegram_html(md: str) -> str:
    """Best-effort markdown -> HTML conversion for Telegram.

    Telegram accepts a small HTML subset: <b>, <i>, <code>, <pre>, <a>.
    """
    import re
    out = md
    # Escape HTML special chars first.
    out = out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Then apply selective replacements. Order matters.
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out, flags=re.DOTALL)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    return out


def push(markdown: str, title: str | None = None) -> str:
    """Push markdown to whichever channel is configured. Returns channel used."""
    webhook = os.environ.get("STOCKAI_DISCORD_WEBHOOK_URL", "").strip()
    tg_token = os.environ.get("STOCKAI_TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("STOCKAI_TELEGRAM_CHAT_ID", "").strip()
    if webhook:
        ok = push_discord(webhook, markdown, title=title)
        return ("discord" if ok else "discord-FAILED")
    if tg_token and tg_chat:
        ok = push_telegram(tg_token, tg_chat, markdown)
        return ("telegram" if ok else "telegram-FAILED")
    return "stdout-only"


# ---------------------------------------------------------------------------
# Report generation wrappers
# ---------------------------------------------------------------------------

def cmd_report() -> int:
    """Generate today's intraday report and push.

    Regime guard: if the recent 2-week avg R is below threshold, do NOT
    push — instead send a "PAUSE" alert so the user knows the system
    has stopped delivering until performance recovers.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from stockai.intraday import (
        score_universe, render_report, check_regime,
    )
    from stockai.data.sources.idx import get_lq45
    from stockai.config import get_settings

    # Regime check FIRST.
    verdict = check_regime()
    print(verdict.message)
    if not verdict.healthy:
        # Push a PAUSE alert instead of the report.
        channel = push(
            f"🚨 **Intraday PAUSE — {date.today().isoformat()}**\n\n"
            f"{verdict.message}\n\n"
            f"No report will be delivered until the regime recovers. "
            f"Use `stockai intraday regime` to check status.",
            title="StockAI Intraday — PAUSE",
        )
        print(f"[push] channel={channel} (PAUSE alert)")
        return 0

    settings = get_settings()
    universe = get_lq45()
    profiles = score_universe(
        universe=universe, top_n=5, min_score=settings.intraday_min_score
    )
    if not profiles:
        msg = (
            f"⚠ No names passed the min_score={settings.intraday_min_score:.2f} "
            f"filter today. Try lowering it or check back tomorrow."
        )
        print(msg)
        channel = push(msg, title="StockAI Intraday — No Plans")
        print(f"[push] channel={channel}")
        return 0
    md = render_report(profiles, as_of=date.today(), capital_idr=10_000_000)
    out_dir = Path(__file__).resolve().parents[1] / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"idx_intraday_{date.today().isoformat()}.md"
    out_path.write_text(md, encoding="utf-8")
    print(f"[report] wrote {out_path}  ({len(profiles)} names)")
    channel = push(md, title=f"StockAI Intraday — {date.today().isoformat()}")
    print(f"[push] channel={channel}")
    return 0


def cmd_evaluate() -> int:
    """Evaluate yesterday's plans and push a brief stats summary."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from stockai.intraday import evaluate_pending_plans, plan_stats

    today = date.today()
    n = evaluate_pending_plans(as_of=today)
    stats = plan_stats(symbol=None, since=today - timedelta(days=30))
    summary = (
        f"**Intraday daily update — {today.isoformat()}**\n\n"
        f"- Plans evaluated today: {n}\n"
        f"- Total evaluated (30d): {stats['n']}\n"
        f"- Win rate (TP1+TP2): {stats['win_rate']*100:.1f}%\n"
        f"- Average R: {stats['avg_r']:+.2f}R\n"
        f"- Total P&L / lot: Rp {stats['total_pnl_idr']:,.0f}\n"
    )
    print(summary)
    channel = push(summary, title=f"StockAI Intraday — EOD {today.isoformat()}")
    print(f"[push] channel={channel}")
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "report"
    if mode == "report":
        return cmd_report()
    if mode == "evaluate":
        return cmd_evaluate()
    if mode in ("-h", "--help"):
        print(__doc__)
        return 0
    print(f"Unknown mode: {mode}. Use 'report' or 'evaluate'.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
