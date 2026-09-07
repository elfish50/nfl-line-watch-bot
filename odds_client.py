from statistics import median
import requests

ODDS_URL = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds/"


def fetch_odds(api_key):
    params = {
        "apiKey": api_key,
        "regions": "us",
        "markets": "spreads,totals",
        "oddsFormat": "american",
    }
    resp = requests.get(ODDS_URL, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def consensus_for_event(ev):
    spreads, totals, books = [], [], []
    for bm in ev.get("bookmakers", []):
        spread_val = None
        total_val = None
        for mkt in bm.get("markets", []):
            if mkt["key"] == "spreads":
                for o in mkt["outcomes"]:
                    if o["name"] == ev["home_team"]:
                        spread_val = o["point"]
            if mkt["key"] == "totals":
                for o in mkt["outcomes"]:
                    if o["name"] == "Over":
                        total_val = o["point"]
        books.append({"name": bm["title"], "spread": spread_val, "total": total_val})
        if spread_val is not None:
            spreads.append(spread_val)
        if total_val is not None:
            totals.append(total_val)
    return {
        "spread": median(spreads) if spreads else None,
        "total": median(totals) if totals else None,
        "books": books,
    }
