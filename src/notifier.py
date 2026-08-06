import time

from telegram import Bot


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.bot = Bot(token=token)
        self.chat_id = chat_id
        self.last_sent_at: float | None = None

    async def send(self, message: str) -> None:
        await self.bot.send_message(
            chat_id=self.chat_id,
            text=message,
            parse_mode="HTML",
        )
        self.last_sent_at = time.monotonic()
