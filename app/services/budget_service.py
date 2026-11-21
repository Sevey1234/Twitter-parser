from dataclasses import dataclass

from app.database.models import UserError, stamp


@dataclass(frozen=True)
class Reservation:
    id: int
    limit: int
    price: int


class Budget:
    def __init__(self, repo, post_price, user_price):
        self.repo = repo
        self.prices = {"timeline": post_price, "profile": user_price}

    def stats(self):
        month = stamp(self.repo.clock())[:7]
        with self.repo.db.connect() as db:
            return tuple(db.execute("""SELECT count(*),
                       coalesce(sum(CASE WHEN endpoint='profile' THEN resources_received ELSE 0 END),0),
                       coalesce(sum(CASE WHEN endpoint='timeline' THEN resources_received ELSE 0 END),0),
                       coalesce(sum(estimated_micros),0) FROM api_usage
                       WHERE requested_at>=? AND requested_at<?""", (month, month + "~")).fetchone())

    def reserve(self, endpoint, maximum, fixed=False):
        when = stamp(self.repo.clock())
        price = self.prices[endpoint]
        minimum = 5 if endpoint == "timeline" else 1
        with self.repo.db.connect(write=True) as db:
            blocked = db.execute("SELECT retry_at FROM rate_limits WHERE endpoint=?", (endpoint,)).fetchone()
            if blocked and blocked[0] > when:
                raise UserError(f"Лимит X API. Следующая попытка после {blocked[0][:19]} UTC.")
            limit = db.execute("SELECT monthly_budget_micros FROM bot_users").fetchone()[0]
            spent = db.execute("""SELECT coalesce(sum(estimated_micros),0) FROM api_usage
                               WHERE requested_at>=? AND requested_at<?""",
                               (when[:7], when[:7] + "~")).fetchone()[0]
            available = min(maximum, (limit - spent) // price)
            if available < (maximum if fixed else minimum):
                raise UserError("Месячного бюджета недостаточно для страницы X. Проверьте /budget.")
            row = db.execute("""INSERT INTO api_usage(endpoint,requested_at,estimated_micros)
                             VALUES(?,?,?)""", (endpoint, when, available * price))
            return Reservation(row.lastrowid, available, price)

    def settle(self, ticket, received=None):
        with self.repo.db.connect(write=True) as db:
            if received is None:
                db.execute("UPDATE api_usage SET status='uncertain' WHERE id=?", (ticket.id,))
            else:
                db.execute("""UPDATE api_usage SET status='complete', resources_received=?,
                           estimated_micros=? WHERE id=?""", (received, received * ticket.price, ticket.id))

    def block(self, endpoint, until):
        with self.repo.db.connect(write=True) as db:
            db.execute("""INSERT INTO rate_limits VALUES(?,?) ON CONFLICT(endpoint)
                       DO UPDATE SET retry_at=max(retry_at,excluded.retry_at)""", (endpoint, stamp(until)))
