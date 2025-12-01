import time
import json
from pathlib import Path
from datetime import datetime
import math
import requests


# ========= 配置区 =========

STATE_FILE = Path("etf_monitor_state.json")

# 你要监控的两只 ETF，先随便起名字，后面替换成自己的代码即可
ETF_CONFIG = {
    "港股红利ETF": {  
        "symbol": "SH520890",  # 换成你真实的代码，注意前面加 SH / SZ
        "base_price": 1.47,   # 你实际底仓买入价
        "grid_pct": 0.04      # 网格间距 4%
    },
    "A股红利ETF": {  
        "symbol": "SZ515080,   
        "base_price": 1.50,
        "grid_pct": 0.04
    }
}

POLL_INTERVAL_SECONDS = 600  # 调试先改成 10 秒一轮，确认逻辑正确后再改回 600（10分钟）


# ========= 状态读写 =========

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    # 初始 state
    return {name: {"last_price": None, "tick": 0} for name in ETF_CONFIG.keys()}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ========= 获取价格函数=========

def get_price_from_api(symbol: str, tick: int = 0) -> float:
    """
    使用东方财富 push2 接口获取实时价格。
    symbol 格式建议：'SH515080' 或 'SZ159920'
    """
    symbol = symbol.upper().strip()
    if symbol.startswith("SH"):
        market = "1"   # 1 = 上证
        code = symbol[2:]
    elif symbol.startswith("SZ"):
        market = "0"   # 0 = 深证
        code = symbol[2:]
    else:
        # 默认按上证处理，你也可以根据自己实际情况改
        market = "1"
        code = symbol

    secid = f"{market}.{code}"
    url = (
        "https://push2.eastmoney.com/api/qt/stock/get"
        f"?secid={secid}&fields=f43"
    )

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/"
    }

    resp = requests.get(url, headers=headers, timeout=5)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("data") or data["data"].get("f43") in (None, 0):
        raise ValueError(f"行情数据为空: {symbol}, 返回: {data}")

    price_raw = data["data"]["f43"]   # 单位是“分”
    price = price_raw / 100.0
    return float(price)



# ========= 通知函数 =========

def send_notification(message: str):
    """
    调试阶段先打印到终端。
    之后你可以在这里加：
      - 发邮件
      - Telegram Bot
      - 企业微信/钉钉机器人 等
    """
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 通知：\n{message}\n")


# ========= 核心：单只 ETF 网格检查 =========

def check_signals_for_etf(name: str, cfg: dict, state: dict):
    """
    对单只 ETF 进行网格检查。
    返回要发的消息字符串列表。
    """
    symbol = cfg["symbol"]
    base_price = cfg["base_price"]
    grid_pct = cfg["grid_pct"]

    # tick 用来让模拟价格随时间变化
    tick = state.get(name, {}).get("tick", 0) + 1
    state[name]["tick"] = tick

    current_price = get_price_from_api(symbol, tick)
    last_price = state.get(name, {}).get("last_price")

    if name not in state:
        state[name] = {}
    state[name]["last_price"] = current_price

    # 第一次运行：只记录价格，不发信号
    if last_price is None:
        print(f"{name} 首次价格记录: {current_price}")
        return []

    messages = []

    price_ratio_now = current_price / base_price
    price_ratio_last = last_price / base_price

    # 格子编号 n: price = base_price * (1 + n * grid_pct)
    current_grid = int((price_ratio_now - 1) / grid_pct)
    last_grid = int((price_ratio_last - 1) / grid_pct)

    if current_grid > last_grid:
        # 向上穿越，触发卖出网格
        for g in range(last_grid + 1, current_grid + 1):
            level_price = base_price * (1 + g * grid_pct)
            msg = (f"{name} ({symbol}) 触发【卖出网格】:\n"
                   f"- 网格编号: {g}\n"
                   f"- 参考卖出价: {level_price:.4f}\n"
                   f"- 当前价: {current_price:.4f}\n"
                   f"- 建议：减一档网格仓（例如减 1% 总资金），不动底仓。")
            messages.append(msg)

    elif current_grid < last_grid:
        # 向下穿越，触发买入网格
        for g in range(last_grid - 1, current_grid - 1, -1):
            level_price = base_price * (1 + g * grid_pct)
            msg = (f"{name} ({symbol}) 触发【买入网格】:\n"
                   f"- 网格编号: {g}\n"
                   f"- 参考买入价: {level_price:.4f}\n"
                   f"- 当前价: {current_price:.4f}\n"
                   f"- 建议：加一档网格仓（例如加 1% 总资金），不动底仓。")
            messages.append(msg)

    state[name]["last_price"] = current_price
    return messages


# ========= 主循环 =========

def main_loop():
    state = load_state()
    print("ETF 网格监控（调试版）启动...")

    while True:
        all_messages = []

        for name, cfg in ETF_CONFIG.items():
            try:
                msgs = check_signals_for_etf(name, cfg, state)
                all_messages.extend(msgs)
            except Exception as e:
                print(f"{name} 检查信号时出错: {e}")

        if all_messages:
            full_msg = "\n\n".join(all_messages)
            send_notification(full_msg)

        save_state(state)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main_loop()
