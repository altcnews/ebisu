# coding: UTF-8

import json
import os
import traceback
from datetime import datetime, timezone

import bitmex
import pandas as pd
from bravado.exception import HTTPNotFound
from pytz import UTC

from src import logger, retry, allowed_range, to_data_frame, \
    resample, delta, FatalError, notify
from src.bitmex_websocket import BitMexWs


# 本番取引用クラス
class BitMex:
    # 利用する時間足
    bin_size = '1h'
    # プライベートAPI用クライアント
    private_client = None
    # パブリックAPI用クライアント
    public_client = None
    # 稼働中
    is_running = True
    # 時間足を取得するクローラ
    crawler = None
    # 戦略を実施するリスナー
    listener = None
    # ログの出力
    enable_trade_log = True
    # OHLCの長さ
    ohlcv_len = 100
    # OHLCのキャッシュ
    data = None

    def __init__(self, demo=False, threading=True):
        """
        コンストラクタ
        :param demo:
        :param run:
        """
        self.demo = demo
        self.is_running = threading

    def __init_client(self):
        """
        初期化関数
        """
        if self.private_client is not None and self.public_client is not None:
            return
        api_key = os.environ.get("BITMEX_TEST_APIKEY") if self.demo else os.environ.get("BITMEX_APIKEY")
        api_secret = os.environ.get("BITMEX_TEST_SECRET") if self.demo else os.environ.get("BITMEX_SECRET")
        self.private_client = bitmex.bitmex(test=self.demo, api_key=api_key, api_secret=api_secret)
        self.public_client = bitmex.bitmex(test=self.demo)

    def now_time(self):
        return datetime.now().astimezone(UTC)

    def __validate_order_quantity(self, order_qty, price=0):
        self.__init_client()
        margin = retry(lambda: self.private_client.User.User_getMargin(currency="XBt").result()[0])
        position = retry(lambda: self.private_client.Position.Position_get(filter=json.dumps({"symbol": "XBTUSD"}))
                         .result()[0][0])
        instrument = retry(lambda: self.public_client.Instrument.Instrument_get(symbol="XBTUSD").result()[0][0])
        multiplier = instrument["multiplier"]
        init_margin_req = position["initMarginReq"]
        if price == 0:
            price = instrument["lastPrice"]
        excess_margin = margin["excessMargin"]
        if multiplier > 0:
            if abs(order_qty * multiplier * price) * init_margin_req < excess_margin:
                return
            else:
                raise FatalError()
        else:
            if abs(order_qty * multiplier / price) * init_margin_req < excess_margin:
                return
            else:
                raise FatalError()

    def get_retain_rate(self):
        """
        証拠金維持率。
        :return:
        """
        return 0.2

    def get_lot(self):
        """
        ロットの計算を行う。
        :return:
        """
        return int((1 - self.get_retain_rate()) * self.get_balance() / 100000000 * self.get_leverage() * self.get_market_price())

    def get_balance(self):
        """
        残高の取得を行う。
        :return:
        """
        self.__init_client()
        return retry(lambda: self.private_client.User.User_getWallet(currency="XBt").result()[0]["amount"])

    def get_leverage(self):
        """
        レバレッジの取得する。
        :return:
        """
        self.__init_client()
        return retry(lambda: self.private_client.Position.Position_get(filter=json.dumps({"symbol": "XBTUSD"})).result()[0][0]["leverage"])

    def get_position_size(self):
        """
        現在のポジションサイズを取得する。
        :return:
        """
        self.__init_client()
        return retry(lambda: self.private_client.Position.Position_get(filter=json.dumps({"symbol": "XBTUSD"})).result()[0][0]["currentQty"])

    def get_position_avg_price(self):
        """
        現在のポジションの平均価格を取得する。
        :return:
        """
        self.__init_client()
        return retry(lambda: self.private_client.Position.Position_get(filter=json.dumps({"symbol": "XBTUSD"})).result()[0][0][
            "avgEntryPrice"])

    def get_market_price(self):
        """
        現在の取引額を取得する。
        :return:
        """
        self.__init_client()
        return retry(lambda: self.public_client.Instrument.Instrument_get(symbol="XBTUSD").result()[0][0]["lastPrice"])

    def get_commission(self):
        """
        手数料を取得する。
        :return:
        """
        return 0.075 / 100

    def cancel_all(self):
        """
        すべての注文をキャンセルする。
        """
        self.__init_client()
        orders = retry(lambda: self.private_client.Order.Order_cancelAll().result()[0])
        for order in orders:
            logger.info(f"Cancel Order : (orderID, orderType, side, orderQty, limit, stop) = "
                        f"({order['orderID']}, {order['ordType']}, {order['side']}, {order['orderQty']}, "
                        f"{order['price']}, {order['stopPx']})")
        logger.info(f"Cancel All Order")

    def close_all(self):
        """
        すべてのポジションを解消する。
        """
        self.__init_client()
        order = retry(lambda: self.private_client.Order.Order_closePosition(symbol="XBTUSD").result()[0])
        logger.info(f"Close Position : (orderID, orderType, side, orderQty, limit, stop) = "
                    f"({order['orderID']}, {order['ordType']}, {order['side']}, {order['orderQty']}, "
                    f"{order['price']}, {order['stopPx']})")
        logger.info(f"Close All Position")

    def entry(self, id, long, qty, limit=0, stop=0, when=True):
        """
        注文をする。pineの関数と同等の機能。
        https://jp.tradingview.com/study-script-reference/#fun_strategy{dot}entry
        :param id: 注文の番号
        :param long: ロング or ショート
        :param qty: 注文量
        :param limit: 指値
        :param stop: ストップ指値
        :param when: 注文するか
        :return:
        """
        self.__init_client()

        if not when:
            return

        pos_size = self.get_position_size()

        if long and pos_size > 0:
            return

        if not long and pos_size < 0:
            return

        side = "Buy" if long else "Sell"
        ord_qty = qty + abs(pos_size)

        if self.exist_open_order(long, ord_qty, limit, stop):
            return

        self.cancel(long)

        if limit > 0 and stop > 0:
            ord_type = "StopLimit"
            self.__validate_order_quantity(ord_qty, limit)
            retry(lambda: self.private_client.Order.Order_new(symbol="XBTUSD", ordType=ord_type,
                                                              side=side, orderQty=ord_qty, price=limit, stopPx=stop).result())
        elif limit > 0:
            ord_type = "Limit"
            self.__validate_order_quantity(ord_qty, limit)
            retry(lambda: self.private_client.Order.Order_new(symbol="XBTUSD", ordType=ord_type,
                                                              side=side, orderQty=ord_qty, price=limit).result())
        elif stop > 0:
            ord_type = "Stop"
            self.__validate_order_quantity(ord_qty, stop)
            retry(lambda: self.private_client.Order.Order_new(symbol="XBTUSD", ordType=ord_type,
                                                              side=side, orderQty=ord_qty, stopPx=stop).result())
        else:
            ord_type = "Market"
            self.__validate_order_quantity(ord_qty)
            retry(lambda: self.private_client.Order.Order_new(symbol="XBTUSD", ordType=ord_type,
                                                              side=side, orderQty=ord_qty).result())

        if self.enable_trade_log:
            logger.info(f"========= Create Order ==============")
            logger.info(f"ID     : {id}")
            logger.info(f"Type   : {ord_type}")
            logger.info(f"Side   : {side}")
            logger.info(f"Qty    : {ord_qty}")
            logger.info(f"Limit  : {limit}")
            logger.info(f"Stop   : {stop}")
            logger.info(f"======================================")

    def get_open_orders(self, long):
        """
        注文を取得する。
        :param long: ロング or ショート
        :return:
        """
        self.__init_client()
        side = "Buy" if long else "Sell"
        return retry(lambda: self.private_client
                     .Order.Order_getOrders(filter=json.dumps({"symbol": "XBTUSD", "open": True, "side": side}))
                     .result()[0])

    def exist_open_order(self, long, qty, limit=0, stop=0):
        """
        同じ注文が存在するか確認する。
        :param long:  ロング or ショート
        :param qty: 注文量
        :param limit: 指値
        :param stop: ストップ指値
        :return:
        """
        orders = self.get_open_orders(long)
        if limit > 0 and stop > 0:
            return len([order for order in orders
                        if order['orderQty'] == qty and order['price'] == limit and order['stopPx'] == stop]) > 0
        elif limit > 0:
            return len([order for order in orders
                        if order['orderQty'] == qty and order['price'] == limit and order['stopPx'] is None]) > 0
        elif stop > 0:
            return len([order for order in orders
                        if order['orderQty'] == qty and order['price'] is None and order['stopPx'] == stop]) > 0
        return False

    def cancel(self, long):
        """
        注文をキャンセルする。
        :param long: ロング or ショート
        :return:
        """
        self.__init_client()
        orders = self.get_open_orders(long)
        if len(orders) == 0:
            return

        for order in orders:
            try:
                retry(lambda: self.private_client.Order.Order_cancel(orderID=order['orderID']).result()[0][0])
            except HTTPNotFound:
                return
            logger.info(f"Cancel Order : (orderID, orderType, side, orderQty, limit, stop) = "
                        f"({order['orderID']}, {order['ordType']}, {order['side']}, {order['orderQty']}, "
                        f"{order['price']}, {order['stopPx']})")

    def get_order_size(self):
        """
        注文の数を取得する。
        :return:
        """
        self.__init_client()
        return len(retry(lambda: self.private_client
                         .Order.Order_getOrders(filter=json.dumps({"symbol": "XBTUSD", "open": True})).result()[0]))

    def fetch_ohlcv(self, bin_size, start_time, end_time):
        """
        足データを取得する
        :param start_time: 開始時間
        :param end_time: 終了時間
        :return:
        """
        self.__init_client()
        fetch_bin_size = allowed_range[bin_size][0]
        data = retry(lambda: self.public_client.Trade.Trade_getBucketed(symbol="XBTUSD", binSize=fetch_bin_size,
                                                                        startTime=start_time, endTime=end_time,
                                                                        count=500, partial=False).result()[0])
        data_frame = to_data_frame(data)
        return resample(data_frame, bin_size)

    def __update(self, new_data):
        """
        データを取得して、戦略を実行する。
        """

        if self.data is None:
            end_time = datetime.now(timezone.utc)
            start_time = end_time - self.ohlcv_len * delta(self.bin_size)
            d1 = self.fetch_ohlcv(self.bin_size, start_time, end_time)
            if len(d1) > 0:
                d2 = self.fetch_ohlcv(allowed_range[self.bin_size][0],
                                      d1.iloc[-1].name + delta(allowed_range[self.bin_size][0]), end_time)
                self.data = pd.concat([d1, d2])
            else:
                self.data = d1
            resample_data = self.data
        else:
            self.data = pd.concat([self.data, new_data])
            resample_data = resample(self.data, self.bin_size)

        if self.data.iloc[-1].name == resample_data.iloc[-1].name:
            self.data = resample_data.iloc[-1*self.ohlcv_len:,:]

        open = resample_data['open'].values
        close = resample_data['close'].values
        high = resample_data['high'].values
        low = resample_data['low'].values

        try:
            if self.listener is not None:
                self.listener(open, close, high, low)
        except FatalError as e:
            # 致命的エラー
            logger.error(f"Fatal error. {e}")
            logger.error(traceback.format_exc())
            notify(f"Fatal error occurred. Stopping Bot. {e}")
            self.stop()
        except Exception as e:
            logger.error(f"An error occurred. {e}")
            logger.error(traceback.format_exc())
            notify(f"An error occurred. {e}")

    def on_update(self, bin_size, listener):
        """
        戦略の関数を登録する。
        :param listener:
        """
        self.bin_size = bin_size
        self.listener = listener
        if self.is_running:
            self.ws = BitMexWs()
            self.ws.on_update(allowed_range[bin_size][0], self.__update)

    def stop(self):
        """
        クローラーを止める。
        """
        self.is_running = False
        self.ws.close()

    def show_result(self):
        """
        取引結果を表示する。
        """
        pass

    def plot(self, name, value, color, overlay=True):
        """
        グラフに描画する。
        """
        pass
