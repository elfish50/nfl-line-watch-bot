"""
ppy_model.py

Points-per-yard predictive spread model. Replaces public_betting.py /
matching.py / signals.py (the Cleatz scraper) as the bot's edge-detection
signal: instead of comparing bets%/handle% splits, this builds a per-team
offensive/defensive efficiency rating (points per yard), projects an
expected score for a matchup, and compares that projection against the
market spread already pulled by odds_client.py.

Data source: ESPN's unofficial site API — same one injuries.py already
uses, reusing its ESPN_TEAM_ID map so both modules key off the exact same
team-name strings The Odds API returns (e.g. "Seattle Seahawks").

State: nested under state["ppy"] in the same JSON blob state.py already
persists (via load_state()/save_state()), so no new persistence path is
needed — just make sure the Railway env vars in config.py are actually set
(RAILWAY_TOKEN etc.), since right now save_state() is a no-op without them.
"""

import requests

from injuries import ESPN_TEAM_ID

ESPN_SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"

HOME_FIELD_ADV = 1.5          # flat point bump for the home team
DISCREPANCY_THRESHOLD = 2.5   # points of divergence needed to flag a signal
LAST_N_GAMES = 4              # window for the "recent form" half of the blend


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
        print(f"[ppy] schedule fetch failed for team {espn_id}: {e}")
        return []

    events = data.get("events", [])
    return [
        e for e in events
        if e.get("competitions", [{}])[0].get("status", {})
              .get("type", {}).get("completed") is True
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
    ppy_state: the dict at state["ppy"] — mutated in place.
    Appends any completed games not already logged for this team.
    """
    espn_id = str(ESPN_TEAM_ID.get(team_name, ""))
    if not espn_id:
        return

    team_log = ppy_state.setdefault(team_name, {"espn_id": espn_id, "games": []})
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


# ---------------------------------------------------------------------------
# Rating computation
# ---------------------------------------------------------------------------

def _ratio(games, num_key, den_key):
    num = sum(g[num_key] for g in games)
    den = sum(g[den_key] for g in games)
    return num / den if den > 0 else 0.0


def compute_rating(ppy_state, team_name):
    """Returns a dict rating or None if no games logged yet for this team."""
    team_log = ppy_state.get(team_name)
    if not team_log or not team_log["games"]:
        return None

    games = team_log["games"]
    recent = games[-LAST_N_GAMES:]

    off_ppy = (
        _ratio(games, "points_scored", "yards_gained")
        + _ratio(recent, "points_scored", "yards_gained")
    ) / 2
    def_ppy_allowed = (
        _ratio(games, "points_allowed", "yards_allowed")
        + _ratio(recent, "points_allowed", "yards_allowed")
    ) / 2

    return {
        "off_ppy": off_ppy,
        "def_ppy_allowed": def_ppy_allowed,
        "avg_yards_gained": sum(g["yards_gained"] for g in games) / len(games),
        "avg_yards_allowed": sum(g["yards_allowed"] for g in games) / len(games),
    }


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

    return {
        "home_points": round(home_points, 1),
        "away_points": round(away_points, 1),
        "predicted_spread": round(predicted_spread, 1),
        "market_spread": market_spread_home,
        "discrepancy": round(discrepancy, 1),
        "is_signal": abs(discrepancy) >= threshold,
    }


def format_signal_line(proj, home_team, away_team):
    fav = home_team if proj["predicted_spread"] < 0 else away_team
    flag = "⚠️ SIGNAL" if proj["is_signal"] else "no edge"
    return (
        f"Model: {fav} by {abs(proj['predicted_spread']):.1f} "
        f"(market {proj['market_spread']:+.1f}) — "
        f"{abs(proj['discrepancy']):.1f} pt gap [{flag}]"
    )
