# Проверка WB-токена из ~/claudecode/.env против живого API WB.
# Тестирует все 3 канала данных бота: продажи, заказы, остатки (новый метод).
# Сам токен НЕ печатает. При успехе — вписывает WB_TOKEN в .env бота.
"""Проверка WB-токена по трем API WB и вписывание WB_TOKEN в .env бота."""
import os

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")
SALES_URL = "https://statistics-api.wildberries.ru/api/v1/supplier/sales"
ORDERS_URL = "https://statistics-api.wildberries.ru/api/v1/supplier/orders"
STOCKS_URL = (
    "https://seller-analytics-api.wildberries.ru"
    "/api/analytics/v1/stocks-report/wb-warehouses"
)


def find_token():
    """Ищет WB_API_KEY (или eyJ-строку) в ~/claudecode/.env. Токен печатает
    только по длине, сам файл с токеном не изменяет."""
    path = os.path.expanduser("~/claudecode/.env")
    if not os.path.exists(path):
        raise SystemExit("WB_API_KEY не найден в ~/claudecode/.env")
    with open(path, encoding="utf-8", errors="ignore") as env_file:
        for line in env_file:
            line = line.strip()
            if line.startswith("WB_API_KEY=") and len(line) > 50:
                token = line.split("=", 1)[1].strip()
                print(f"Токен из {path}: длина {len(token)}")
                return token
    raise SystemExit("WB_API_KEY не найден в ~/claudecode/.env")


def test_sales(token):
    """Тест 1: продажи (v1 statistics) — GET. Возвращает True/False."""
    response = requests.get(
        SALES_URL,
        headers={"Authorization": token},
        params={"dateFrom": "2026-09-13", "flag": 0},
        timeout=30,
    )
    print(f"Продажи  (v1 /supplier/sales):        {response.status_code}",
          end="")
    if response.status_code == 200:
        print(f" — строк: {len(response.json())}")
    else:
        print(f" — {response.text[:120]}")
    return response.status_code == 200


def test_orders(token):
    """Тест 2: заказы (v1 statistics) — GET. Возвращает True/False."""
    response = requests.get(
        ORDERS_URL,
        headers={"Authorization": token},
        params={"dateFrom": "2026-09-13", "flag": 0},
        timeout=30,
    )
    print(f"Заказы   (v1 /supplier/orders):       {response.status_code}",
          end="")
    if response.status_code == 200:
        print(f" — строк: {len(response.json())}")
    else:
        print(f" — {response.text[:120]}")
    return response.status_code == 200


def _describe_response(data):
    """Структура ответа остатков без значений секретов (их тут и нет)."""
    if isinstance(data, dict):
        print(f" — ключи: {list(data.keys())[:6]}")
        for value in data.values():
            if isinstance(value, list) and value:
                print(
                    f"   строк в списке: {len(value)}, "
                    f"поля первой: {list(value[0].keys())}"
                )
                break
    elif isinstance(data, list):
        keys = list(data[0].keys()) if data else "—"
        print(f" — строк: {len(data)}, поля первой: {keys}")


def test_stocks(token):
    """Тест 3: остатки — НОВЫЙ метод (v1 stocks отключён 23.06.2026)."""
    response = requests.post(
        STOCKS_URL,
        headers={"Authorization": token, "Content-Type": "application/json"},
        json={"limit": 100000, "offset": 0},
        timeout=60,
    )
    print(f"Остатки  (аналитика /wb-warehouses):  {response.status_code}",
          end="")
    ok = response.status_code == 200
    if ok:
        _describe_response(response.json())
    else:
        print(f" — {response.text[:150]}")
    return ok


def save_token(token):
    """Вписывает WB_TOKEN в .env бота без затирания остальных строк."""
    with open(ENV_PATH, encoding="utf-8") as env_file:
        lines = [
            line
            for line in env_file
            if line.strip() and not line.startswith("WB_TOKEN=")
        ]
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"  # иначе дописанный токен слипнется с предыдущей
    lines.append(f"WB_TOKEN={token}\n")
    with open(ENV_PATH, "w", encoding="utf-8") as env_file:
        env_file.writelines(lines)


def main():
    token = find_token()
    sales_ok = test_sales(token)
    orders_ok = test_orders(token)
    stocks_ok = test_stocks(token)

    # Вердикт
    print()
    if sales_ok and orders_ok and stocks_ok:
        save_token(token)
        print("✅ Все 3 канала работают. Токен вписан в .env бота.")
        print("   Дальше: python3 -u wb_data.py, затем /summary в Telegram.")
    elif sales_ok or orders_ok:
        print("⚠️ Продажи/заказы работают, остатки — нет "
              "(токену не хватает прав «Аналитика»).")
        print("   Вариант: в кабинете WB (Настройки → Доступ к API) создать "
              "токен")
        print("   с правами «Статистика» + «Аналитика» и заменить WB_API_KEY.")
    else:
        print("❌ Основные каналы не отвечают — покажи этот вывод Claude.")


if __name__ == "__main__":
    main()
