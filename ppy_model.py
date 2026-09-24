"""
ppy_model.py

Points-per-yard predictive spread model. Replaces public_betting.py /
matching.py / signals.py (the Cleatz scraper) as the bot's edge-detection
signal: instead of comparing bets%/handle% splits, this builds a per-team
offensive/defensive efficiency rating (points per yard), projects an
expected score for a matchup, and compares that projection against the
market spread already pulled by odds_client.py.

Cold-start handling: early in a season, current-season game logs are thin
(or empty). To avoid either ignoring the problem or over-trusting 1-2 games,
each team's rating blends a "prior" baseline built from PRIOR_SEASONS worth
of aggregate stats with the current season's game-by-game log. The prior's
weight shrinks toward zero as current-season games accumulate, reaching
zero once MIN_GAMES_FOR_SIGNAL is hit.

Data source: ESPN's unofficial site API — same one injuries.py already
uses, reusing its ESPN_TEAM_ID map so both modules key off the exact same
team-name strings The Odds API returns (e.g. "Seattle Seahawks").

State: nested under state["ppy"] in the same JSON blob state.py already
persists (via load_state()/save_state()), so no new persistence path is
needed — just make sure the Railway env vars in config.py are actually set
(RAILWAY_TOKEN etc.), since right now save_state() is a no-op without them.
Prior-season stats are fetched once and cached forever under state["ppy_priors"]
(a completed season's aggregate stats never change).
"""

import requests

from injuries import ESPN_TEAM_ID

ESPN_SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"

HOME_FIELD_ADV = 1.5          # flat point bump for the home team
DISCREPANCY_THRESHOLD = 2.5   # points of divergence needed to flag a signal
LAST_N_GAMES = 4               # window for the "recent form" half of the blend
MIN_GAMES_FOR_SIGNAL = 4       # current-season games needed before the prior's
                                # weight has fully shrunk to zero
PRIOR_SEASON_WEIGHTS = [0.7, 0.3]  # weight for [last season, two seasons ago]
                                     # normalized over whichever are available


# ---------------------------------------------------------------------------
# ESPN data fetching (sync, matches injuries.py / odds_client.py style)
# ---------------------------------------------------------------------------

def _fetch_team_schedule(espn_id, season):
    url = f"{ESPN_SITE_BASE}/teams/{espn_id}/schedule"
    try:
        resp = requests.get(url, params={"season": season}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[ppy] schedule fetch failed for team {espn_id} season {season}: {e}")
        return []

    events = data.get("events", [])
    return [
        e for e in events
        if e.get("competitions", [{}])[0].get("status", {})
              .get("type", {}).get("completed") is True
        # Regular season only (seasonType.id "2") — preseason games use
        # backups and vanilla schemes and would corrupt the per-yard ratios.
        and str(e.get("seasonType", {}).get("id", "")) == "2"
    ]


def _fetch_boxscore_team_stats(game_id):
    """Returns { espn_team_id: {"points": float, "yards": float, "is_home": bool} }"""
    url = f"{ESPN_SITE_BASE}/summary"
    try:
        resp = requests.get(url, params={"event": game_id}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[ppy] boxscore fetch failed for game {game_id}: {e}")
        return {}

    result = {}
    competitors = data.get("header", {}).get("competitions", [{}])[0].get("competitors", [])
    home_away_by_id = {c["team"]["id"]: c.get("homeAway") for c in competitors}
    points_by_id = {c["team"]["id"]: float(c.get("score", 0)) for c in competitors}

    for team_block in data.get("boxscore", {}).get("teams", []):
        team_id = team_block.get("team", {}).get("id")
        stats = {s["name"]: s.get("displayValue") for s in team_block.get("statistics", [])}
        yards_raw = stats.get("totalYards") or "0"
        try:
            yards = float(str(yards_raw).replace(",", ""))
        except ValueError:
            yards = 0.0
        result[team_id] = {
            "points": points_by_id.get(team_id, 0.0),
            "yards": yards,
            "is_home": home_away_by_id.get(team_id) == "home",
        }
    return result


def update_team_log(ppy_state, team_name, season):
    """
    ppy_state: the dict at state["ppy"] — mutated in place. Current-season
    game-by-game log only (used for the recent-form half of the blend).
    """
    espn_id = str(ESPN_TEAM_ID.get(team_name, ""))
    if not espn_id:
        return

    teams = ppy_state.setdefault("teams", {})
    team_log = teams.setdefault(team_name, {"espn_id": espn_id, "season": season, "games": []})

    # New season started — old game-by-game log is stale, reset it. (The
    # season it belongs to is compared against the season being requested
    # now, so this fires once, on the season's first update_team_log call.)
    if team_log.get("season") != season:
        team_log["season"] = season
        team_log["games"] = []

    known_ids = {g["game_id"] for g in team_log["games"]}

    for event in _fetch_team_schedule(espn_id, season):
        game_id = event["id"]
        if game_id in known_ids:
            continue

        box = _fetch_boxscore_team_stats(game_id)
        team_stats = box.get(espn_id)
        opp_stats = next((v for k, v in box.items() if k != espn_id), None)
        if not team_stats or not opp_stats:
            continue

        week = event.get("week", {}).get("number", 0)
        team_log["games"].append({
            "game_id": game_id,
            "week": week,
            "points_scored": team_stats["points"],
            "yards_gained": team_stats["yards"],
            "points_allowed": opp_stats["points"],
            "yards_allowed": opp_stats["yards"],
        })

    team_log["games"].sort(key=lambda g: g["week"])
    team_log["games"] = team_log["games"][-17:]  # cap at one season's worth


def update_prior_seasons(ppy_state, team_name, current_season):
    """
    ppy_state: the dict at state["ppy"] — mutated in place. Fetches and
    caches aggregate stats (summed across the whole season) for each of the
    last len(PRIOR_SEASON_WEIGHTS) completed seasons, skipping any season
    already cached — a completed season's totals never change.
    """
    espn_id = str(ESPN_TEAM_ID.get(team_name, ""))
    if not espn_id:
        return

    priors = ppy_state.setdefault("priors", {})
    team_priors = priors.setdefault(team_name, {})

    for offset in range(1, len(PRIOR_SEASON_WEIGHTS) + 1):
        season = current_season - offset
        key = str(season)
        if key in team_priors:
            continue  # already cached, completed seasons don't change

        games = _fetch_team_schedule(espn_id, season)
        if not games:
            continue

        totals = {"points_scored": 0.0, "yards_gained": 0.0,
                  "points_allowed": 0.0, "yards_allowed": 0.0, "games": 0}
        for event in games:
            box = _fetch_boxscore_team_stats(event["id"])
            team_stats = box.get(espn_id)
            opp_stats = next((v for k, v in box.items() if k != espn_id), None)
            if not team_stats or not opp_stats:
                continue
            totals["points_scored"] += team_stats["points"]
            totals["yards_gained"] += team_stats["yards"]
            totals["points_allowed"] += opp_stats["points"]
            totals["yards_allowed"] += opp_stats["yards"]
            totals["games"] += 1

        if totals["games"] > 0:
            team_priors[key] = totals


# ---------------------------------------------------------------------------
# Rating computation
# ---------------------------------------------------------------------------

def _ratio(num, den):
    return num / den if den > 0 else 0.0


def _prior_rating(ppy_state, team_name, current_season):
    """
    Blends the cached prior seasons (weighted, most recent first) into a
    single baseline off_ppy/def_ppy_allowed/avg_yards rating. Returns None
    if no prior seasons are cached yet.
    """
    team_priors = ppy_state.get("priors", {}).get(team_name, {})
    weighted = []
    for offset, weight in enumerate(PRIOR_SEASON_WEIGHTS, start=1):
        season_totals = team_priors.get(str(current_season - offset))
        if season_totals:
            weighted.append((weight, season_totals))

    if not weighted:
        return None

    total_weight = sum(w for w, _ in weighted)
    off_ppy = sum(w * _ratio(t["points_scored"], t["yards_gained"]) for w, t in weighted) / total_weight
    def_ppy_allowed = sum(w * _ratio(t["points_allowed"], t["yards_allowed"]) for w, t in weighted) / total_weight
    avg_yards_gained = sum(w * (t["yards_gained"] / t["games"]) for w, t in weighted) / total_weight
    avg_yards_allowed = sum(w * (t["yards_allowed"] / t["games"]) for w, t in weighted) / total_weight

    return {
        "off_ppy": off_ppy,
        "def_ppy_allowed": def_ppy_allowed,
        "avg_yards_gained": avg_yards_gained,
        "avg_yards_allowed": avg_yards_allowed,
    }


def compute_rating(ppy_state, team_name, current_season):
    """
    Returns a blended rating dict, or None if there's neither a current-season
    game logged nor a cached prior season to fall back on.
    """
    team_log = ppy_state.get("teams", {}).get(team_name)
    games = team_log["games"] if team_log else []
    games_played = len(games)

    current = None
    if games:
        recent = games[-LAST_N_GAMES:]
        off_ppy = (
            _ratio(sum(g["points_scored"] for g in games), sum(g["yards_gained"] for g in games))
            + _ratio(sum(g["points_scored"] for g in recent), sum(g["yards_gained"] for g in recent))
        ) / 2
        def_ppy_allowed = (
            _ratio(sum(g["points_allowed"] for g in games), sum(g["yards_allowed"] for g in games))
            + _ratio(sum(g["points_allowed"] for g in recent), sum(g["yards_allowed"] for g in recent))
        ) / 2
        current = {
            "off_ppy": off_ppy,
            "def_ppy_allowed": def_ppy_allowed,
            "avg_yards_gained": sum(g["yards_gained"] for g in games) / len(games),
            "avg_yards_allowed": sum(g["yards_allowed"] for g in games) / len(games),
        }

    prior = _prior_rating(ppy_state, team_name, current_season)

    if current is None and prior is None:
        return None
    if current is None:
        blended = dict(prior)
    elif prior is None:
        blended = dict(current)
    else:
        # Shrink the prior's weight to 0 by MIN_GAMES_FOR_SIGNAL current games.
        w_current = min(games_played / MIN_GAMES_FOR_SIGNAL, 1.0)
        w_prior = 1.0 - w_current
        blended = {
            k: w_current * current[k] + w_prior * prior[k]
            for k in ("off_ppy", "def_ppy_allowed", "avg_yards_gained", "avg_yards_allowed")
        }

    blended["games_played"] = games_played
    blended["used_prior"] = prior is not None
    return blended


# ---------------------------------------------------------------------------
# Matchup projection
# ---------------------------------------------------------------------------

def project_spread(home_rating, away_rating, market_spread_home,
                    threshold=DISCREPANCY_THRESHOLD):
    """
    market_spread_home: consensus["spread"] from odds_client.py — already
    uses the "negative = home favored" convention, so no sign conversion
    needed on that end.
    """
    home_exp_yards = (home_rating["avg_yards_gained"] + away_rating["avg_yards_allowed"]) / 2
    away_exp_yards = (away_rating["avg_yards_gained"] + home_rating["avg_yards_allowed"]) / 2

    home_points = ((home_rating["off_ppy"] + away_rating["def_ppy_allowed"]) / 2) * home_exp_yards
    away_points = ((away_rating["off_ppy"] + home_rating["def_ppy_allowed"]) / 2) * away_exp_yards
    home_points += HOME_FIELD_ADV

    predicted_spread = -(home_points - away_points)
    discrepancy = predicted_spread - market_spread_home

    min_games = min(home_rating["games_played"], away_rating["games_played"])
    used_prior = home_rating["used_prior"] or away_rating["used_prior"]
    # Only truly "no data" if there's no current-season log AND no prior to
    # fall back on for at least one team — compute_rating() already returns
    # None in that case, so by the time we're here both teams have *some*
    # basis. Low confidence now just means "still leaning heavily on priors".
    low_confidence = min_games < MIN_GAMES_FOR_SIGNAL and used_prior is False and min_games == 0

    return {
        "home_points": round(home_points, 1),
        "away_points": round(away_points, 1),
        "predicted_spread": round(predicted_spread, 1),
        "market_spread": market_spread_home,
        "discrepancy": round(discrepancy, 1),
        "is_signal": abs(discrepancy) >= threshold and not low_confidence,
        "low_confidence": low_confidence,
        "min_games": min_games,
        "used_prior": used_prior,
    }


def format_signal_line(proj, home_team, away_team):
    fav = home_team if proj["predicted_spread"] < 0 else away_team
    if proj["low_confidence"]:
        flag = "no data yet — low confidence"
    elif proj["is_signal"]:
        note = " (blended w/ prior seasons)" if proj["used_prior"] else ""
        flag = f"⚠️ SIGNAL{note}"
    else:
        flag = "no edge"
    return (
        f"Model: {fav} by {abs(proj['predicted_spread']):.1f} "
        f"(market {proj['market_spread']:+.1f}) — "
        f"{abs(proj['discrepancy']):.1f} pt gap [{flag}]"
    )
