import time
from datetime import datetime

from telegram import Bot

QUIET_HOURS_START = 23
QUIET_HOURS_END = 6


def _in_quiet_hours(now: datetime | None = None) -> bool:
    """True between 23:00 and 06:00 local time, when non-critical messages are held."""
    hour = (now or datetime.now()).hour
    return hour >= QUIET_HOURS_START or hour < QUIET_HOURS_END


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.bot = Bot(token=token)
        self.chat_id = chat_id
        self.last_sent_at: float = time.monotonic()

    async def send(self, message: str, reply_markup=None, force: bool = False) -> None:
        """Send a message, unless it's quiet hours (23:00-06:00) and this isn't
        a forced (critical) message."""
        if not force and _in_quiet_hours():
            return
        await self.bot.send_message(
            chat_id=self.chat_id,
            text=message,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
        self.last_sent_at = time.monotonic()
