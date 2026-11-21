import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

from app.database.models import Post, Profile, UserError, moment

HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")
X_ID = re.compile(r"[0-9]{1,19}")


class ExpiredPage(UserError):
    pass


def valid_id(value):
    if not isinstance(value, str) or not X_ID.fullmatch(value):
        raise ValueError("Invalid X ID")
    return value


class TwitterClient:
    def __init__(self, http, token, budget):
        self.http, self.budget = http, budget
        self.headers = {"Authorization": "Bearer " + token}
        self.fields = "tweet.fields"

    def retry_at(self, response):
        clock = self.budget.repo.clock()
        candidates = []
        for header in ("retry-after", "x-rate-limit-reset"):
            raw = response.headers.get(header)
            if raw:
                try:
                    if header == "x-rate-limit-reset":
                        candidates.append(datetime.fromtimestamp(int(raw), timezone.utc))
                    elif raw.isdigit():
                        candidates.append(clock + timedelta(seconds=int(raw)))
                    else:
                        candidates.append(parsedate_to_datetime(raw))
                except (ValueError, TypeError, OverflowError, OSError):
                    pass
        return max([clock + timedelta(seconds=1), *candidates]) if candidates else clock + timedelta(minutes=15)

    def request(self, endpoint, path, params, maximum=1, fixed=False):
        ticket = self.budget.reserve(endpoint, maximum, fixed)
        params = dict(params)
        if endpoint == "timeline":
            params["max_results"] = ticket.limit
        try:
            response = self.http.get("https://api.x.com/2/" + path, params=params,
                                     headers=self.headers, timeout=30)
        except httpx.HTTPError:
            self.budget.settle(ticket)
            raise UserError("Нет ответа X API. Резерв расходов сохранён.") from None
        code = response.status_code
        if code == 429 or response.headers.get("x-rate-limit-remaining") == "0":
            self.budget.block(endpoint, self.retry_at(response))
        if not response.is_success:
            self.budget.settle(ticket, 0 if 400 <= code < 500 else None)
            if code == 400 and params.get("pagination_token") and "pagination" in response.text.lower():
                raise ExpiredPage("X отклонил токен страницы.")
            if (code == 400 and self.fields == "tweet.fields" and self.fields in params
                    and "tweet.fields" in response.text):
                self.fields = "post.fields"
                fields = params.pop("tweet.fields")
                params[self.fields] = fields.replace("note_tweet", "note_post")
                return self.request(endpoint, path, params, maximum, fixed)
            hints = {401: "проверьте Bearer Token", 402: "пополните кредиты X",
                     403: "проверьте доступ приложения и публичность профиля",
                     404: "автор не найден", 429: "повтор после сброса лимита"}
            raise UserError(f"X API HTTP {code}: {hints.get(code, 'повторите проверку позже')}.")
        try:
            payload = response.json()
            data = payload.get("data")
            if endpoint == "timeline" and (isinstance(data, list) or
                    (data is None and payload.get("meta", {}).get("result_count") == 0)):
                received = len(data or [])
            elif endpoint == "profile" and isinstance(data, dict):
                received = 1
            elif payload.get("errors") and data is None:
                received = 0
            else:
                raise ValueError
        except (AttributeError, TypeError, ValueError):
            self.budget.settle(ticket)
            raise UserError("Некорректный ответ X API. Продвижение курсора остановлено.") from None
        self.budget.settle(ticket, received)
        if payload.get("errors"):
            raise UserError("X вернул ошибку или неполные данные. Проверьте доступность автора.")
        return payload, ticket.limit

    def profile(self, handle):
        payload, _ = self.request("profile", "users/by/username/" + handle, {"user.fields": "protected"})
        try:
            row = payload["data"]
            if row.get("protected"):
                raise UserError("Этот профиль закрыт. Выберите публичного автора.")
            if not HANDLE.fullmatch(row["username"]) or not isinstance(row["name"], str):
                raise ValueError
            return Profile(valid_id(row["id"]), row["username"], row["name"][:100])
        except (KeyError, TypeError, ValueError):
            raise UserError("X вернул неполный профиль.") from None

    def timeline(self, author, state):
        fields = "created_at,note_tweet" if self.fields == "tweet.fields" else "created_at,note_post"
        params = {self.fields: fields, "exclude": "retweets,replies", "end_time": state["end_time"][:19]+"Z"}
        if author["last_tweet_id"]:
            params["since_id"] = author["last_tweet_id"]
        else:
            params["start_time"] = author["tracking_started_at"][:19] + "Z"
        if state["pagination_token"]:
            params["pagination_token"] = state["pagination_token"]
        payload, size = self.request("timeline", f"users/{author['x_user_id']}/tweets", params,
                                     state["page_size"] or 100, fixed=bool(state["page_size"]))
        try:
            posts = []
            for item in payload.get("data", []):
                note = item.get("note_tweet") or item.get("note_post") or {}
                text = note.get("text") or item["text"]
                if not isinstance(text, str) or not text.strip():
                    raise ValueError
                posts.append(Post(valid_id(item["id"]), text, moment(item["created_at"])))
            meta = payload["meta"]
            token = meta.get("next_token")
            if meta["result_count"] != len(posts) or (token is not None and
                                                      (not isinstance(token, str) or not token)):
                raise ValueError
            return posts, token, size
        except (KeyError, TypeError, ValueError, AttributeError):
            raise UserError("Неполная страница X. Позиция чтения сохранена для повтора.") from None
