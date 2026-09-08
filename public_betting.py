import re
from playwright.async_api import async_playwright

CLEATZ_URL = "https://cleatz.com/public-betting/nfl/"

# "NO Saints@DET Lions\nSUN, SEP 13 · 1:00 PM ET"
GAME_HEADER_RE = re.compile(
    r"([A-Z]{2,3} [\w .'\-]+?)@([A-Z]{2,3} [\w .'\-]+?)\s*\n\s*"
    r"([A-Za-z]{3}, [A-Za-z]{3} \d{1,2})\s*[·\-]\s*(\d{1,2}:\d{2}\s?[AaPp][Mm])\s*ET",
)

# Just the Bets/Handle percentage pairs, in the order they appear — no attempt
# to parse the label line, since it can carry extra badge/move text (e.g. "+30")
# that breaks a strict single-line label match.
PAIR_RE = re.compile(
    r"Bets\s*\n\s*(\d{1,3})%\s*\n\s*Handle\s*\n\s*(\d{1,3})%",
    re.IGNORECASE,
)


async def _fetch_rendered_text():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()
        await page.goto(CLEATZ_URL, wait_until="networkidle", timeout=45000)

        try:
            await page.wait_for_function("document.body.innerText.includes('@')", timeout=15000)
        except Exception:
            print("[cleatz] '@' marker never appeared — likely bot-blocked or needs interaction")

        for _ in range(4):
            await page.mouse.wheel(0, 2000)
            await page.wait_for_timeout(500)

        text = await page.inner_text("body")
        await browser.close()

    print(f"[cleatz] fetched {len(text)} chars")
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
            continue

        away_label = m.group(1).strip()  # e.g. "NO Saints"
        home_label = m.group(2).strip()  # e.g. "DET Lions"

        game = {
            "away": away_label,
            "home": home_label,
            "date_str": m.group(3).strip(),
            "time_str": m.group(4).strip(),
            # ASSUMPTION: home listed first, away second — unverified against a
            # game where the away team is favored. Flip if that turns out wrong.
            "spread": {
                "side_a": home_label, "bets_a": int(pairs[0][0]), "handle_a": int(pairs[0][1]),
                "side_b": away_label, "bets_b": int(pairs[1][0]), "handle_b": int(pairs[1][1]),
            },
            "total": {
                "side_a": "Over", "bets_a": int(pairs[2][0]), "handle_a": int(pairs[2][1]),
                "side_b": "Under", "bets_b": int(pairs[3][0]), "handle_b": int(pairs[3][1]),
            },
        }
        if len(pairs) >= 6:
            game["moneyline"] = {
                "side_a": home_label, "bets_a": int(pairs[4][0]), "handle_a": int(pairs[4][1]),
                "side_b": away_label, "bets_b": int(pairs[5][0]), "handle_b": int(pairs[5][1]),
            }
        games.append(game)
    print(f"[cleatz] parsed {len(games)} games, {len(headers)} headers matched")
    return games


async def fetch_public_betting():
    text = await _fetch_rendered_text()
    return parse_public_betting(text)
