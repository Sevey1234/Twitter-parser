import logging
from dataclasses import asdict

from app.config import dollars, micros
from app.database.models import Profile, UserError, moment
from app.database.repository import INTERVALS
from app.services.formatting import MOSCOW, schedule_time
from . import keyboards as kb
from .states import State

HELP = """Twitter Digest Bot · личный кабинет
/add_author — добавить автора X
/authors — список и состояние
/edit_author — изменить подпись
/pause_author и /resume_author — пауза и продолжение
/remove_author — удалить автора
/interval — интервал автоматической проверки X
/digest — проверить новые публикации сейчас
/history — история, /history N — выпуск и время публикации источников
/settings — расписание и результат последней плановой проверки
/reset_settings — вернуть настройки из .env
/stats — запросы, публикации и расходы
/budget — месячный предел расходов
/delivery — ожидание выжимки и состояние доставки
/forget_tweet ID — удалить источник и его выпуск
/cancel — отменить ввод
/help — эта справка"""
ACTIONS = {"remove_author": "remove", "pause_author": "pause", "resume_author": "resume",
           "edit_author": "rename"}


class Chat:
    def __init__(self, bot, repo, authors, usage, scheduler, delivery):
        self.bot, self.repo, self.authors = bot, repo, authors
        self.usage, self.scheduler, self.delivery = usage, scheduler, delivery

    def allowed(self, message):
        return (message.chat.type == "private" and message.chat.id == self.repo.owner
                and message.from_user is not None and message.from_user.id == self.repo.owner)

    def say(self, text, keyboard=None, html=False):
        self.bot.send_message(self.repo.owner, text, reply_markup=keyboard,
                              parse_mode="HTML" if html else None, disable_web_page_preview=True)

    def ask(self, state, text, buttons=(), data=None):
        self.repo.set_state(state.value, data)
        self.say(text, kb.choices(buttons))

    def finish(self, text):
        self.repo.set_state()
        self.say(text, kb.close())

    def handle(self, message):
        try:
            value = (message.text or "").strip()
            if value == kb.CANCEL or value == "/cancel":
                self.finish("Операция отменена.")
            elif value.startswith("/"):
                command, _, arg = value.partition(" ")
                self.repo.set_state()
                self.command(command[1:].split("@")[0], arg.strip())
            else:
                self.answer(value)
        except UserError as exc:
            self.say(str(exc))
        except Exception as exc:
            logging.getLogger(__name__).error("Ошибка обработчика: %s", type(exc).__name__)
            self.say("Не удалось выполнить операцию. Проверьте /settings и повторите команду.")

    def command(self, cmd, arg):
        if cmd in ("start", "help"):
            self.say(HELP, kb.close())
        elif cmd == "add_author":
            self.ask(State.USERNAME, "Введите @username или ссылку на профиль X.")
            if arg:
                self.answer(arg)
        elif cmd == "authors":
            rows = self.repo.authors()
            if not rows:
                self.say("Список пуст. Добавьте автора через /add_author.")
            for i in range(0, len(rows), 12):
                self.say("\n".join(f"@{r['username']} · {r['display_name']} · "
                                   + ("активен" if r["is_active"] else "пауза")
                                   + (f" · {r['last_error']}" if r["last_error"] else "")
                                   for r in rows[i:i + 12]))
        elif cmd in ACTIONS:
            self.ask(State.ACTION, "Укажите автора из /authors.", data={"action": ACTIONS[cmd]})
            if arg:
                self.answer(arg)
        elif cmd == "interval":
            self.ask(State.INTERVAL, "Как часто проверять новые публикации X?", list(map(str, INTERVALS)))
        elif cmd == "budget":
            self.ask(State.BUDGET, f"Лимит в месяц: ${dollars(self.repo.settings()['monthly_budget_micros'])}. "
                     "Введите новый предел в USD. Ноль остановит платные чтения.")
        elif cmd == "reset_settings":
            self.ask(State.RESET, "Вернуть интервал и бюджет из .env?", [kb.YES])
        elif cmd == "settings":
            row = self.repo.settings()
            last = "пока не выполнялась с момента обновления."
            if row["last_check_at"]:
                last = f"{schedule_time(moment(row['last_check_at']))} — {row['last_check_result']}"
            retry = (f"Повтор выжимки: {schedule_time(moment(row['summary_retry_at']))}.\n"
                     if row["summary_retry_at"] else "")
            self.say(f"Язык: русский\nИнтервал: {row['digest_interval_hours']} ч\n"
                     f"Следующая плановая проверка: {schedule_time(moment(row['next_due_at']))}.\n"
                     f"Последняя плановая проверка: {last}\n{retry}"
                     f"Бюджет: ${dollars(row['monthly_budget_micros'])}/месяц.\n"
                     "Изменить: /interval, /budget. Сброс: /reset_settings.")
        elif cmd == "stats":
            authors, tweets, digests = self.repo.counts()
            requests, profiles, posts, cost = self.usage.stats()
            self.say(f"В базе: авторов {authors}, твитов {tweets}, дайджестов {digests}.\n"
                     f"За месяц UTC: запросов {requests}, профилей {profiles}, "
                     f"публикаций от X {posts}. Оценка с резервами: ${dollars(cost)}.")
        elif cmd == "digest":
            self.say(self.scheduler.manual())
        elif cmd == "history":
            if arg:
                self.archive(arg)
            else:
                rows = self.repo.history()
                self.say("\n".join(f"#{r['id']} · {r['period_end'][:10]} · "
                                   + ("отправлен" if r["status"] == "sent" else "в очереди")
                                   for r in rows) or "История пуста.")
        elif cmd == "forget_tweet":
            if not arg.isascii() or not arg.isdigit() or not 1 <= len(arg) <= 19:
                raise UserError("Укажите числовой ID из ссылки: /forget_tweet ID.")
            self.ask(State.ERASE, "Удалить этот твит, весь связанный выпуск и его сохранённые "
                     "исходники? Бот также попробует удалить части выпуска из чата.", [kb.YES], {"id": arg})
        elif cmd == "delivery":
            self.say(self.delivery.inspect_or_resolve(arg))
        else:
            self.say("Список команд доступен в /help.")

    def archive(self, value):
        number = int(value) if value.isascii() and value.isdigit() and len(value) <= 15 else None
        parts = self.repo.history(number) if number is not None else []
        if not parts:
            raise UserError("Используйте /history или /history N с номером из истории.")
        first, last = self.repo.source_period(number)
        source_note = ""
        if first:
            dates = (first,) if first == last else (first, last)
            times = " — ".join(moment(t).astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M:%S") for t in dates)
            source_note = f"Время публикации сохранённых источников в X: {times} МСК.\n"
        for part in parts:
            header = f"<b>Архив #{number} · часть {part['part']}</b>\n"
            self.say(header + (source_note if part["part"] == 1 else "") + part["html"], html=True)

    def answer(self, value):
        state, data = self.repo.conversation()
        if state == State.USERNAME:
            profile = self.authors.lookup(value)
            self.ask(State.CONFIRM, f"Добавить @{profile.username} ({profile.name})? "
                     "Отслеживание начнётся при подтверждении.", [kb.YES], asdict(profile))
        elif state == State.CONFIRM and value == kb.YES:
            self.repo.add(Profile(**data))
            self.finish("Автор добавлен. Отслеживаю публикации с этого момента.")
        elif state == State.INTERVAL:
            if value not in tuple(map(str, INTERVALS)):
                raise UserError("Выберите 3, 6, 12 или 24.")
            self.repo.interval(int(value))
            self.scheduler.refresh()
            self.finish(f"Проверяю X каждые {value} ч. Когда есть новые публикации, отправляю дайджест.")
        elif state == State.BUDGET:
            self.repo.budget(micros(value))
            self.finish("Месячный лимит сохранён.")
        elif state == State.RESET and value == kb.YES:
            self.repo.reset()
            self.scheduler.refresh()
            self.finish("Настройки восстановлены.")
        elif state == State.ACTION:
            name = self.authors.normalize(value)
            if not self.repo.author(name):
                raise UserError("Автор не найден в списке.")
            action = data["action"]
            if action == "remove":
                self.ask(State.REMOVE, f"Удалить @{name} и его сохранённые твиты?", [kb.YES],
                         {"username": name})
            elif action == "rename":
                self.ask(State.LABEL, "Введите новую подпись автора.", data={"username": name})
            else:
                self.repo.change_author(name, action)
                self.finish("Пауза включена." if action == "pause" else
                            "Отслеживание включено. Публикации за время паузы пропускаются.")
        elif state == State.ERASE and value == kb.YES:
            self.finish(self.delivery.erase(data["id"]))
        elif state == State.LABEL:
            self.repo.change_author(data["username"], "rename", value)
            self.finish("Подпись сохранена.")
        elif state == State.REMOVE and value == kb.YES:
            self.repo.change_author(data["username"], "remove")
            self.finish("Автор удалён.")
        elif state:
            self.say("Нажмите «Подтвердить» или «Отмена».")
        else:
            self.say("Выберите команду из /help.")


def register(bot, repo, authors, usage, scheduler, delivery):
    chat = Chat(bot, repo, authors, usage, scheduler, delivery)
    bot.register_message_handler(chat.handle, func=chat.allowed, content_types=["text"])
    return chat
