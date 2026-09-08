import asyncio
from datetime import datetime, timedelta, timezone

from telegram.ext import Application, CommandHandler
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

import config
from odds_client import fetch_odds, consensus_for_event
from injuries import fetch_team_injuries
from public_betting import fetch_public_betting
from matching import match_cleatz_game
from signals import sharp_side
from state import load_state, save_state

scheduler = AsyncIOScheduler(timezone="America/New_York")
scheduled_keys = set()


def fmt_signed(n):
    if n is None:
        return "—"
    return f"{'+' if n > 0 else ''}{n}"


async def check_game(bot, odds_event, checkpoint_label):
    state = load_state()
    games_state = state.setdefault("games", {})
    gid = odds_event["id"]
    g_state = games_state.setdefault(gid, {"history": []})

    consensus = consensus_for_event(odds_event)
    g_state["history"].append({"spread": consensus["spread"], "total": consensus["total"]})
    g_state["history"] = g_state["history"][-10:]

    open_snap = g_state["history"][0]
    cur_snap = g_state["history"][-1]
    spread_delta = (cur_snap["spread"] - open_snap["spread"]
                    if cur_snap["spread"] is not None and open_snap["spread"] is not None else None)
    total_delta = (cur_snap["total"] - open_snap["total"]
                   if cur_snap["total"] is not None and open_snap["total"] is not None else None)

    try:
        cleatz_games = await fetch_public_betting()
    except Exception as e:
        print("public betting fetch failed:", e)
        cleatz_games = []
    match = match_cleatz_game(odds_event, cleatz_games)

    lines = [
        f"*{odds_event['away_team']} @ {odds_event['home_team']}* — {checkpoint_label}",
        f"Spread: {fmt_signed(consensus['spread'])}"
        + (f" (moved {fmt_signed(spread_delta)} since first check)" if spread_delta else ""),
        f"Total: {consensus['total']}"
        + (f" (moved {fmt_signed(total_delta)} since first check)" if total_delta else ""),
    ]

    if match:
        sp, tot = match["spread"], match["total"]
        lines.append(f"Spread bets/handle — {sp['side_a']}: {sp['bets_a']}%/{sp['handle_a']}%  |  "
                     f"{sp['side_b']}: {sp['bets_b']}%/{sp['handle_b']}%")
        lines.append(f"Total bets/handle — {tot['side_a']}: {tot['bets_a']}%/{tot['handle_a']}%  |  "
                     f"{tot['side_b']}: {tot['bets_b']}%/{tot['handle_b']}%")
        sp_sharp, tot_sharp = sharp_side(sp), sharp_side(tot)
        if sp_sharp:
            lines.append(f"⚠️ Sharp signal (spread): {sp_sharp[0]} — {sp_sharp[1]}pp handle-over-bets gap")
        if tot_sharp:
            lines.append(f"⚠️ Sharp signal (total): {tot_sharp[0]} — {tot_sharp[1]}pp handle-over-bets gap")
    else:
        lines.append("(No public-betting match found — parser may need a fix, send me the Railway log)")

    try:
        away_inj = fetch_team_injuries(odds_event["away_team"])
        home_inj = fetch_team_injuries(odds_event["home_team"])
        notable = [p for p in away_inj + home_inj if p["status"] in ("Out", "Doubtful")]
        if notable:
            lines.append("Injuries: " + ", ".join(f"{p['name']} ({p['status']})" for p in notable))
    except Exception as e:
        print("injury fetch failed:", e)

    save_state(state)
    await bot.send_message(chat_id=config.TELEGRAM_CHAT_ID, text="\n".join(lines), parse_mode="Markdown")


async def refresh_schedule(app):
    try:
        events = await asyncio.to_thread(fetch_odds, config.ODDS_API_KEY)
    except Exception as e:
        print("odds fetch failed:", e)
        return
    now = datetime.now(timezone.utc)
    for ev in events:
        kickoff = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        for offset in config.CHECK_OFFSETS_MIN:
            run_at = kickoff - timedelta(minutes=offset)
            key = f"{ev['id']}-{offset}"
            if key in scheduled_keys or run_at <= now:
                continue
            scheduler.add_job(check_game, DateTrigger(run_date=run_at),
                               args=[app.bot, ev, f"T-{offset}min"], id=key, misfire_grace_time=300)
            scheduled_keys.add(key)


async def cmd_status(update, context):
    await update.message.reply_text("Line Watch is running. Checks fire at T-60/30/10 min before each kickoff.")


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
    await update.message.reply_text(
        "Running a live check — this can take ~20-30s (Playwright has to render the Cleatz page)..."
    )
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
    app.run_polling()


if __name__ == "__main__":
    main()
