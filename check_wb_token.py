# Проверка WB-токена из ~/claudecode/.env против живого API WB.
# Тестирует все 3 канала данных бота: продажи, заказы, остатки (новый метод).
# Сам токен НЕ печатает. При успехе — вписывает WB_TOKEN в .env бота.
import os

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")

# 1. Достаём токен из ~/claudecode/.env (WB_API_KEY или eyJ-строка)
token = None
p = os.path.expanduser("~/claudecode/.env")
if os.path.exists(p):
    for line in open(p, encoding="utf-8", errors="ignore"):
        line = line.strip()
        if line.startswith("WB_API_KEY=") and len(line) > 50:
            token = line.split("=", 1)[1].strip()
            print(f"Токен из {p}: длина {len(token)}")
            break
if not token:
    raise SystemExit("WB_API_KEY не найден в ~/claudecode/.env")

H = {"Authorization": token}

# 2. Тест 1: продажи (v1 statistics) — GET
r = requests.get(
    "https://statistics-api.wildberries.ru/api/v1/supplier/sales",
    headers=H, params={"dateFrom": "2026-09-13", "flag": 0}, timeout=30,
)
print(f"Продажи  (v1 /supplier/sales):        {r.status_code}", end="")
if r.status_code == 200:
    print(f" — строк: {len(r.json())}")
else:
    print(f" — {r.text[:120]}")
sales_ok = r.status_code == 200

# 3. Тест 2: заказы (v1 statistics) — GET
r = requests.get(
    "https://statistics-api.wildberries.ru/api/v1/supplier/orders",
    headers=H, params={"dateFrom": "2026-09-13", "flag": 0}, timeout=30,
)
print(f"Заказы   (v1 /supplier/orders):       {r.status_code}", end="")
if r.status_code == 200:
    print(f" — строк: {len(r.json())}")
else:
    print(f" — {r.text[:120]}")
orders_ok = r.status_code == 200

# 4. Тест 3: остатки — НОВЫЙ метод (v1 stocks отключён 23.06.2026)
r = requests.post(
    "https://seller-analytics-api.wildberries.ru/api/analytics/v1/stocks-report/wb-warehouses",
    headers={**H, "Content-Type": "application/json"},
    json={"limit": 100000, "offset": 0}, timeout=60,
)
print(f"Остатки  (аналитика /wb-warehouses):  {r.status_code}", end="")
stocks_ok = r.status_code == 200
if r.status_code == 200:
    data = r.json()
    # показать структуру ответа без значений секретов (их тут и нет)
    if isinstance(data, dict):
        print(f" — ключи: {list(data.keys())[:6]}")
        for v in data.values():
            if isinstance(v, list) and v:
                print(f"   строк в списке: {len(v)}, поля первой: {list(v[0].keys())}")
                break
    elif isinstance(data, list):
        print(f" — строк: {len(data)}, поля первой: {list(data[0].keys()) if data else '—'}")
else:
    print(f" — {r.text[:150]}")

# 5. Вердикт
print()
if sales_ok and orders_ok and stocks_ok:
    lines = [l for l in open(ENV_PATH, encoding="utf-8") if l.strip() and not l.startswith("WB_TOKEN=")]
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"  # иначе дописанный токен слипнется с предыдущей строкой
    lines.append(f"WB_TOKEN={token}\n")
    open(ENV_PATH, "w", encoding="utf-8").writelines(lines)
    print("✅ Все 3 канала работают. Токен вписан в .env бота.")
    print("   Дальше: python3 -u wb_data.py, затем /summary в Telegram.")
elif sales_ok or orders_ok:
    print("⚠️ Продажи/заказы работают, остатки — нет (токену не хватает прав «Аналитика»).")
    print("   Вариант: в кабинете WB (Настройки → Доступ к API) создать токен")
    print("   с правами «Статистика» + «Аналитика» и заменить WB_API_KEY.")
else:
    print("❌ Основные каналы не отвечают — покажи этот вывод Claude.")