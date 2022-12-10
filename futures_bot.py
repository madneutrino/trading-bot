from binance.um_futures import UMFutures as Client
import os
import time
from typing import List
from dotenv import load_dotenv
from models import TradingCall
import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import traceback
from utils import *

# SETUP ENV
dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path)
BINANCE_API_KEY = os.getenv("FUTURES_API_KEY")
BINANCE_API_SECRET = os.getenv("FUTURES_API_SECRET")
BINANCE_API_URL = os.getenv("FUTURES_API_URL")

# SETUP DB
engine = create_engine("sqlite:///tradingbot.db")
session = sessionmaker(bind=engine)()

# CONSTANTS
ORDER_SIZE = 100  # USD per trade
ORDER_EXPIRY_TIME_HOURS = 24  # 1 day
DELAY_BETWEEN_STEPS = 10  # seconds
TARGET_NUM = 3
LEVERAGE = 10

logger = setup_logger("futoor")


def fetch_unseen_calls(latest_first: bool = True, limit=10, lookback_hours=12):
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


def get_unfilled_opening_orders():
    trades = (
        session.query(TradingCall)
        .filter(TradingCall.open_order.is_not(None))
        .filter(TradingCall.closed == 0)
        .all()
    )

    return [t for t in trades if t.open_order["status"] != "FILLED"]


def get_unfilled_tpsl_orders():
    trades = (
        session.query(TradingCall)
        .filter(
            TradingCall.take_profit_order.is_not(None)
            or TradingCall.stop_loss_order.is_not(None)
        )
        .filter(TradingCall.closed == 0)
        .all()
    )

    return [
        t
        for t in trades
        if t.open_order["status"] == "FILLED"
        and (
            t.take_profit_order["status"] != "FILLED"
            or t.stop_loss_order["status"] != "FILLED"
        )
    ]


class BinanceAPI:
    def __init__(self):
        self.client = Client(
            BINANCE_API_KEY, BINANCE_API_SECRET, base_url=BINANCE_API_URL
        )

    def filter_viable_trades(self, trades: List[TradingCall]):
        for trade in trades:
            # # ONLY FOR TEST NET. It has a limited asset list ###
            # if trade.symbol != "LTCUSDT":
            #     continue

            current_price = float(self.client.mark_price(trade.symbol)["markPrice"])
            if not (trade.entry[0] <= current_price <= trade.entry[1]):
                logger.debug(
                    f"Skipping because not in entry range => {trade.id}/{trade.symbol}"
                )
                continue
            else:
                yield trade

    def send_open_position(self, trade: TradingCall):
        """
        Have to long/short + TP + SL separately because atomic endpoint is private
        See: https://dev.binance.vision/t/how-to-implement-otoco-tp-sl-orders-using-api/1622/18
        """
        if trade.open_order is not None:
            return trade

        try:
            self.client.change_margin_type(trade.symbol, "ISOLATED")
            self.client.change_leverage(trade.symbol, LEVERAGE)
        except Exception as e:
            logger.error(
                f"Could not change margin type/leverage => {trade.id}/{trade.symbol} : {e}"
            )

        info = self.client.exchange_info(trade.symbol)["symbols"][0]
        # TODO sanity check on the asset pair
        quantity = format_quantity(ORDER_SIZE / trade.entry[0], info)

        # open the long/short position
        try:

            params = {
                "symbol": trade.symbol,
                "side": trade.side,
                "type": "LIMIT",
                "quantity": quantity,
                "reduceOnly": "false",
                "price": format_price(max(iter(trade.entry)), info),
                "newOrderRespType": "FULL",
                "timeInForce": "GTC",
            }

            response = self.client.new_order(**params)
            confirmed_order = self.client.query_order(
                trade.symbol, orderId=response["orderId"]
            )
            trade.open_order = confirmed_order

            session.add(trade)
            session.commit()
            logger.info(f"New opening order => {trade.id} : {trade.open_order}")
        except Exception as e:
            logger.error(
                f"Could not create new opening order => {trade.id}/{trade.symbol} : {e}"
            )

        # create the stop loss order
        try:
            params = {
                "symbol": trade.symbol,
                "side": "SELL" if trade.side == "BUY" else "BUY",
                "type": "STOP",
                "quantity": quantity,
                "reduceOnly": "true",
                "stopPrice": format_price(trade.stop_loss, info),
                "price": format_price(trade.stop_loss, info),
                "newOrderRespType": "FULL",
                "timeInForce": "GTE_GTC",
                "workingType": "MARK_PRICE",
            }

            response = self.client.new_order(**params)
            confirmed_order = self.client.query_order(
                trade.symbol, orderId=response["orderId"]
            )
            trade.stop_loss_order = confirmed_order

            session.add(trade)
            session.commit()
            logger.info(f"New stop loss order => {trade.id} : {trade.stop_loss_order}")
        except Exception as e:
            logger.error(
                f"Could not create new stop loss order => {trade.id}/{trade.symbol} : {e}"
            )

        # create the take profit order
        try:
            params = {
                "symbol": trade.symbol,
                "side": "SELL" if trade.side == "BUY" else "BUY",
                "type": "TAKE_PROFIT",
                "quantity": quantity,
                "reduceOnly": "true",
                "stopPrice": format_price(trade.targets[TARGET_NUM], info),
                "price": format_price(trade.targets[TARGET_NUM], info),
                "newOrderRespType": "FULL",
                "timeInForce": "GTE_GTC",
                "workingType": "MARK_PRICE",
            }

            response = self.client.new_order(**params)
            confirmed_order = self.client.query_order(
                trade.symbol, orderId=response["orderId"]
            )
            trade.take_profit_order = confirmed_order

            session.add(trade)
            session.commit()
            logger.info(
                f"New take profit order => {trade.id} : {trade.take_profit_order}"
            )
        except Exception as e:
            logger.error(
                f"Could not create new take profit order => {trade.id}/{trade.symbol} : {e}"
            )

        return trade

    def send_open_positions(self, trades):
        return [self.send_open_position(trade) for trade in trades]

    def update_opening_order_status(self, trade: TradingCall):
        order = self.client.query_order(
            trade.symbol, orderId=trade.open_order["orderId"]
        )
        if order["status"] != trade.open_order.get("status", None):
            trade.open_order = order
            session.add(trade)
            session.commit()
            logger.info(f"Filled opening order => {trade.id} : {order}")
        return trade

    def update_opening_order_statuses(self, pendingOrders: list[TradingCall]):
        return [self.update_opening_order_status(trade) for trade in pendingOrders]

    def update_tp_sl_order_status(self, trade: TradingCall):
        tp_order = self.client.query_order(
            trade.symbol, orderId=trade.take_profit_order["orderId"]
        )
        if tp_order["status"] != trade.take_profit_order.get("status", None):
            trade.take_profit_order = tp_order
            # it is necessary to fully close the trade for it to be closed
            # so, cancelled / expired etc. are not considered closed
            # Additionally, An already closed trade cannnot be uncompleted
            trade.closed = 1 if trade.closed or tp_order["status"] == "FILLED" else 0
            session.add(trade)
            session.commit()
            logger.info(f"Filled take profit order => {trade.id} : {tp_order}")

        sl_order = self.client.query_order(
            trade.symbol, orderId=trade.stop_loss_order["orderId"]
        )
        if sl_order["status"] != trade.stop_loss_order.get("status", None):
            trade.stop_loss_order = sl_order
            # it is necessary to fully close the trade for it to be closed
            # so, cancelled / expired etc. are not considered closed
            # Additionally, An already closed trade cannnot be uncompleted
            trade.closed = 1 if trade.closed or sl_order["status"] == "FILLED" else 0
            session.add(trade)
            session.commit()
            logger.info(f"Filled stop loss order => {trade.id} : {sl_order}")

        return trade

    def update_tp_sl_order_statuses(self, pendingOrders: list[TradingCall]):
        return [self.update_tp_sl_order_status(trade) for trade in pendingOrders]

    def filter_expired_open_orders(
        self, trades: list[TradingCall], max_expiry_hours: int
    ):
        return [
            trade
            for trade in trades
            if trade.open_order.get("status", None)
            == "NEW"  # TODO: What about partially filled ones?
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

    def filter_expired_tpsl_orders(
        self, trades: list[TradingCall], max_expiry_hours: int
    ):
        try:
            return [
                trade
                for trade in trades
                if trade.take_profit_order.get("status", None) == "NEW"
                and trade.stop_loss_order.get("status", None) == "NEW"
                and (
                    datetime.datetime.now()
                    - datetime.datetime.fromtimestamp(
                        (
                            trade.take_profit_order.get("time", None)
                            or trade.take_profit_order.get("transactTime", None)
                        )
                        // 1000
                    )
                )
                > datetime.timedelta(hours=max_expiry_hours)
            ]
        except Exception as e:
            logger.error(f"Could not filter TP/SL orders => {trades} : {e}")
            return []

    def send_cancel_open_orders(self, trades: list[TradingCall]):
        """
        Note: This cancels TP/SL orders automatically
        """
        for trade in trades:
            try:
                trade.open_order = self.client.cancel_order(
                    trade.symbol, orderId=trade.open_order["orderId"]
                )
                trade.closed = 1
                session.commit()
                logger.info(f"Cancelled open order => {trade.id}/{trade.symbol}")
            except:
                logger.info(f"Could not cancel open order => {trade.id}/{trade.symbol}")

    def send_cancel_tpsl_orders_and_close_position(self, trades: list[TradingCall]):
        """
        If you're cancelling a position that has filled open but did not trigger sl/tp, you have to cancel both tp and sl manually and close the position
        Note: If either of the tp/sl orders are filled, you don't have to do anything!
        """
        for trade in trades:
            try:
                trade.take_profit_order = self.client.cancel_order(
                    trade.symbol, orderId=trade.take_profit_order["orderId"]
                )
                logger.info(f"cancelled take_profit order => {trade.id}/{trade.symbol}")
                session.commit()
            except:
                logger.error(
                    f"Could not cancel take_profit order => {trade.id}/{trade.symbol}"
                )

            try:
                trade.stop_loss_order = self.client.cancel_order(
                    trade.symbol, orderId=trade.stop_loss_order["orderId"]
                )
                logger.info(f"cancelled stop_loss order => {trade.id}/{trade.symbol}")
                session.commit()
            except:
                logger.error(
                    f"Could not cancel stop_loss order => {trade.id}/{trade.symbol}"
                )

            try:
                self.client.new_order(
                    **{
                        "symbol": trade.symbol,
                        "side": "SELL" if trade.side == "BUY" else "BUY",
                        "type": "MARKET",
                        "quantity": float(trade.open_order["origQty"]),
                        "newOrderRespType": "FULL",
                    }
                )
                trade.closed = 1
                session.commit()
                logger.info(f"closed position => {trade.id}/{trade.symbol}")
            except:
                logger.error(f"Could not close position => {trade.id}/{trade.symbol}")


def step(binance_api: BinanceAPI):
    logger.debug("--- NEW STEP ---")

    pendingOpeningOrders = get_unfilled_opening_orders()
    logger.debug(f"Pending opening orders => {pendingOpeningOrders}")
    binance_api.update_opening_order_statuses(pendingOpeningOrders)

    pendingTpSlOrders = get_unfilled_tpsl_orders()
    logger.debug(f"Pending tp/sl orders => {pendingTpSlOrders}")
    binance_api.update_tp_sl_order_statuses(pendingTpSlOrders)

    # Cull orders taking too long to fill
    binance_api.send_cancel_open_orders(
        binance_api.filter_expired_open_orders(
            get_unfilled_opening_orders(), ORDER_EXPIRY_TIME_HOURS
        )
    )
    binance_api.send_cancel_tpsl_orders_and_close_position(
        binance_api.filter_expired_tpsl_orders(
            get_unfilled_tpsl_orders(), ORDER_EXPIRY_TIME_HOURS
        )
    )

    # Get account and balance information
    account_balance = float(
        [
            b["availableBalance"]
            for b in binance_api.client.account()["assets"]
            if b["asset"] == "USDT"
        ][0]
    )

    if account_balance > ORDER_SIZE:
        unseen_trades = fetch_unseen_calls(
            latest_first=True, limit=int(account_balance // ORDER_SIZE)
        )
        logger.debug(f"Unseen trades => {unseen_trades}")
        viable_trades = binance_api.filter_viable_trades(unseen_trades)
        pendingOpeningOrders = binance_api.send_open_positions(viable_trades)
    else:
        logger.debug("!!! Insufficient USDT balance !!!")

    # TODO: Token accounting


def main():
    binance_api = BinanceAPI()

    while True:
        try:
            step(binance_api)
        except Exception:
            # logger.info detailed trace of the error
            logger.error("!!! step failed :/ !!!")
            logger.error(traceback.format_exc())

        time.sleep(DELAY_BETWEEN_STEPS)


main()
