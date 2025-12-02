import time
import json
from pathlib import Path
from datetime import datetime
import requests
import logging
import os
import math

# ===========================
# 路径配置
# ===========================

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "etf.conf"
STATE_FILE = BASE_DIR / "etf_monitor_state.json"
LOG_FILE = BASE_DIR / "etf.log"

# ===========================
# 日志设置
# ===========================

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ===========================
# 读取配置文件
# ===========================

def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logging.exception(f"读取配置文件 {CONFIG_FILE} 失败")
        return None


CONFIG = load_config()
if CONFIG is None:
    print("配置文件读取失败，退出。")
    exit(1)

ETF_CONFIG = CONFIG.get("ETF_CONFIG", {})
STRATEGY = CONFIG.get("STRATEGY", {})

# ===========================
# 状态文件读写
# ===========================

def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {
        name: {
            "last_price": None,
            "tick": 0,
            "ma_short": None,
            "ma_long": None
        }
        for name in ETF_CONFIG.keys()
    }

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ===========================
# 行情数据获取
# ===========================

PRICE_SCALE = {
    "SH520890": 0.1,
    "SH515080": 0.1,
}

def get_price_from_api(symbol):
    raw_symbol = symbol.upper().strip()

    if raw_symbol.startswith("SH"):
        market = "1"
        code = raw_symbol[2:]
    else:
        market = "0"
        code = raw_symbol[2:]

    url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={market}.{code}&fields=f43"

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/"
    }

    resp = requests.get(url, headers=headers, timeout=5)
    data = resp.json()

    if not data.get("data") or not data["data"].get("f43"):
        raise Exception(f"行情数据为空: {symbol}")

    price_raw = data["data"]["f43"]
    price = price_raw / 100.0
    price = price * PRICE_SCALE.get(raw_symbol, 1)

    return round(price, 3)

# ===========================
# 历史 K 线，用来计算 MA
# ===========================

def get_history_close(symbol, days=400):
    raw_symbol = symbol.upper()
    if raw_symbol.startswith("SH"):
        market = "1"
        code = raw_symbol[2:]
    else:
        market = "0"
        code = raw_symbol[2:]

    url = f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={market}.{code}&klt=101&fqt=1&lmt={days}"

    headers = {"User-Agent": "Mozilla/5.0"}

    resp = requests.get(url, headers=headers, timeout=5)
    data = resp.json()

    if not data.get("data") or not data["data"].get("klines"):
        raise Exception(f"无法获取历史 K 线: {symbol}")

    closes = []
    for item in data["data"]["klines"]:
        arr = item.split(",")
        closes.append(float(arr[2]))

    return closes

def calc_ma(closes, length):
    if len(closes) < length:
        return None
    return sum(closes[-length:]) / length

# ===========================
# 推送功能
# ===========================

PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "")
PUSHPLUS_URL = "http://www.pushplus.plus/send"

def send_notification(msg):
    if not PUSHPLUS_TOKEN:
        logging.info("未配置 PushPlus Token，跳过推送")
        return

    payload = {
        "token": PUSHPLUS_TOKEN,
        "title": "ETF 策略信号",
        "content": msg,
        "template": "txt"
    }

    try:
        resp = requests.post(PUSHPLUS_URL, json=payload, timeout=5)
        if resp.json().get("code") != 200:
            logging.error(f"PushPlus 推送失败: {resp.text}")
    except Exception:
        logging.exception("PushPlus 推送异常")

# ===========================
# 区间定义
# ===========================

def get_zone(price, ma150, ma300):
    if price <= ma300:
        return "BELOW_MA300"
    if ma300 < price < ma150:
        return "BETWEEN_300_150"
    if ma150 <= price < ma150 * 1.2:
        return "BOX_AREA"
    return "ABOVE_120"

# ===========================
# 核心策略逻辑
# ===========================

def strategy_for_etf(name, cfg, state):
    symbol = cfg["symbol"]

    current_price = get_price_from_api(symbol)
    closes = get_history_close(symbol, STRATEGY.get("fetch_history_days", 400))

    ma150 = calc_ma(closes, STRATEGY.get("ma_period_short", 150))
    ma300 = calc_ma(closes, STRATEGY.get("ma_period_long", 300))

    if not ma150 or not ma300:
        return []

    state[name]["ma_short"] = ma150
    state[name]["ma_long"] = ma300
    last_price = state[name]["last_price"]
    state[name]["last_price"] = current_price

    base_units = cfg["base_units"]
    target_units = cfg["target_units"]
    double_target = cfg["double_target_factor"] * target_units

    interval_pct = cfg["ma_interval_percent"]
    add_pct = cfg["add_percent"]
    sell_pct = cfg["sell_percent"]
    sell_up_pct = cfg["sell_trigger_up_percent"]
    stop_add_above = cfg["stop_add_above_percent"]

    grid_pct = cfg["grid_box_percent"]
    grid_units_pct = cfg["grid_box_units_percent"]

    zone = get_zone(current_price, ma150, ma300)

    messages = []
    now = datetime.now().strftime("%Y.%m.%d.%H:%M")

    messages.append(
        f"{name} 区间: {zone}\n"
        f"当前价: {current_price}\n"
        f"MA150={ma150:.4f}, MA300={ma300:.4f}"
    )

    # ---- BELOW MA300：停止操作 ----
    if zone == "BELOW_MA300":
        messages.append(f"{now} 当前价格已跌破 MA300，所有操作停止。")
        return messages

    # ---- MA300~MA150：建立底仓逻辑 ----
    if zone == "BETWEEN_300_150":
        diff = ma150 - ma300
        interval = diff * interval_pct
        low_price = ma300
        steps = int((current_price - low_price) / interval) + 1

        add_units = int(target_units * add_pct * steps)
        add_units = min(add_units, double_target - base_units)

        messages.append(
            f"{now} 加仓区间：价格在 MA300~MA150，建议加仓 {add_units} 单位"
        )
        return messages

    # ---- 箱体区：MA150 ~ MA150*1.2 ----
    if zone == "BOX_AREA":
        if last_price is None:
            return messages

        last_grid = int((last_price - ma150) / (ma150 * grid_pct))
        current_grid = int((current_price - ma150) / (ma150 * grid_pct))

        if current_grid > last_grid:
            units = int(target_units * grid_units_pct)
            messages.append(
                f"{now} 箱体区网格卖出：价格从网格 {last_grid} 升到 {current_grid}，卖出 {units} 单位"
            )
        elif current_grid < last_grid:
            units = int(target_units * grid_units_pct)
            messages.append(
                f"{now} 箱体区网格买入：价格从网格 {last_grid} 降到 {current_grid}，买入 {units} 单位"
            )

        return messages

    # ---- 超强趋势区：价格 > MA150*1.2 ----
    if zone == "ABOVE_120":
        last_ratio = (last_price - ma150) / ma150 if last_price else 0
        now_ratio = (current_price - ma150) / ma150

        step_last = int(last_ratio / sell_up_pct)
        step_now = int(now_ratio / sell_up_pct)

        if step_now > step_last:
            sell_units = int(target_units * sell_pct * (step_now - step_last))
            messages.append(
                f"{now} 趋势区强势上涨：卖出 {sell_units} 单位"
            )

        return messages

    return messages

# ===========================
# 主循环
# ===========================

def main_loop():
    state = load_state()
    logging.info("ETF 策略脚本启动完成")

    while True:
        all_messages = []

        for name, cfg in ETF_CONFIG.items():
            try:
                msgs = strategy_for_etf(name, cfg, state)
                all_messages.extend(msgs)
            except Exception:
                logging.exception(f"{name} 策略执行出错")

        if all_messages:
            message = "\n\n".join(all_messages)
            send_notification(message)
            logging.info("推送:\n" + message)

        save_state(state)
        time.sleep(600)


if __name__ == "__main__":
    main_loop()
