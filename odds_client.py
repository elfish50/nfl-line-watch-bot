from statistics import median
import time
import requests

ODDS_URL = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds/"
RETRY_STATUSES = {500, 502, 503, 504}


def fetch_odds(api_key, retries=3):
    params = {
        "apiKey": api_key,
        "regions": "us",
        "markets": "spreads,totals",
        "oddsFormat": "american",
    }
    last_err = "unknown error"
    for attempt in range(retries):
        try:
            resp = requests.get(ODDS_URL, params=params, timeout=15)
            if resp.status_code in RETRY_STATUSES:
                last_err = f"HTTP {resp.status_code} from The Odds API"
            elif resp.status_code != 200:
                # error no recuperable (401 key mala, 429 sin cuota, etc.): no reintentar
                raise RuntimeError(f"HTTP {resp.status_code} from The Odds API")
            else:
                return resp.json()
        except requests.RequestException as e:
            # nunca usar str(e): trae la URL con la apiKey
            last_err = type(e).__name__
        if attempt < retries - 1:
            time.sleep(5 * (2 ** attempt))  # 5s, 10s
    raise RuntimeError(f"Odds fetch failed after {retries} tries ({last_err})")


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
