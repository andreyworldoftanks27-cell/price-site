import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
SITE_PASSWORD = os.getenv("SITE_PASSWORD", "")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-please")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL не задан в переменных окружения")
if not SITE_PASSWORD:
    raise RuntimeError("SITE_PASSWORD не задан в переменных окружения")
