import asyncio
import http.server
import os
import threading
from datetime import datetime, timedelta, timezone

from telegram.ext import Application, CommandHandler
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

import config
from odds_client import fetch_odds, consensus_for_event
from injuries import fetch_team_injuries
import ppy_model
from state import load_state, save_state

CURRENT_SEASON = 2026

scheduler = AsyncIOScheduler(timezone="America/New_York")
scheduled_keys = set()


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):
        pass  # keep this out of the logs, it's just noise


def _start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = http.server.HTTPServer(("0.0.0.0", port), _HealthHandler)
    print(f"[startup] health server listening on :{port}")
    server.serve_forever()


def fmt_signed(n):
    if n is None:
        return "—"
    n = round(n, 2)
    return f"{'+' if n > 0 else ''}{n}"


# ---------------------------------------------------------------------------
# State: load the Railway env var ONCE and keep it in memory. os.environ never
# changes inside a running container, so calling load_state() on every check
# kept handing back the startup copy and threw away every snapshot taken since
# — that's why the spread never showed any movement.
# ---------------------------------------------------------------------------
_state = None


def get_state():
    global _state
    if _state is None:
        _state = load_state()
    return _state


MX_TZ = timezone(timedelta(hours=-6))   # Mexico City (no DST since 2022)
MAX_HISTORY = 30                        # line changes kept per game (opening line always kept)
PRUNE_AFTER_DAYS = 3                    # drop finished games from state after this


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _fmt_time(iso):
    if not iso:
        return "earlier"
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc).astimezone(MX_TZ)
    return dt.strftime("%a %d/%m %H:%M")


def record_snapshot(games_state, ev, consensus):
    """Adds a line snapshot for this game if spread or total changed.
    Returns True if something new was stored."""
    g = games_state.setdefault(ev["id"], {"history": []})
    g["kickoff"] = ev["commence_time"]
    g["teams"] = f"{ev['away_team']} @ {ev['home_team']}"
    hist = g["history"]
    if hist and hist[-1].get("spread") == consensus["spread"] and hist[-1].get("total") == consensus["total"]:
        return False
    hist.append({"t": _now_iso(), "spread": consensus["spread"], "total": consensus["total"]})
    if len(hist) > MAX_HISTORY:
        g["history"] = [hist[0]] + hist[-(MAX_HISTORY - 1):]
    return True


def prune_finished(games_state, live_ids):
    """Removes games that are no longer in the odds feed and kicked off days ago."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=PRUNE_AFTER_DAYS)
    removed = False
    for gid in list(games_state.keys()):
        if gid in live_ids:
            continue
        ko = games_state[gid].get("kickoff")
        if not ko or datetime.fromisoformat(ko.replace("Z", "+00:00")) < cutoff:
            del games_state[gid]
            removed = True
    return removed


def movement_line(label, key, current, hist, prev):
    """'Spread: -3.5 | opened -2.5 (Mon 12/10 09:00) → week -1.0 | since Thu 15/10 13:00: -0.5'"""
    text = f"{label}: {fmt_signed(current) if key == 'spread' else (current if current is not None else '—')}"
    opening = next((s for s in hist if s.get(key) is not None), None)
    if current is None or opening is None:
        return text
    if len(hist) == 1:
        return text + " | first snapshot — week tracking starts now"
    op_val = opening[key]
    op_shown = fmt_signed(op_val) if key == "spread" else op_val
    text += f" | opened {op_shown} ({_fmt_time(opening.get('t'))}) → week {fmt_signed(current - op_val)}"
    if prev is not None and prev.get(key) is not None and prev is not opening:
        text += f" | since {_fmt_time(prev.get('t'))}: {fmt_signed(current - prev[key])}"
    return text


async def check_game(bot, odds_event, checkpoint_label):
    state = get_state()
    games_state = state.setdefault("games", {})
    gid = odds_event["id"]

    consensus = consensus_for_event(odds_event)
    prev_hist = games_state.get(gid, {}).get("history", [])
    prev = prev_hist[-1] if prev_hist else None
    record_snapshot(games_state, odds_event, consensus)
    hist = games_state[gid]["history"]

    lines = [
        f"*{odds_event['away_team']} @ {odds_event['home_team']}* — {checkpoint_label}",
        movement_line("Spread", "spread", consensus["spread"], hist, prev),
        movement_line("Total", "total", consensus["total"], hist, prev),
    ]

    away_team, home_team = odds_event["away_team"], odds_event["home_team"]
    try:
        away_inj = fetch_team_injuries(away_team)
        home_inj = fetch_team_injuries(home_team)
    except Exception as e:
        print("injury fetch failed:", e)
        away_inj, home_inj = [], []

    ppy_state = state.setdefault("ppy", {})
    try:
        ppy_model.update_team_log(ppy_state, home_team, CURRENT_SEASON)
        ppy_model.update_team_log(ppy_state, away_team, CURRENT_SEASON)
        ppy_model.update_prior_seasons(ppy_state, home_team, CURRENT_SEASON)
        ppy_model.update_prior_seasons(ppy_state, away_team, CURRENT_SEASON)
        home_rating = ppy_model.compute_rating(ppy_state, home_team, CURRENT_SEASON)
        away_rating = ppy_model.compute_rating(ppy_state, away_team, CURRENT_SEASON)
        if home_rating and away_rating and consensus["spread"] is not None:
            home_rating = ppy_model.apply_injury_adjustment(home_rating, home_inj)
            away_rating = ppy_model.apply_injury_adjustment(away_rating, away_inj)
            proj = ppy_model.project_spread(home_rating, away_rating, consensus["spread"])
            lines.append(ppy_model.format_signal_line(proj, home_team, away_team))
            if home_rating["injury_off_discount"] or home_rating["injury_def_weakness"]:
                lines.append(f"  ↳ {home_team} injury adj: "
                             f"off -{home_rating['injury_off_discount']*100:.0f}%, "
                             f"def +{home_rating['injury_def_weakness']*100:.0f}%")
            if away_rating["injury_off_discount"] or away_rating["injury_def_weakness"]:
                lines.append(f"  ↳ {away_team} injury adj: "
                             f"off -{away_rating['injury_off_discount']*100:.0f}%, "
                             f"def +{away_rating['injury_def_weakness']*100:.0f}%")
            ppy_model.log_prediction(ppy_state, odds_event["id"], home_team, away_team,
                                      odds_event["commence_time"], checkpoint_label, proj)
        else:
            lines.append("(Not enough game data yet for a model projection)")
        ppy_model.grade_predictions(ppy_state)
    except Exception as e:
        print("ppy model failed:", e)
        lines.append("(Model projection failed — send me the Railway log)")

    try:
        notable = [p for p in away_inj + home_inj if p["status"] in ("Out", "Doubtful")]
        if notable:
            lines.append("Injuries: " + ", ".join(f"{p['name']} ({p['status']})" for p in notable))
    except Exception as e:
        print("injury summary failed:", e)

    save_state(state)
    await bot.send_message(chat_id=config.TELEGRAM_CHAT_ID, text="\n".join(lines), parse_mode="Markdown")


async def scheduled_check(bot, event_id, fallback_ev, checkpoint_label):
    """T-60/30/10 jobs: pull LIVE odds at fire time. Before, the job reused the
    odds captured at the last 6-hour refresh, so the alert showed a stale line."""
    ev = None
    try:
        events = await asyncio.to_thread(fetch_odds, config.ODDS_API_KEY)
        ev = next((e for e in events if e["id"] == event_id), None)
    except Exception as e:
        print("live odds fetch for scheduled check failed:", e)
    if ev is None:
        ev = fallback_ev
        checkpoint_label += " (⚠️ live odds unavailable, line from last refresh)"
    await check_game(bot, ev, checkpoint_label)


async def refresh_schedule(app):
    try:
        events = await asyncio.to_thread(fetch_odds, config.ODDS_API_KEY)
    except Exception as e:
        print("odds fetch failed:", e)
        scheduler.add_job(refresh_schedule,
                          DateTrigger(run_date=datetime.now(timezone.utc) + timedelta(minutes=10)),
                          args=[app], id="refresh_retry", replace_existing=True)
        return

    # Snapshot every game's line on each refresh, so movement is tracked
    # all week long instead of only during the last hour before kickoff.
    state = get_state()
    games_state = state.setdefault("games", {})
    changed = False
    for ev in events:
        changed |= record_snapshot(games_state, ev, consensus_for_event(ev))
    changed |= prune_finished(games_state, {ev["id"] for ev in events})
    if changed:
        save_state(state)

    now = datetime.now(timezone.utc)
    for ev in events:
        kickoff = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        if kickoff <= now:
            continue  # already underway/finished, nothing left to schedule
        for offset in config.CHECK_OFFSETS_MIN:
            run_at = kickoff - timedelta(minutes=offset)
            key = f"{ev['id']}-{offset}"
            if key in scheduled_keys:
                continue
            if run_at <= now:
                # This checkpoint's time already passed — most likely the
                # service restarted between when it should have fired and
                # this refresh tick. Kickoff hasn't happened yet, so run it
                # now instead of silently losing the check forever.
                print(f"[schedule] catching up missed checkpoint {key} (was due {run_at.isoformat()})")
                scheduler.add_job(scheduled_check, DateTrigger(run_date=now),
                                   args=[app.bot, ev["id"], ev, f"T-{offset}min (late catch-up)"],
                                   id=key, misfire_grace_time=300)
            else:
                scheduler.add_job(scheduled_check, DateTrigger(run_date=run_at),
                                   args=[app.bot, ev["id"], ev, f"T-{offset}min"],
                                   id=key, misfire_grace_time=300)
            scheduled_keys.add(key)


async def cmd_status(update, context):
    await update.message.reply_text("Line Watch is running. Checks fire at T-60/30/10 min before each kickoff.")


async def cmd_whoami(update, context):
    await update.message.reply_text(f"This chat's ID is: {update.effective_chat.id}")


async def cmd_accuracy(update, context):
    state = get_state()
    ppy_state = state.setdefault("ppy", {})

    # Refresh logs for any team with an ungraded prediction, so a completed
    # game gets picked up even though no future /check happens to touch that
    # team first — otherwise grading silently waits until the team's next
    # scheduled matchup.
    pending_teams = {
        t for e in ppy_state.get("predictions", {}).values() if not e.get("graded")
        for t in (e["home_team"], e["away_team"])
    }
    for team in pending_teams:
        try:
            ppy_model.update_team_log(ppy_state, team, CURRENT_SEASON)
        except Exception as e:
            print(f"accuracy refresh failed for {team}:", e)

    ppy_model.grade_predictions(ppy_state)
    save_state(state)

    summary = ppy_model.accuracy_summary(ppy_state)
    if not summary:
        await update.message.reply_text("No graded predictions yet — check back after some games complete.")
        return

    lines = [
        f"Graded predictions: {summary['n']}",
        f"Model avg error: {summary['avg_model_error']} pts  |  "
        f"Market avg error: {summary['avg_market_error']} pts",
        f"Model beat market: {summary['beat_market_pct']}% of games",
    ]
    if summary["signal_count"]:
        lines.append(f"Signal record: {summary['signal_wins']}-"
                     f"{summary['signal_count'] - summary['signal_wins']} "
                     f"({summary['signal_win_pct']}%)")
    else:
        lines.append("No graded SIGNAL bets yet.")
    await update.message.reply_text("\n".join(lines))


async def cmd_games(update, context):
    try:
        events = await asyncio.to_thread(fetch_odds, config.ODDS_API_KEY)
    except Exception as e:
        await update.message.reply_text(f"Couldn't fetch schedule: {e}")
        return
    if not events:
        await update.message.reply_text("No upcoming NFL games found.")
        return
    lines = [f"{e['away_team']} @ {e['home_team']} — {e['commence_time']}" for e in events[:20]]
    await update.message.reply_text("\n".join(lines))


async def cmd_check(update, context):
    """Manually runs the full line+bets+injuries check right now, on demand,
    against the next upcoming game (or a named team) — no waiting for T-60/30/10."""
    await update.message.reply_text("Running a live check...")
    try:
        events = await asyncio.to_thread(fetch_odds, config.ODDS_API_KEY)
    except Exception as e:
        await update.message.reply_text(f"Odds fetch failed: {e}")
        return
    if not events:
        await update.message.reply_text("No upcoming NFL games found.")
        return
    target_team = " ".join(context.args) if context.args else None
    ev = None
    if target_team:
        for e in events:
            if target_team.lower() in e["away_team"].lower() or target_team.lower() in e["home_team"].lower():
                ev = e
                break
        if ev is None:
            await update.message.reply_text(f"No upcoming game matching '{target_team}' found.")
            return
    else:
        ev = events[0]
    try:
        await check_game(context.bot, ev, "manual /check")
    except Exception as e:
        await update.message.reply_text(f"Check failed: {e}")


async def post_init(app):
    scheduler.start()
    await refresh_schedule(app)
    scheduler.add_job(refresh_schedule, IntervalTrigger(hours=config.SCHEDULE_REFRESH_HOURS),
                       args=[app], id="refresh_schedule_job")


def main():
    token = config.TELEGRAM_BOT_TOKEN
    print(f"[startup] TELEGRAM_BOT_TOKEN present: {bool(token)}, length: {len(token)}")
    if not token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is empty. Check the Railway Variables tab on THIS service: "
            "name must be exactly TELEGRAM_BOT_TOKEN, and this service must have redeployed "
            "after the variable was saved."
        )
    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("games", cmd_games))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("accuracy", cmd_accuracy))
    threading.Thread(target=_start_health_server, daemon=True).start()
    app.run_polling()


if __name__ == "__main__":
    main()
