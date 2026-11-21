"""Common value types. SQLite stores UTC timestamps with fixed precision."""
from dataclasses import dataclass
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc)


def stamp(value):
    if value.utcoffset() is None:
        raise ValueError("Требуется время с часовым поясом.")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def moment(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("X вернул дату без часового пояса.")
    return parsed.astimezone(timezone.utc)


class UserError(Exception):
    """Safe, actionable text suitable for the owner's chat."""


@dataclass(frozen=True)
class Profile:
    x_id: str
    username: str
    name: str


@dataclass(frozen=True)
class Post:
    x_id: str
    text: str
    created: datetime
