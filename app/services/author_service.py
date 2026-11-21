from urllib.parse import urlsplit

from app.database.models import UserError
from .twitter_client import HANDLE


class Authors:
    def __init__(self, repo, twitter):
        self.repo, self.twitter = repo, twitter

    @staticmethod
    def normalize(value):
        value = value.strip()
        if "://" in value:
            try:
                url = urlsplit(value)
            except ValueError:
                raise UserError("Некорректная ссылка на профиль X.") from None
            if url.scheme not in ("https", "http") or url.netloc.lower() not in (
                    "x.com", "www.x.com", "twitter.com", "www.twitter.com"):
                raise UserError("Нужна ссылка на профиль x.com или twitter.com.")
            value = url.path.removeprefix("/").removesuffix("/")
        else:
            value = value.removeprefix("@")
        if not HANDLE.fullmatch(value):
            raise UserError("Введите @username (1–15 латинских букв, цифр или _) либо ссылку на профиль.")
        return value

    def lookup(self, value):
        handle = self.normalize(value)
        if self.repo.author(handle):
            raise UserError("Этот автор уже добавлен.")
        profile = self.twitter.profile(handle)
        if any(r["x_user_id"] == profile.x_id for r in self.repo.authors()):
            raise UserError("Этот X User ID уже отслеживается.")
        return profile
