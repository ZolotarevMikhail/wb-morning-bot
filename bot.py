# Бот «Утренняя сводка WB» (шаги 1–3)
# 1 — команды, 2 — данные WB, 3 — автоотправка сводки каждый день в 10:00 МСК.
"""Телеграм-бот: команды /summary, /ozon и утренняя авто-рассылка сводки."""
import json
import os
import subprocess
import sys
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


# лог ручных запусков не должен расти бесконечно (launchd пишет свой в .plist)
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
        # битый файл не прячем: сохраняем рядом и орём в лог — молчаливая
        # «рассылка в никуда» (ревью 30.10.2026)
        if os.path.exists(SUBS_PATH):
            try:
                os.replace(SUBS_PATH, SUBS_PATH + "-битый.json")
                print("subscribers.json был бит — сохранён как "
                      "-битый.json, список подписчиков пуст.", flush=True)
            except OSError:
                pass
        return set()


def save_subs(subs):
    # атомарная запись: временный файл + переименование (краш посреди
    # записи не оставит оборванный json)
    tmp = SUBS_PATH + "-временная"
    with open(tmp, "w", encoding="utf-8") as subs_file:
        json.dump(sorted(subs), subs_file)
    os.replace(tmp, SUBS_PATH)


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
    Любая ошибка печатается в консоль, но цикл не останавливается.
    Перезапуск процесса после 10:00 сводку дня не теряет: если флага
    «сегодня уже рассылали» нет — догоняем сразу (повторная полная
    доставка возможна только при сбое посреди рассылки)."""
    now = datetime.now(MSK)
    if now.hour >= MORNING_HOUR and not сводка_сегодня_ушла():
        print("Перезапуск после 10:00 — досылаю сводку за сегодня.",
              flush=True)
        deliver_morning_summary()
        отметить_шлёт()
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
            deliver_morning_summary()
            отметить_шлёт()
        except Exception:
            traceback.print_exc()  # ошибка не должна убить утреннюю рассылку
            time.sleep(60)


RETRY_PAUSE_MIN = 30  # пауза между повторами утренней сводки
RETRY_LIMIT = 8       # максимум повторов за день (8 × 30 мин = 4 часа)

# сторож при активной рассылке себя не перезапускает, чтобы не рвать её
рассылка_идёт = threading.Event()
# момент последнего «409: два экземпляра бота» — при нём перезапуск бессмыслен
последний_409 = 0.0
FLUSH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "сводка-флаг.json")


def сводка_сегодня_ушла() -> bool:
    """Читает флаг «сегодняшняя сводка уже рассылалась» — переживает
    перезапуск процесса (os.execv) и нужен догону пропущенного выпуска."""
    try:
        with open(FLUSH_FILE, encoding="utf-8") as f:
            st = json.load(f)
        return st.get("дата") == datetime.now(MSK).strftime("%Y-%m-%d")
    except Exception:
        return False


def отметить_шлёт() -> None:
    """Фиксирует факт рассылки за сегодня."""
    try:
        with open(FLUSH_FILE, "w", encoding="utf-8") as f:
            json.dump({"дата": datetime.now(MSK).strftime("%Y-%m-%d")}, f)
    except OSError:
        traceback.print_exc()


def ждать_vpn():
    """Утренний поток живёт вне главного цикла, поэтому ждёт VPN теми же
    вратами (ревью 30.10.2026): без туннеля — пауза, а не мгновенный
    «WB-блок не получен» и потерянный выпуск."""
    while not vpn_is_up():
        print(
            "VPN выключен — сводка ждёт включения (проверка раз в 5 мин).",
            flush=True,
        )
        time.sleep(300)


def deliver_morning_summary():
    """Рассылает сводку подписчикам; если сеть глухая — повторяет.
    Два правила починки (ревью 30.10.2026):
    1. ошибку СБОРА данных (WB/Ozon недоступен) повторяем целиком —
       раньше сводка молча терялась до завтра;
    2. повтор ОТПРАВКИ идёт только тем чатам, кому не ушло: один
       заблокировавший бот получатель больше не шлёт остальным копии.
    Пока идёт рассылка, сторож перезапуска себе не производит."""
    ждать_vpn()
    рассылка_идёт.set()
    try:
        token = os.getenv("WB_TOKEN")
        if not token:
            text = "Нет WB_TOKEN в .env — сводку собрать не могу."
            for chat_id in load_subs():
                _send(chat_id, text)
            return
        # сбор сводки: до RETRY_LIMIT попыток, пауза между ними
        text = None
        last_exc = None
        for attempt in range(1, RETRY_LIMIT + 1):
            try:
                text = build_summary(token)
                if ozon_configured():
                    text += "\n\n" + build_ozon_summary(
                        os.getenv("OZON_CLIENT_ID"),
                        os.getenv("OZON_API_KEY"),
                    )
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                traceback.print_exc()
                if attempt < RETRY_LIMIT:
                    print(
                        f"Сводку собрать не удалось (попытка {attempt}) — "
                        f"повтор через {RETRY_PAUSE_MIN} мин.",
                        flush=True,
                    )
                    time.sleep(RETRY_PAUSE_MIN * 60)
        if last_exc is not None:
            # собрать не вышло до конца — рассылать нечего,
            # следующий утренний цикл попробует сам
            print("Сводку собрать так и не удалось.", flush=True)
            return
        недоставлены = list(load_subs())
        for attempt in range(1, RETRY_LIMIT + 1):
            ещё = []
            for chat_id in недоставлены:
                try:
                    _send(chat_id, text)
                    time.sleep(5)  # пауза, чтобы не упереться в лимиты
                except Exception:
                    traceback.print_exc()
                    ещё.append(chat_id)
            недоставлены = ещё
            if not недоставлены:
                return
            if attempt < RETRY_LIMIT:
                print(
                    f"Не ушло {len(недоставлены)} чат(ов) — повтор через "
                    f"{RETRY_PAUSE_MIN} мин.",
                    flush=True,
                )
                time.sleep(RETRY_PAUSE_MIN * 60)
        print("Часть подписчиков так и не получила сводку.", flush=True)
    finally:
        рассылка_идёт.clear()


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
    try:
        bot.send_chat_action(message.chat.id, "typing")
    except Exception:
        pass  # «печатает…» — косметика: обрыв сети не должен рвать ответ
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
    try:
        bot.send_chat_action(message.chat.id, "typing")
    except Exception:
        pass  # «печатает…» — косметика: обрыв сети не должен рвать ответ
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


STALE_SEC = 600  # столько секунд молчания Telegram подряд = перезапуск себя

last_poll_ok = time.time()  # момент последнего успешного опроса Telegram


def _restart_self():
    """Перезапуск своего процесса: свежие соединения вместо зависших.
    Хук сбоя, проверенный 26.10: туннель VPN «умирает наполовину» — процесс
    жив, а обновления не приходят, и без перезапуска бот молчит неделями.
    launchd (KeepAlive=true) страхует, если процесс всё же умрёт."""
    print(
        "Бот не получает Telegram дольше 10 минут — перезапускаю себя.",
        flush=True,
    )
    os.execv(sys.executable, [sys.executable, os.path.abspath(sys.argv[0])])


def _watchdog():
    """Стража глухоты (фоновый поток): VPN есть, а Telegram недоступен
    дольше 10 минут подряд — сам себя перезапускает. Обычные паузы связи
    (до 40 секунд на попытку) выглядят иначе и перезапуск не вызывают.
    Перезапуск не стреляет: 1) посреди рассылки сводки; 2) после 409
    (два экземпляра — перезапуск бесполезен, двойника убирает человек)."""
    while True:
        time.sleep(60)
        try:
            if (vpn_is_up()
                    and not рассылка_идёт.is_set()
                    and time.time() - последний_409 > STALE_SEC
                    and time.time() - last_poll_ok > STALE_SEC):
                _restart_self()
        except Exception:
            traceback.print_exc()


def get_updates_safely(offset):
    """Получает обновления Telegram; если связь пропала — тихая короткая пауза.
    Возвращает (список обновлений, актуальный offset)."""
    global last_poll_ok, последний_409
    try:
        updates = bot.get_updates(offset=offset, timeout=25)
        last_poll_ok = time.time()  # успех — стражу глухоты сдерживаем
        return updates, offset
    except Exception as exc:
        if "409" in str(exc):
            # 409 Conflict = работает второй экземпляр бота. Перезапуск
            # от этого не лечит никогда — сторож должен стоять, а в лог
            # пишем, что чинить надо двойника.
            последний_409 = time.time()
            print(
                "Telegram отвечает 409: судя по всему, запущен второй "
                "экземпляр бота. Перезапуск здесь не поможет — закрой "
                "двойника (проверь ручной запуск).", flush=True,
            )
            time.sleep(15)
            return [], offset
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
    threading.Thread(target=_watchdog, daemon=True).start()
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
