import os

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Optional — enables state to survive a Railway restart (same pattern as quality-momentum-bot)
RAILWAY_TOKEN = os.environ.get("RAILWAY_TOKEN", "")
RAILWAY_PROJECT_ID = os.environ.get("RAILWAY_PROJECT_ID", "")
RAILWAY_SERVICE_ID = os.environ.get("RAILWAY_SERVICE_ID", "")
RAILWAY_ENVIRONMENT_ID = os.environ.get("RAILWAY_ENVIRONMENT_ID", "")

STATE_VAR_NAME = "LINE_WATCH_STATE"
CHECK_OFFSETS_MIN = [60, 30, 10]      # minutes before kickoff to check
SCHEDULE_REFRESH_HOURS = 6            # how often to re-pull the schedule for new games
SHARP_GAP_THRESHOLD = 15              # handle% - bets% gap (points) needed to call it a sharp signal
