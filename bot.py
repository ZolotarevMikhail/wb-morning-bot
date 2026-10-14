# Бот «Утренняя сводка WB» (шаги 1–3)
# 1 — команды, 2 — данные WB, 3 — автоотправка сводки каждый день в 10:00 МСК.
"""Телеграм-бот: команды /summary, /ozon и утренняя авто-рассылка сводки."""
import json
import os
import subprocess
import threading
import time
import traceback
from datetime import datetime, timedelta

import telebot
from dotenv import load_dotenv

from wb_data import MSK, build_summary
from ozon_data import build_ozon_summary

load_dotenv()  # читает .env рядом с ботом — токены не попадают в код

_dir = os.path.dirname(os.path.abspath(__file__))


def _trim_log(path, keep=200):
    """Ротация: оставляем в логе только последние `keep` строк."""
    try:
        with open(path, encoding="utf-8") as log_file:
            tail = log_file.readlines()[-keep:]
        with open(path, "w", encoding="utf-8") as log_file:
            log_file.writelines(tail)
    except OSError:
        pass


# лог launchd + лог ручных запусков не должны расти бесконечно
_trim_log(os.path.join(_dir, "bot_launchd.log"))
_trim_log(os.path.join(_dir, "bot.log"))

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit(
        "Нет токена. Создайте файл .env со строкой: "
        "BOT_TOKEN=ваш_токен_от_BotFather"
    )

bot = telebot.TeleBot(TOKEN)

MORNING_HOUR = 10  # во сколько присылать сводку (по Москве)
SUBS_PATH = os.path.join(_dir, "subscribers.json")


def auto_summary_on():
    """Авто-рассылка включается строкой AUTO_SUMMARY=1 в .env."""
    return os.getenv("AUTO_SUMMARY", "0") == "1"


def ozon_configured():
    """True, если в .env есть оба ключа Ozon."""
    return bool(os.getenv("OZON_CLIENT_ID") and os.getenv("OZON_API_KEY"))


# --- подписчики утренней сводки -------------------------------------------

def load_subs():
    try:
        with open(SUBS_PATH, encoding="utf-8") as subs_file:
            return set(json.load(subs_file))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_subs(subs):
    with open(SUBS_PATH, "w", encoding="utf-8") as subs_file:
        json.dump(sorted(subs), subs_file)


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
    """Собирает сводку WB (+ Ozon, если настроен) и шлёт в чат."""
    token = os.getenv("WB_TOKEN")
    if not token:
        bot.send_message(
            chat_id, "Нет WB_TOKEN в .env — сводку собрать не могу."
        )
        return
    try:
        # WB и Ozon независимы: если одна площадка не отвечает,
        # вторая всё равно придёт.
        try:
            text = build_summary(token)
        except Exception as exc:
            text = f"⚠️ WB-блок не получен: {exc}"
        if ozon_configured():
            try:
                ozon_text = build_ozon_summary(
                    os.getenv("OZON_CLIENT_ID"), os.getenv("OZON_API_KEY")
                )
                text += "\n\n" + ozon_text
            except Exception as exc:
                text += f"\n\n⚠️ Ozon-блок не получен: {exc}"
        _send(chat_id, text)
    except Exception as exc:
        bot.send_message(chat_id, f"Не получилось собрать сводку: {exc}")


def morning_loop():
    """Спит до 10:00 МСК, рассылает сводку подписчикам — и так каждый день.
    Любая ошибка печатается в консоль, но цикл не останавливается."""
    while True:
        try:
            now = datetime.now(MSK)
            target = now.replace(
                hour=MORNING_HOUR, minute=0, second=0, microsecond=0
            )
            if now >= target:
                target += timedelta(days=1)
            delay = max((target - now).total_seconds(), 1)
            print(
                f"Следующая сводка: {target.strftime('%d.%m %H:%M')} МСК",
                flush=True,
            )
            time.sleep(delay)
            for chat_id in load_subs():
                send_summary_to(chat_id)
                # пауза между получателями, чтобы не упереться в лимиты WB
                time.sleep(5)
        except Exception:
            traceback.print_exc()  # ошибка не должна убить утреннюю рассылку
            time.sleep(60)


# --- команды ----------------------------------------------------------------

@bot.message_handler(commands=["start"])
def cmd_start(message):
    subs = load_subs()
    subs.add(message.chat.id)
    save_subs(subs)
    auto_on = auto_summary_on()
    text = (
        "Здравствуйте! Я бот «Утренняя сводка».\n\n"
        + (
            f"Авто-рассылка включена: каждый день в {MORNING_HOUR}:00 МСК "
            "пришлю сводку.\n"
            if auto_on
            else "Авто-рассылка выключена — сводка по запросу.\n"
        )
        + (
            "Ozon подключён — в сводку войдут обе площадки.\n"
            if ozon_configured()
            else "Ozon не подключён: добавь OZON_CLIENT_ID и OZON_API_KEY "
                 "в .env, если нужен.\n"
        )
        + "\nКоманды:\n"
        "/summary — сводка по WB и Ozon прямо сейчас\n"
        "/ozon — только Ozon\n"
        "/stop — отписаться от утренних сводок\n"
        "/help — что я умею"
    )
    bot.reply_to(message, text)


@bot.message_handler(commands=["stop"])
def cmd_stop(message):
    subs = load_subs()
    subs.discard(message.chat.id)
    save_subs(subs)
    bot.reply_to(
        message,
        "Отписал: утренние сводки приходить больше не будут. "
        "Вернуться — /start.",
    )


@bot.message_handler(commands=["help"])
def cmd_help(message):
    auto_line = (
        f"/start — подписаться на сводку в {MORNING_HOUR}:00 МСК\n"
        if auto_summary_on()
        else ""
    )
    bot.reply_to(
        message,
        "Сводка продаж и остатков Wildberries (+ Ozon, если настроен).\n"
        "/summary — сводка сейчас\n"
        "/ozon — только Ozon\n" + auto_line + "/stop — отписаться",
    )


@bot.message_handler(commands=["summary"])
def cmd_summary(message):
    """Сводка по команде: WB обязательно, Ozon добавляем, если настроен."""
    token = os.getenv("WB_TOKEN")
    if not token:
        bot.reply_to(
            message,
            "Нет токена WB: добавь строку WB_TOKEN=... в файл .env "
            "и перезапусти бота.",
        )
        return
    bot.send_chat_action(message.chat.id, "typing")
    try:
        text = build_summary(token)
        if ozon_configured():
            try:
                ozon_text = build_ozon_summary(
                    os.getenv("OZON_CLIENT_ID"), os.getenv("OZON_API_KEY")
                )
                text += "\n\n" + ozon_text
            except Exception as exc:
                text += f"\n\n⚠️ Ozon-блок не получен: {exc}"
        _send(None, text, reply=message)
    except RuntimeError as exc:    # наша осмысленная ошибка (лимиты API)
        bot.reply_to(message, str(exc))
    except Exception as exc:       # всё остальное — покажем коротко
        bot.reply_to(message, f"Не получилось получить данные: {exc}")


@bot.message_handler(commands=["ozon"])
def cmd_ozon(message):
    client_id = os.getenv("OZON_CLIENT_ID")
    api_key = os.getenv("OZON_API_KEY")
    if not client_id or not api_key:
        bot.reply_to(
            message,
            "Ozon не подключён: добавь в .env две строки — "
            "OZON_CLIENT_ID=... и OZON_API_KEY=... "
            "(кабинет Ozon → Настройки → API-ключи) — и перезапусти бота.",
        )
        return
    bot.send_chat_action(message.chat.id, "typing")
    try:
        _send(None, build_ozon_summary(client_id, api_key), reply=message)
    except RuntimeError as exc:
        bot.reply_to(message, str(exc))
    except Exception as exc:
        bot.reply_to(message, f"Не получилось получить данные Ozon: {exc}")


def vpn_is_up():
    """True, если маршрут в интернет идёт через VPN-туннель (utun).
    Проверка чисто локальная — смотрим таблицу маршрутов Mac, без единого
    пакета наружу. Поэтому без VPN бот не делает никаких попыток связи."""
    try:
        out = subprocess.run(
            ["route", "-n", "get", "-inet", "1.1.1.1"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
        for line in out.splitlines():
            if line.strip().startswith("interface:"):
                return line.split()[1].startswith("utun")
    except Exception:
        return False
    return False


def watch_vpn(vpn_ok):
    """Ждёт смену состояния VPN: печатает, когда туннель включился или дрогнул.
    Возвращает актуальное состояние (True — VPN есть)."""
    up = vpn_is_up()
    if up != vpn_ok:
        vpn_ok = up
        print(
            "VPN включился — работаю." if up else "VPN выключен — молчу, "
            "жду включения.",
            flush=True,
        )
    return up


def get_updates_safely(offset):
    """Получает обновления Telegram; если связь пропала — тихая короткая пауза.
    Возвращает (список обновлений, актуальный offset)."""
    try:
        return bot.get_updates(offset=offset, timeout=25), offset
    except Exception:
        print("Связь с Telegram пропала, жду 15 сек...", flush=True)
        time.sleep(15)
        return [], offset


def handle_updates(updates, offset):
    """Обрабатывает пачку обновлений и возвращает offset после последнего."""
    for update in updates:
        try:
            bot.process_new_updates([update])
        except Exception:
            traceback.print_exc()
        # запоминаем, что обработали, даже после сбоя
        offset = update.update_id + 1
    return offset


if __name__ == "__main__":
    if auto_summary_on():
        print(
            "Бот запущен. Авто-рассылка включена. "
            "Для остановки нажмите Ctrl+C."
        )
        threading.Thread(target=morning_loop, daemon=True).start()
    else:
        print(
            "Бот запущен в ручном режиме (AUTO_SUMMARY=0): сводка только "
            "по команде /summary. Для остановки нажмите Ctrl+C."
        )
    # Ждём VPN: пока туннеля нет — молчим и не лезем в сеть вообще
    # (проверка маршрута локальная, каждые 5 секунд). VPN включился — сразу
    # работаем.
    vpn_ok = vpn_is_up()
    print(
        "VPN есть — работаю."
        if vpn_ok
        else "VPN выключен — жду включения (проверяю раз в 5 сек).",
        flush=True,
    )
    offset = None
    while True:
        up = watch_vpn(vpn_ok)
        vpn_ok = up
        if not up:
            time.sleep(5)
            continue
        updates, offset = get_updates_safely(offset)
        offset = handle_updates(updates, offset)
