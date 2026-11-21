from datetime import timedelta

from telebot.apihelper import ApiTelegramException

from app.database.models import UserError, stamp


class Delivery:
    def __init__(self, repo, bot):
        self.repo, self.bot = repo, bot

    def recover(self):
        # Telegram may already have accepted these messages before the process stopped.
        with self.repo.db.connect(write=True) as db:
            db.execute("UPDATE digest_parts SET status='uncertain' WHERE status='sending'")

    def open_id(self):
        with self.repo.db.connect() as db:
            row = db.execute("SELECT id FROM digests WHERE status='queued'").fetchone()
            return row[0] if row else None

    def queue(self, rows, parts, start, end):
        with self.repo.db.connect(write=True) as db:
            if db.execute("SELECT 1 FROM digests WHERE status='queued'").fetchone():
                raise UserError("Сначала завершите предыдущую доставку.")
            did = db.execute("""INSERT INTO digests(owner_id,digest_text,period_start,period_end,
                             created_at,status) VALUES(?,?,?,?,?,'queued')""",
                             (self.repo.owner, "\n\n".join(parts), stamp(start), stamp(end),
                              stamp(self.repo.clock()))).lastrowid
            for row in rows:
                changed = db.execute("""UPDATE tweets SET processing_status='queued'
                                     WHERE id=? AND x_tweet_id=? AND processing_status='pending'
                                     AND author_id IN (SELECT id FROM authors WHERE is_active=1)""",
                                     (row["id"], row["x_tweet_id"])).rowcount
                if changed != 1:
                    raise UserError("Список публикаций изменился. Повторите сборку.")
                db.execute("INSERT INTO digest_tweets VALUES(?,?)", (did, row["id"]))
            db.executemany("INSERT INTO digest_parts(digest_id,part,html) VALUES(?,?,?)",
                           [(did, n, html) for n, html in enumerate(parts, 1)])
            return did

    def mark(self, did, part, status, message_id=None, retry_at=None, expected="sending"):
        when = stamp(self.repo.clock())
        with self.repo.db.connect(write=True) as db:
            changed = db.execute("""UPDATE digest_parts SET status=?,message_id=?,retry_at=?
                                 WHERE digest_id=? AND part=? AND status=?""",
                                 (status, message_id, retry_at, did, part, expected)).rowcount
            if not changed:
                raise UserError("Состояние части уже изменилось.")
            remaining = db.execute("SELECT 1 FROM digest_parts WHERE digest_id=? AND status<>'sent'",
                                   (did,)).fetchone()
            if not remaining:
                db.execute("UPDATE digests SET status='sent',sent_at=? WHERE id=?", (when, did))
                db.execute("""UPDATE tweets SET processing_status='sent' WHERE id IN
                           (SELECT tweet_id FROM digest_tweets WHERE digest_id=?)""", (did,))
                db.execute("UPDATE bot_users SET last_digest_at=?", (when,))

    def flush(self):
        did = self.open_id()
        if not did:
            return True
        parts = self.repo.history(did)
        for row in parts:
            if row["status"] == "sent":
                continue
            if row["status"] != "pending" or (row["retry_at"] or "") > stamp(self.repo.clock()):
                return False
            part = row["part"]
            self.mark(did, part, "sending", expected="pending")
            try:
                msg = self.bot.send_message(self.repo.owner,
                        row["html"] + f"\n\n<i>#{did} · {part}/{len(parts)}</i>",
                        parse_mode="HTML", disable_web_page_preview=True)
            except ApiTelegramException as exc:
                if 400 <= exc.error_code < 500:
                    delay = exc.result_json.get("parameters", {}).get("retry_after", 900)
                    retry = stamp(self.repo.clock() + timedelta(seconds=max(1, int(delay))))
                    self.mark(did, part, "pending", retry_at=retry)
                else:
                    self.mark(did, part, "uncertain")
                return False
            except Exception:
                self.mark(did, part, "uncertain")
                return False
            self.mark(did, part, "sent", message_id=msg.message_id)
        return True

    def erase(self, xid):
        manual = []
        for message_id, did, part in self.repo.erase_post(xid):
            if message_id is not None:
                try:
                    self.bot.delete_message(self.repo.owner, message_id)
                    continue
                except Exception:
                    pass
            manual.append(f"#{did} · {part}")
        result = "Источник и связанные выпуски удалены из базы."
        if manual:
            result += " Вручную удалите из чата части " + ", ".join(manual) + "."
        return result

    def inspect_or_resolve(self, arg):
        did = self.open_id()
        if not did:
            return "Очередь доставки пуста."
        rows = [r for r in self.repo.history(did) if r["status"] != "sent"]
        if not rows:
            return "Доставка завершена."
        row = rows[0]
        key = f"{did} {row['part']}"
        if arg:
            if row["status"] != "uncertain" or arg not in (key + " sent", key + " retry"):
                raise UserError("Сверьте номер и статус части через /delivery.")
            new = "sent" if arg.endswith(" sent") else "pending"
            self.mark(did, row["part"], new, expected="uncertain")
            return "Часть подтверждена." if new == "sent" else "Повтор разрешён. Доставка в течение минуты."
        if row["status"] == "uncertain":
            return (f"Проверьте в чате часть #{did} · {row['part']}/{len(self.repo.history(did))}. "
                    f"Если видите её — /delivery {key} sent. Если уверены, что её нет — "
                    f"/delivery {key} retry. Повтор может создать копию.")
        return f"Дайджест #{did}, часть {row['part']}: {row['status']}. " + (row["retry_at"] or "")
