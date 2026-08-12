import asyncio
import logging
import time
from datetime import datetime

from telegram import Bot

log = logging.getLogger(__name__)

QUIET_HOURS_START = 23
QUIET_HOURS_END = 6

# Belt-and-suspenders on top of the Bot's own (usually ~5s) request timeouts:
# if the underlying connection pool ever wedges (observed in practice - a
# connection stuck in CLOSE_WAIT can hang a send indefinitely regardless of
# the configured per-request timeouts), this guarantees the poll loop still
# gets control back instead of freezing forever.
SEND_TIMEOUT_SECONDS = 20


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
        try:
            await asyncio.wait_for(
                self.bot.send_message(
                    chat_id=self.chat_id,
                    text=message,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                ),
                timeout=SEND_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            log.warning("Telegram send timed out after %ss, skipping", SEND_TIMEOUT_SECONDS)
            return
        self.last_sent_at = time.monotonic()

    async def send_photo(
        self, photo: bytes, caption: str, reply_markup=None, force: bool = False
    ) -> None:
        """Send a photo with a caption, subject to the same quiet-hours and
        timeout handling as send()."""
        if not force and _in_quiet_hours():
            return
        try:
            await asyncio.wait_for(
                self.bot.send_photo(
                    chat_id=self.chat_id,
                    photo=photo,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                ),
                timeout=SEND_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            log.warning("Telegram photo send timed out after %ss, skipping", SEND_TIMEOUT_SECONDS)
            return
        self.last_sent_at = time.monotonic()
