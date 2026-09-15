# Бот «Утренняя сводка WB» (шаги 1–3)
# 1 — команды, 2 — данные WB, 3 — автоотправка сводки каждый день в 10:00 МСК.
import json
import os
import threading
import time
import traceback
from datetime import datetime, timedelta

import telebot
from dotenv import load_dotenv

from wb_data import MSK, build_summary

load_dotenv()  # читает .env рядом с ботом — токены не попадают в код

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit(
        "Нет токена. Создайте файл .env со строкой: BOT_TOKEN=ваш_токен_от_BotFather"
    )

bot = telebot.TeleBot(TOKEN)

MORNING_HOUR = 10  # во сколько присылать сводку (по Москве)
SUBS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "subscribers.json")


# --- подписчики утренней сводки -------------------------------------------

def load_subs():
    try:
        with open(SUBS_PATH, encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_subs(subs):
    with open(SUBS_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(subs), f)


# --- утренняя рассылка ------------------------------------------------------

def _send(chat_id, text, reply=None):
    """Отправка с обрезкой под лимит Telegram (4096 символов)."""
    if len(text) > 4050:
        text = text[:4050] + "\n… (обрезано)"
    if reply is not None:
        bot.reply_to(reply, text)
    else:
        bot.send_message(chat_id, text)


def send_summary_to(chat_id):
    token = os.getenv("WB_TOKEN")
    if not token:
        bot.send_message(chat_id, "Нет WB_TOKEN в .env — сводку собрать не могу.")
        return
    try:
        _send(chat_id, build_summary(token))
    except Exception as e:
        bot.send_message(chat_id, f"Не получилось собрать сводку: {e}")


def morning_loop():
    """Спит до 10:00 МСК, рассылает сводку подписчикам — и так каждый день.
    Любая ошибка печатается в консоль, но цикл не останавливается."""
    while True:
        try:
            now = datetime.now(MSK)
            target = now.replace(hour=MORNING_HOUR, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            print(f"Следующая сводка: {target.strftime('%d.%m %H:%M')} МСК", flush=True)
            time.sleep(max((target - now).total_seconds(), 1))
            for chat_id in load_subs():
                send_summary_to(chat_id)
                time.sleep(5)  # пауза между получателями, чтобы не упереться в лимиты WB
        except Exception:
            traceback.print_exc()  # ошибка не должна убить утреннюю рассылку
            time.sleep(60)


# --- команды ----------------------------------------------------------------

@bot.message_handler(commands=["start"])
def cmd_start(message):
    subs = load_subs()
    subs.add(message.chat.id)
    save_subs(subs)
    text = (
        "Привет! Я бот «Утренняя сводка WB».\n\n"
        f"Ты подписан: каждый день в {MORNING_HOUR}:00 МСК пришлю сводку по кабинету "
        "Wildberries — продажи, заказы, заканчивающиеся товары.\n\n"
        "Команды:\n"
        "/summary — сводка прямо сейчас\n"
        "/stop — отписаться от утренних сводок\n"
        "/help — что я умею"
    )
    bot.reply_to(message, text)


@bot.message_handler(commands=["stop"])
def cmd_stop(message):
    subs = load_subs()
    subs.discard(message.chat.id)
    save_subs(subs)
    bot.reply_to(message, "Отписал: утренние сводки приходить больше не будут. Вернуться — /start.")


@bot.message_handler(commands=["help"])
def cmd_help(message):
    bot.reply_to(
        message,
        "Я присылаю сводку продаж и остатков Wildberries.\n"
        "/summary — сводка сейчас\n"
        "/start — подписаться на сводку в 10:00 МСК\n"
        "/stop — отписаться",
    )


@bot.message_handler(commands=["summary"])
def cmd_summary(message):
    # Тянем данные из кабинета WB и собираем сводку.
    token = os.getenv("WB_TOKEN")
    if not token:
        bot.reply_to(message, "Нет токена WB: добавь строку WB_TOKEN=... в файл .env и перезапусти бота.")
        return
    bot.send_chat_action(message.chat.id, "typing")
    try:
        _send(None, build_summary(token), reply=message)
    except RuntimeError as e:      # наша осмысленная ошибка (лимиты WB)
        bot.reply_to(message, str(e))
    except Exception as e:         # всё остальное — покажем коротко
        bot.reply_to(message, f"Не получилось получить данные WB: {e}")


if __name__ == "__main__":
    print("Бот запущен. Для остановки нажмите Ctrl+C.")
    threading.Thread(target=morning_loop, daemon=True).start()
    # без skip_pending: сообщения, присланные пока бот был выключен, обработаются
    bot.infinity_polling()