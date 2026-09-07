def _mascot(name):
    return name.strip().split()[-1]


def match_cleatz_game(odds_event, cleatz_games):
    away_mascot = _mascot(odds_event["away_team"])
    home_mascot = _mascot(odds_event["home_team"])
    for g in cleatz_games:
        if away_mascot in g["away"] and home_mascot in g["home"]:
            return g
    return None
