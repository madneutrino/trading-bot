import os
import time
from typing import List
from binance.spot import Spot as Client
from dotenv import load_dotenv
from models import TradingCall
import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import traceback
import logging
import sys
import math

# SETUP ENV
dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path)
BINANCE_API_KEY = os.getenv("API_KEY")
BINANCE_API_SECRET = os.getenv("API_SECRET")
BINANCE_API_URL = os.getenv("API_URL")

# SETUP DB
engine = create_engine("sqlite:///tradingbot.db")
session = sessionmaker(bind=engine)()

# CONSTANTS
ORDER_SIZE = 100  # USD per trade
ORDER_EXPIRY_TIME_HOURS = 24 * 2  # 2 days

# SETUP LOGGING to log to file with timestamp and console and auto-rotate
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(
            "logs/binance-" + datetime.datetime.utcnow().strftime("%s") + ".log"
        ),
        logging.StreamHandler(sys.stdout),
    ],
    level=logging.INFO,
)


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


def fetch_unseen_trades(latest_first: bool = True, limit=10, lookback_hours=12):
    return (
        session.query(TradingCall)
        .filter(TradingCall.open_order.is_(None))
        .filter(
            TradingCall.timestamp
            >= datetime.datetime.now() - datetime.timedelta(hours=lookback_hours)
        )
        .filter(TradingCall.bragged == 0)
        .order_by(TradingCall.id.desc() if latest_first else TradingCall.id.asc())
        .limit(limit)
        .all()
    )


def get_pending_opening_limit_orders():
    return (
        session.query(TradingCall)
        .filter(TradingCall.open_order.is_not(None))
        .filter(TradingCall.close_order.is_(None))
        .filter(TradingCall.completed == 0)
        .all()
    )


def get_pending_closing_limit_orders():
    return (
        session.query(TradingCall)
        .filter(TradingCall.close_order.is_not(None))
        .filter(TradingCall.completed == 0)
        .all()
    )


class BinanceAPI:
    def __init__(self):
        self.client = Client(
            BINANCE_API_KEY, BINANCE_API_SECRET, base_url=BINANCE_API_URL
        )

    def filter_viable_trades(self, trades: List[TradingCall]):
        for trade in trades:
            max_price = trade.targets[2]
            min_price = trade.stop_loss

            # # ONLY FOR TEST NET. It has a limited asset list ###
            # if trade.symbol != "LTCUSDT":
            #     continue

            current_price = float(self.client.avg_price(trade.symbol)["price"])

            if current_price < min_price or current_price > max_price:
                logging.debug("skipping {}".format(trade.symbol))
                # for testing.
                # trade.entry = [
                #     round(current_price * 0.99, 5),
                #     round(current_price * 0.98, 5),
                # ]
                # trade.targets[5] = round(current_price * 1.02, 5)
                # yield trade
                continue
            else:
                yield trade

    def send_open_order(self, trade: TradingCall):
        if trade.open_order is not None or trade.side != "BUY":
            # We dont support SHORT orders yet.
            return trade

        try:
            info = self.client.exchange_info(trade.symbol)["symbols"][0]
            # TODO sanity check on the asset pair
            quantity = format_quantity(ORDER_SIZE / trade.entry[0], info)

            params = {
                "symbol": trade.symbol,
                "side": trade.side,
                "type": "LIMIT",
                "quantity": quantity,
                "price": format_price(max(iter(trade.entry)), info),
                # TODO this might help avoid calling get_order again. need to confirm
                "newOrderRespType": "FULL",
                "timeInForce": "GTC",
            }

            response = self.client.new_order(**params)
            confirmed_order = self.client.get_order(
                trade.symbol, orderId=response["orderId"]
            )
            trade.open_order = confirmed_order

            session.add(trade)
            session.commit()
            logging.info(f"New opening limit order => {trade.id} : {trade.open_order}")
        except Exception as e:
            logging.error(
                f"Could not create new opening limit order => {trade.id}/{trade.symbol} : {e}"
            )

        return trade

    def send_open_orders(self, trades):
        return [self.send_open_order(trade) for trade in trades]

    def update_opening_order_status(self, trade: TradingCall):
        order = self.client.get_order(trade.symbol, orderId=trade.open_order["orderId"])
        if order["status"] != trade.open_order.get("status", None):
            trade.open_order = order
            session.add(trade)
            session.commit()
            logging.info(f"Filled limit order => {trade.id} : {order}")
        return trade

    def update_opening_order_statuses(self, pendingOrders: list[TradingCall]):
        return [self.update_opening_order_status(trade) for trade in pendingOrders]

    def update_closing_order_status(self, trade: TradingCall):
        order = self.client.get_order(
            trade.symbol, orderId=trade.close_order["orderId"]
        )
        if order["status"] != trade.close_order.get("status", None):
            trade.close_order = order
            # it is necessary to fully close the position for it to be completed
            # so, cancelled / expired etc. are not considered completed
            # Additionally, An already completed trade cannnot be uncompleted
            trade.completed = 1 if trade.completed or order["status"] == "FILLED" else 0
            session.add(trade)
            session.commit()
            logging.info(f"Filled closing limit order => {trade.id} : {order}")
        return trade

    def update_closing_order_statuses(self, pendingOrders: list[TradingCall]):
        return [self.update_closing_order_status(trade) for trade in pendingOrders]

    def filter_filled_opening_orders(
        self,
        trades: list[TradingCall],
    ):
        return [
            trade
            for trade in trades
            if trade.open_order.get("status", None) == "FILLED"
        ]

    def filter_expired_open_orders(
        self, trades: list[TradingCall], max_expiry_hours: int
    ):
        return [
            trade
            for trade in trades
            if trade.open_order.get("status", None) == "NEW"
            and (
                datetime.datetime.now()
                - datetime.datetime.fromtimestamp(
                    (
                        trade.open_order.get("time", None)
                        or trade.open_order.get("transactTime", None)
                    )
                    // 1000
                )
            )
            > datetime.timedelta(hours=max_expiry_hours)
        ]

    def filter_expired_close_orders(
        self, trades: list[TradingCall], max_expiry_hours: int
    ):
        try:
            return [
                trade
                for trade in trades
                if trade.close_order.get("status", None) == "NEW"
                and (
                    datetime.datetime.now()
                    - datetime.datetime.fromtimestamp(
                        (
                            trade.close_order.get("time", None)
                            or trade.close_order.get("transactTime", None)
                        )
                        // 1000
                    )
                )
                > datetime.timedelta(hours=max_expiry_hours)
            ]
        except Exception as e:
            logging.error(f"Could not filter close orders => {trades} : {e}")
            return []

    def filter_need_to_stop_loss(self, trades):
        return [
            trade
            for trade in trades
            if float(self.client.avg_price(trade.symbol)["price"]) < trade.stop_loss
        ]

    def send_close_order(self, trade: TradingCall):
        params = {
            "symbol": trade.symbol,
            "side": "SELL" if trade.side == "BUY" else "BUY",
            "newOrderRespType": "FULL",
        }
        try:
            info = self.client.exchange_info(trade.symbol)["symbols"][0]

            fills = self.client.my_trades(
                trade.symbol, orderId=trade.open_order["orderId"]
            )
            qty = float(trade.open_order["executedQty"]) - sum(
                float(fill["commission"]) for fill in fills
            )
            params["quantity"] = format_quantity(qty, info)

            current_price = float(self.client.avg_price(trade.symbol)["price"])
            target = format_price(trade.targets[3], info)
            if current_price > target:
                params["type"] = "MARKET"
            else:
                params["type"] = "LIMIT"
                params["timeInForce"] = "GTC"
                params["price"] = target

            response = self.client.new_order(**params)

            trade.close_order = response
            session.add(trade)
            session.commit()
            logging.info(f"New close order => {trade.id} : {response}")

        except Exception as e:
            logging.error(
                f"Could not create new close order => {trade.id}/{trade.symbol} : {params} : {e} {traceback.format_exc()}"
            )

        return trade

    def send_close_orders(self, filledOrders: list[TradingCall]):
        return [self.send_close_order(trade) for trade in filledOrders]

    def send_cancel_open_orders(self, trades: list[TradingCall]):
        for trade in trades:
            try:
                self.client.cancel_order(
                    trade.symbol, orderId=trade.open_order["orderId"]
                )
                trade.completed = 1
                trade.reason = "Open Order took too long to fill"
                session.commit()
                logging.info(f"Cancelled open order => {trade.id}/{trade.symbol}")
            except:
                logging.info(
                    f"Could not cancel open order => {trade.id}/{trade.symbol}"
                )

    def send_cancel_close_orders(self, trades: list[TradingCall], reason: str):
        for trade in trades:
            try:
                self.client.cancel_order(
                    trade.symbol, orderId=trade.close_order["orderId"]
                )
            except:
                logging.info(
                    f"Could not cancel close order => {trade.id}/{trade.symbol}"
                )

            try:
                # TODO shouldnt we store this somewhere?
                # it might make sense to just overwrite close_order with this.
                self.client.new_order(
                    **{
                        "symbol": trade.symbol,
                        "side": "SELL" if trade.side == "BUY" else "BUY",
                        "type": "MARKET",
                        "quantity": float(trade.close_order["origQty"]),
                        "newOrderRespType": "FULL",
                    }
                )
                trade.completed = 1
                trade.reason = reason
                session.commit()
                logging.info(f"Cancelled close order => {trade.id}/{trade.symbol}")
            except:
                logging.error(f"Could not market order => {trade.id}/{trade.symbol}")


def step(binance_api: BinanceAPI):
    logging.debug("--- NEW STEP ---")

    pendingOpeningLimitOrders = get_pending_opening_limit_orders()
    logging.debug(f"Pending opening limit orders => {pendingOpeningLimitOrders}")
    filledOpeningLimitOrders = binance_api.filter_filled_opening_orders(
        binance_api.update_opening_order_statuses(pendingOpeningLimitOrders)
    )

    pendingClosingLimitOrders = get_pending_closing_limit_orders()
    logging.debug(f"Pending closing limit orders => {pendingClosingLimitOrders}")
    binance_api.update_closing_order_statuses(pendingClosingLimitOrders)

    binance_api.send_close_orders(filledOpeningLimitOrders)

    # Cull limit orders taking too long to fill
    binance_api.send_cancel_open_orders(
        binance_api.filter_expired_open_orders(
            get_pending_opening_limit_orders(), ORDER_EXPIRY_TIME_HOURS
        )
    )
    binance_api.send_cancel_close_orders(
        binance_api.filter_expired_close_orders(
            get_pending_closing_limit_orders(), ORDER_EXPIRY_TIME_HOURS
        ),
        "Close order took too long to fill",
    )

    # STOP LOSS
    binance_api.send_cancel_close_orders(
        binance_api.filter_need_to_stop_loss(get_pending_closing_limit_orders()),
        "stop loss",
    )

    # Get account and balance information
    account_balance = float(
        [
            b["free"]
            for b in binance_api.client.account()["balances"]
            if b["asset"] == "USDT"
        ][0]
    )

    if account_balance > ORDER_SIZE:
        unseen_trades = fetch_unseen_trades(
            latest_first=True, limit=int(account_balance // ORDER_SIZE)
        )
        logging.debug(f"Unseen trades => {unseen_trades}")
        viable_trades = binance_api.filter_viable_trades(unseen_trades)
        pendingOpeningLimitOrders = binance_api.send_open_orders(viable_trades)
    else:
        logging.debug("!!! Insufficient USDT balance !!!")

    # TODO: Token accounting


def main():
    binance_api = BinanceAPI()

    while True:
        try:
            step(binance_api)
        except Exception as e:
            # logging.info detailed trace of the error
            logging.error("!!! step failed :/ !!!")
            logging.error(traceback.format_exc())

        time.sleep(30)


main()
