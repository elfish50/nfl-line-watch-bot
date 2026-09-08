import re
from playwright.async_api import async_playwright

CLEATZ_URL = "https://cleatz.com/public-betting/nfl/"

# Matches a game header like: "NE Patriots@SEA Seahawks" followed by
# "Wed, Sep 9 · 8:20 pm ET" on the next line.
GAME_HEADER_RE = re.compile(
    r"([A-Z]{2,3} [\w .'\-]+?)@([A-Z]{2,3} [\w .'\-]+?)\s*\n\s*"
    r"([A-Za-z]{3}, [A-Za-z]{3} \d{1,2})\s*[·\-]\s*(\d{1,2}:\d{2}\s?[ap]m)\s*ET",
)

# Matches "<label line>\nBets\n<N>%\nHandle\n<N>%" — used sequentially to pull
# out spread side A, spread side B, total over, total under, ML side A, ML side B.
PAIR_RE = re.compile(
    r"([^\n]+?)\s*\n\s*Bets\s*\n\s*(\d{1,3})%\s*\n\s*Handle\s*\n\s*(\d{1,3})%",
    re.IGNORECASE,
)


async def _fetch_rendered_text():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()
        await page.goto(CLEATZ_URL, wait_until="networkidle", timeout=45000)
        text = await page.inner_text("body")
        await browser.close()
    print(f"[cleatz] fetched {len(text)} chars")
    print("[cleatz] first 1500 chars:\n" + text[:1500])
    return text


def parse_public_betting(full_text):
    headers = list(GAME_HEADER_RE.finditer(full_text))
    games = []
    for i, m in enumerate(headers):
        start = m.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(full_text)
        block = full_text[start:end]
        pairs = PAIR_RE.findall(block)
        if len(pairs) < 4:
            # Not enough parsed to trust this game — skip rather than guess
            continue
        game = {
            "away": m.group(1).strip(),
            "home": m.group(2).strip(),
            "date_str": m.group(3).strip(),
            "time_str": m.group(4).strip(),
            "spread": {
                "side_a": pairs[0][0].strip(), "bets_a": int(pairs[0][1]), "handle_a": int(pairs[0][2]),
                "side_b": pairs[1][0].strip(), "bets_b": int(pairs[1][1]), "handle_b": int(pairs[1][2]),
            },
            "total": {
                "side_a": pairs[2][0].strip(), "bets_a": int(pairs[2][1]), "handle_a": int(pairs[2][2]),
                "side_b": pairs[3][0].strip(), "bets_b": int(pairs[3][1]), "handle_b": int(pairs[3][2]),
            },
        }
        if len(pairs) >= 6:
            game["moneyline"] = {
                "side_a": pairs[4][0].strip(), "bets_a": int(pairs[4][1]), "handle_a": int(pairs[4][2]),
                "side_b": pairs[5][0].strip(), "bets_b": int(pairs[5][1]), "handle_b": int(pairs[5][2]),
            }
        games.append(game)
    print(f"[cleatz] parsed {len(games)} games, {len(headers)} headers matched")
    return games


async def fetch_public_betting():
    text = await _fetch_rendered_text()
    return parse_public_betting(text)
