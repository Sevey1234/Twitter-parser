import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv

from .database.models import UserError
from .database.repository import INTERVALS


def micros(value):
    try:
        amount = Decimal(str(value).strip().replace(",", "."))
        if not amount.is_finite() or not 0 <= amount <= 1_000_000:
            raise ValueError
        scaled = amount * 1_000_000
        if scaled != scaled.to_integral_value():
            raise ValueError
        return int(scaled)
    except (InvalidOperation, ValueError):
        raise UserError("Введите сумму в USD от 0 до 1 000 000, до шести знаков после запятой.") from None


def dollars(value):
    return f"{Decimal(value) / 1_000_000:.6f}".rstrip("0").rstrip(".") or "0"


@dataclass(frozen=True)
class Config:
    telegram_token: str = field(repr=False)
    bearer_token: str = field(repr=False)
    owner: int
    database: str
    hours: int
    budget: int
    post_price: int
    user_price: int
    ollama_url: str
    model: str

    @classmethod
    def load(cls):
        load_dotenv(".env", override=False)
        names = ("TELEGRAM_BOT_TOKEN", "X_BEARER_TOKEN", "TELEGRAM_ALLOWED_USER_ID")
        missing = [name for name in names if not os.getenv(name, "").strip()]
        if missing:
            raise UserError("Заполните в .env поля " + ", ".join(missing))
        token, bearer, owner = (os.environ[k].strip() for k in names)
        if not owner.isascii() or not owner.isdigit() or int(owner) < 1:
            raise UserError("TELEGRAM_ALLOWED_USER_ID должен быть положительным числовым ID.")
        if not token.partition(":")[0].isdigit() or not token.partition(":")[2]:
            raise UserError("Проверьте формат TELEGRAM_BOT_TOKEN из BotFather.")
        hours = os.getenv("DIGEST_INTERVAL_HOURS", "12")
        if hours not in tuple(map(str, INTERVALS)):
            raise UserError("DIGEST_INTERVAL_HOURS допускает 3, 6, 12 или 24.")
        prices = [micros(os.getenv(k, default)) for k, default in
                  (("X_POST_READ_USD", "0.005"), ("X_USER_READ_USD", "0.010"))]
        if min(prices) <= 0:
            raise UserError("Укажите положительные ставки чтения X API.")
        return cls(token, bearer, int(owner), os.getenv("DATABASE_PATH", "data/twitter_digest.db"),
                   int(hours), micros(os.getenv("MONTHLY_X_API_BUDGET_USD", "5")), *prices,
                   os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/"),
                   os.getenv("OLLAMA_MODEL", "qwen2.5:7b"))
