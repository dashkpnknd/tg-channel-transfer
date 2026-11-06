"""Copy to local_config.py and set values through environment variables."""

import os

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
