"""Run with python -m app.main. One polling process owns one private bot."""
import logging
import signal
import sys

import httpx
import telebot
from filelock import FileLock, Timeout
from telebot import apihelper

from .ai.summarizer import Summarizer
from .bot.handlers import register
from .config import Config
from .database.connection import Database
from .database.models import UserError
from .database.repository import Repository
from .services.author_service import Authors
from .services.budget_service import Budget
from .services.delivery import Delivery
from .services.digest_service import DigestService
from .services.scheduler import Scheduler
from .services.tweet_service import Timeline
from .services.twitter_client import TwitterClient

log = logging.getLogger(__name__)


def serve(cfg):
    db = Database(cfg.database)
    with (FileLock(str(db.path) + ".lock", timeout=0), httpx.Client() as http,
          httpx.Client(trust_env=False) as local_http):
        repo = Repository(db, cfg.owner, cfg.hours, cfg.budget)
        bot = telebot.TeleBot(cfg.telegram_token, threaded=False)
        apihelper.RETRY_ON_ERROR = False
        usage = Budget(repo, cfg.post_price, cfg.user_price)
        x = TwitterClient(http, cfg.bearer_token, usage)
        delivery = Delivery(repo, bot)
        delivery.recover()
        ai = Summarizer(local_http, cfg.ollama_url, cfg.model)
        digest = DigestService(repo, Timeline(repo, x), ai, delivery)
        scheduler = Scheduler(repo, digest, lambda text: bot.send_message(cfg.owner, text))
        register(bot, repo, Authors(repo, x), usage, scheduler, delivery)

        def stop(signum, frame):
            bot.stop_polling()

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, stop)
        try:
            bot.get_me()
        except Exception:
            raise UserError("Telegram недоступен. Проверьте TELEGRAM_BOT_TOKEN и подключение.") from None
        scheduler.start()
        log.info("Личный бот запущен. Расписание восстановлено из SQLite.")
        try:
            bot.infinity_polling(timeout=25, long_polling_timeout=20, allowed_updates=["message"],
                                 skip_pending=False)
        finally:
            scheduler.stop()
            bot.stop_bot()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Library exceptions can contain a Bot API URL with the token in its path.
    telebot.logger.disabled = True
    for name in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        serve(Config.load())
    except Timeout:
        log.error("База уже используется работающим экземпляром бота.")
        return 2
    except UserError as exc:
        log.error("%s", exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
