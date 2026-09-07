import requests

ESPN_TEAM_ID = {
    'Arizona Cardinals': 22, 'Atlanta Falcons': 1, 'Baltimore Ravens': 33, 'Buffalo Bills': 2,
    'Carolina Panthers': 29, 'Chicago Bears': 3, 'Cincinnati Bengals': 4, 'Cleveland Browns': 5,
    'Dallas Cowboys': 6, 'Denver Broncos': 7, 'Detroit Lions': 8, 'Green Bay Packers': 9,
    'Houston Texans': 34, 'Indianapolis Colts': 11, 'Jacksonville Jaguars': 30, 'Kansas City Chiefs': 12,
    'Las Vegas Raiders': 13, 'Los Angeles Chargers': 24, 'Los Angeles Rams': 14, 'Miami Dolphins': 15,
    'Minnesota Vikings': 16, 'New England Patriots': 17, 'New Orleans Saints': 18, 'New York Giants': 19,
    'New York Jets': 20, 'Philadelphia Eagles': 21, 'Pittsburgh Steelers': 23, 'San Francisco 49ers': 25,
    'Seattle Seahawks': 26, 'Tampa Bay Buccaneers': 27, 'Tennessee Titans': 10, 'Washington Commanders': 28,
}
INJURY_RANK = {"Out": 3, "Doubtful": 2, "Questionable": 1, "Probable": 0}


def fetch_team_injuries(team_name):
    espn_id = ESPN_TEAM_ID.get(team_name)
    if not espn_id:
        return []
    url = f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{espn_id}/roster?enable=injuries"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []
    players = []
    for group in data.get("athletes", []):
        for p in group.get("items", []):
            inj = p.get("injuries") or []
            if inj:
                status = inj[0].get("status", "Unknown")
                players.append({
                    "name": p.get("displayName"),
                    "position": (p.get("position") or {}).get("abbreviation", ""),
                    "status": status,
                })
    players.sort(key=lambda x: -INJURY_RANK.get(x["status"], 0))
    return players
