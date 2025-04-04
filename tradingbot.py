import re
import csv
import os
import pytz
import gspread
import schedule
import time
import requests
from datetime import datetime
from telethon.sync import TelegramClient, events
from binance.client import Client
from binance.enums import *
from apscheduler.schedulers.background import BackgroundScheduler
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv

# === 🌱 Cargar variables de entorno desde .env ===
load_dotenv()

# === ✅ Validar variables requeridas ===
required_env_vars = [
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_SESSION",
    "SIGNAL_CHANNEL_ID",
    "CAPITAL_USDT",
    "RISK_PER_TRADE",
    "TARGET_INDEX",
    "CSV_LOG_FILE",
    "GOOGLE_SHEET_NAME",
    "USE_TESTNET",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_BOT_CHAT_ID",
    "TRADING_PIT_CHANNEL_ID"
]

def notify_via_bot(text):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_BOT_CHAT_ID")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    requests.post(url, data=payload)

# Agregar claves específicas según el entorno
if os.getenv("USE_TESTNET", "True") == "True":
    required_env_vars += ["TESTNET_API_KEY", "TESTNET_API_SECRET"]
else:
    required_env_vars += ["REAL_API_KEY", "REAL_API_SECRET"]

missing_vars = [var for var in required_env_vars if os.getenv(var) is None]
if missing_vars:
    raise EnvironmentError(f"❌ Faltan variables en el archivo .env: {', '.join(missing_vars)}")

# === 🔐 BINANCE CONFIG ===
USE_TESTNET = os.getenv("USE_TESTNET", "True") == "True"

if USE_TESTNET:
    API_KEY = os.getenv("TESTNET_API_KEY")
    API_SECRET = os.getenv("TESTNET_API_SECRET")
else:
    API_KEY = os.getenv("REAL_API_KEY")
    API_SECRET = os.getenv("REAL_API_SECRET")

binance = Client(API_KEY, API_SECRET)
if USE_TESTNET:
    testnet_base_url = "https://testnet.binancefuture.com"
    binance.FUTURES_URL = testnet_base_url + "/fapi"

# === ⚙️ TELEGRAM CONFIG ===
api_id = int(os.getenv("TELEGRAM_API_ID"))
api_hash = os.getenv("TELEGRAM_API_HASH")
session_name = os.getenv("TELEGRAM_SESSION")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))

# === 📌 Channel with special signal format ===
signal_channel_id = int(os.getenv("SIGNAL_CHANNEL_ID"))
trading_pit_channel_id = int(os.getenv("TRADING_PIT_CHANNEL_ID"))

# === ⚙️ BOT CONFIG ===
CAPITAL_USDT = float(os.getenv("CAPITAL_USDT"))
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE"))
TARGET_INDEX = int(os.getenv("TARGET_INDEX"))
SIMULATION_MODE = os.getenv("SIMULATION_MODE", "False") == "True"
CSV_LOG_FILE = os.getenv("CSV_LOG_FILE")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")

# === Google Sheets Setup ===
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
credentials = ServiceAccountCredentials.from_json_keyfile_name("telegram-signals-455719-e9ea546e10ed.json", scope)
gsheets_client = gspread.authorize(credentials)
gsheets_sheet = gsheets_client.open(GOOGLE_SHEET_NAME).sheet1

client = TelegramClient(session_name, api_id, api_hash)

# === Enviar notificación al iniciar el bot ===
def notify_telegram(message):
    notify_via_bot(message)

# Mensaje inicial indicando entorno
async def startup_notify():
    if USE_TESTNET:
        msg = "🚧 Bot iniciado en modo TESTNET..."
    elif SIMULATION_MODE:
        msg = "🧪 Bot iniciado en modo SIMULACIÓN..."
    else:
        msg = "✅ Bot iniciado en modo REAL..."
    notify_via_bot(msg)

with client:
    client.loop.run_until_complete(startup_notify())

# === Funciones utilitarias ===
def parse_signal(text):
    signal_type = 'Long' if '🟢 Long' in text else ('Short' if '🔴 Short' in text else None)
    if not signal_type:
        return None
    pattern = r"""Name:\s*(?P<symbol>[\w/]+).*?
Margin mode:\s*(?P<margin>[\w\s]+\(\d+X\)).*?
Entry price\(USDT\):\s*(?P<entry>\d+(\.\d+)?).*?
Targets\(USDT\):\s*
1\)\s*(?P<tp1>\d+(\.\d+)?).*?
2\)\s*(?P<tp2>\d+(\.\d+)?).*?
3\)\s*(?P<tp3>\d+(\.\d+)?).*?
4\)\s*(?P<tp4>\d+(\.\d+)?).*?
5\)\s*🔝 unlimited"""
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        return None
    data = match.groupdict()
    return {
        'type': signal_type,
        'symbol': data['symbol'].replace('/', ''),
        'entry': float(data['entry']),
        'targets': [float(data['tp1']), float(data['tp2']), float(data['tp3']), float(data['tp4'])]
    }

def calculate_position_size(entry, sl, capital, risk_pct):
    risk = capital * risk_pct
    distance = abs(entry - sl)
    return round(risk / distance, 3) if distance != 0 else 0

def is_duplicate(symbol, signal_type):
    if not os.path.exists(CSV_LOG_FILE):
        return False
    with open(CSV_LOG_FILE, 'r') as f:
        for line in f:
            if symbol in line and signal_type in line and 'Pending' in line:
                return True
    return False

def log_to_csv(data, qty, tp, sl):
    exists = os.path.exists(CSV_LOG_FILE)
    with open(CSV_LOG_FILE, 'a', newline='') as f:
        writer = csv.writer(f)
        if not exists:
            writer.writerow(['Date', 'Type', 'Symbol', 'Entry', 'TP', 'SL', 'Qty', 'Result'])
        writer.writerow([datetime.now().strftime('%Y-%m-%d %H:%M:%S'), data['type'], data['symbol'], data['entry'], tp, sl, qty, 'Pending'])

def log_to_sheets(data, qty, tp, sl):
    row = [datetime.now().strftime('%Y-%m-%d %H:%M:%S'), data['type'], data['symbol'], data['entry'], tp, sl, qty, 'Pending']
    gsheets_sheet.append_row(row)

def update_csv_result(symbol, result):
    rows = []
    with open(CSV_LOG_FILE, 'r') as f:
        rows = list(csv.reader(f))
    for row in rows[1:]:
        if row[2] == symbol and row[7] == 'Pending':
            row[7] = result
            break
    with open(CSV_LOG_FILE, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerows(rows)

def monitor_trade(data, tp, sl):
    symbol = data['symbol']
    signal_type = data['type']
    while True:
        price = float(binance.futures_ticker_price(symbol=symbol)['price'])
        if signal_type == 'Long':
            if price >= tp:
                notify_telegram(f"✅ TP hit for {symbol} ({price})")
                update_csv_result(symbol, 'TP')
                break
            elif price <= sl:
                notify_telegram(f"❌ SL hit for {symbol} ({price})")
                update_csv_result(symbol, 'SL')
                break
        else:
            if price <= tp:
                notify_telegram(f"✅ TP hit for {symbol} ({price})")
                update_csv_result(symbol, 'TP')
                break
            elif price >= sl:
                notify_telegram(f"❌ SL hit for {symbol} ({price})")
                update_csv_result(symbol, 'SL')
                break
        time.sleep(10)

def execute_trade(data):
    entry = data['entry']
    tp = data['targets'][TARGET_INDEX - 1]
    signal_type = data['type']
    symbol = data['symbol']
    sl = entry - (tp - entry) if signal_type == 'Long' else entry + (entry - tp)
    qty = calculate_position_size(entry, sl, CAPITAL_USDT, RISK_PER_TRADE)
    if is_duplicate(symbol, signal_type):
        print(f"⚠️ Trade already exists for {symbol} {signal_type}, skipping.")
        return
    log_to_csv(data, qty, tp, sl)
    log_to_sheets(data, qty, tp, sl)
    notify_telegram(f"📈 New trade: {signal_type} {symbol}\nEntry: {entry} | TP: {tp} | SL: {sl} | Qty: {qty}")
    if not SIMULATION_MODE:
        side = SIDE_BUY if signal_type == 'Long' else SIDE_SELL
        opposite = SIDE_SELL if signal_type == 'Long' else SIDE_BUY
        binance.futures_create_order(symbol=symbol, side=side, type=ORDER_TYPE_MARKET, quantity=qty)
        binance.futures_create_order(symbol=symbol, side=opposite, type=ORDER_TYPE_TAKE_PROFIT_MARKET, stopPrice=tp, closePosition=True, timeInForce='GTC')
        binance.futures_create_order(symbol=symbol, side=opposite, type=ORDER_TYPE_STOP_MARKET, stopPrice=sl, closePosition=True, timeInForce='GTC')
    monitor_trade(data, tp, sl)

def daily_summary():
    tz = pytz.timezone("America/Costa_Rica")
    today = datetime.now(tz).date().strftime('%Y-%m-%d')
    tp_count = sl_count = 0
    profit = 0
    if not os.path.exists(CSV_LOG_FILE):
        return
    with open(CSV_LOG_FILE, 'r') as f:
        next(f)
        for row in csv.reader(f):
            date, ttype, symbol, entry, tp, sl, qty, result = row
            if today in date:
                if result == 'TP':
                    tp_count += 1
                    profit += float(tp) - float(entry) if ttype == 'Long' else float(entry) - float(tp)
                elif result == 'SL':
                    sl_count += 1
                    profit -= abs(float(entry) - float(sl))
    total = tp_count + sl_count
    effectiveness = round(tp_count / total * 100, 2) if total > 0 else 0
    summary = f"📊 Daily Summary ({today})\nTrades: {total}\nNet Profit: {round(profit, 4)} USDT\nEffectiveness: {effectiveness}%"
    notify_telegram(summary)

# Programar resumen diario a las 6 pm hora Costa Rica
scheduler = BackgroundScheduler()
scheduler.add_job(daily_summary, 'cron', hour=18, minute=0, timezone='America/Costa_Rica')
scheduler.start()

# === Iniciar escucha del canal de señales ===
async def main():
    # Escuchar canal adicional: JANHTRADERS SPIKE DETECTOR
    extra_channel_id = -1002292329542
    extra_entity = await client.get_entity(extra_channel_id)

    @client.on(events.NewMessage(chats=extra_entity))
    async def handle_spike_signal(event):
        message_text = event.message.message
        if "📈 COMPRA 📈" in message_text:
            print("📡 Señal recibida desde JANHTRADERS:")
            print(message_text)
            await client.send_message(TELEGRAM_CHAT_ID, f"📡 Señal recibida desde JANHTRADERS:\n\n{message_text}")
            await client.send_message(TELEGRAM_CHAT_ID, "mensaje aquí")


    entity = await client.get_entity(signal_channel_id)
    @client.on(events.NewMessage(chats=entity))
    async def handler(event):
        data = parse_signal(event.message.message)
        if data:
            execute_trade(data)
    print("🔌 Listening for signals...")
    await client.run_until_disconnected()

    # === Canal adicional con señales Trading Pit Signal ===
    fx_channel_id = trading_pit_channel_id
    fx_entity = await client.get_entity(fx_channel_id)

    @client.on(events.NewMessage(chats=fx_entity))
    async def handle_fx_signal(event):
        text = event.message.message
        match = re.search(r"(?P<symbol>[A-Z]{3}/[A-Z]{3}) (?P<side>BUY|SELL) @ (?P<entry>\\d+\\.\\d+)", text)
        if match:
            symbol = match.group("symbol")
            side = match.group("side")
            entry = match.group("entry")

            tp_matches = re.findall(r"TP\\d\\s*[–-]\\s*(\\d+\\.\\d+)", text)
            sl_match = re.search(r"SL\\s*[–-]\\s*(\\d+\\.\\d+)", text)

            message = f"🔥 *Nueva señal detectada*\n{symbol} - *{side}* @ {entry}"
            if tp_matches:
                message += f"\nTPs: {' | '.join(tp_matches)}"
            if sl_match:
                message += f"\nSL: {sl_match.group(1)}"

            print("📥 Señal FX recibida:")
            print(text)
            notify_via_bot(message)


with client:
    client.loop.run_until_complete(main())