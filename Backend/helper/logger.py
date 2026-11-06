from datetime import datetime
from logging import ERROR, INFO, Formatter, RotatingFileHandler, StreamHandler, basicConfig, getLogger
from os import getenv

import pytz

IST = pytz.timezone("Asia/Kolkata")

#----- Cap the log before it eats the phone's storage: 5 MiB per file, 3 kept.
#----- Override with LOG_MAX_BYTES / LOG_BACKUPS if a bigger window is needed.
try:
    LOG_MAX_BYTES = max(256 * 1024, int(getenv("LOG_MAX_BYTES", 5 * 1024 * 1024)))
    LOG_BACKUPS = max(0, min(20, int(getenv("LOG_BACKUPS", 3))))
except (TypeError, ValueError):
    LOG_MAX_BYTES, LOG_BACKUPS = 5 * 1024 * 1024, 3


#----- Formatter that renders timestamps in IST
class ISTFormatter(Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, IST)
        return dt.strftime(datefmt or "%d-%b-%y %I:%M:%S %p")


#----- Root logging configuration
formatter = ISTFormatter("[%(asctime)s] [%(levelname)s] - %(message)s", "%d-%b-%y %I:%M:%S %p")
file_handler = RotatingFileHandler("log.txt", maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
stream_handler = StreamHandler()
file_handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)

basicConfig(handlers=[file_handler, stream_handler], level=INFO)

getLogger("httpx").setLevel(ERROR)
getLogger("pyrogram").setLevel(ERROR)
getLogger("fastapi").setLevel(ERROR)

LOGGER = getLogger(__name__)
LOGGER.setLevel(INFO)
LOGGER.info("Logger initialized with IST timezone.")
