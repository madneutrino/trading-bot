import sys
import math
import logging
import datetime


def setup_logger(name):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    streamHandler = logging.StreamHandler(sys.stdout)
    streamHandler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    fileHandler = logging.FileHandler(
        f"logs/{name}-binance-{datetime.datetime.utcnow().strftime('%s')}.log"
    )
    fileHandler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )

    logger.addHandler(streamHandler)
    logger.addHandler(fileHandler)

    return logger


def round_down_to_precision(number, precision):
    factor = 10**precision
    return math.floor(number * factor) / factor


def step_size_to_precision(step_size: str) -> int:
    x = float(step_size)
    return -int(math.log10(x))


def format_quantity(qty: float, exchange_info):
    qty_precision = step_size_to_precision(
        [
            i["stepSize"]
            for i in exchange_info["filters"]
            if i["filterType"] == "LOT_SIZE"
        ][0]
    )
    return round_down_to_precision(qty, qty_precision)


def format_price(price: float, exchange_info):
    price_precision = step_size_to_precision(
        [i for i in exchange_info["filters"] if i["filterType"] == "PRICE_FILTER"][0][
            "tickSize"
        ]
    )
    return round_down_to_precision(price, price_precision)