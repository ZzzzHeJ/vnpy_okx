"""
1. 只支持单币种保证金模式
2. 只支持全仓模式
3. 只支持单向持仓模式
"""


import base64
import hashlib
import hmac
import json
import sys
import time
from copy import copy
from datetime import datetime
from urllib.parse import urlencode
from typing import Any, Dict, List, Set
from types import TracebackType

from requests import Response

from vnpy.event.engine import EventEngine
from vnpy.trader.constant import (
    Direction,
    Exchange,
    Interval,
    Offset,
    OrderType,
    Product,
    Status
)
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.utility import round_to, ZoneInfo
from vnpy.trader.object import (
    AccountData,
    BarData,
    CancelRequest,
    ContractData,
    HistoryRequest,
    OrderData,
    OrderRequest,
    PositionData,
    SubscribeRequest,
    TickData,
    TradeData
)
from vnpy_rest import Request, RestClient
from vnpy_websocket import WebsocketClient


# 中国时区
CHINA_TZ: ZoneInfo = ZoneInfo("Asia/Shanghai")

# 实盘和模拟盘REST API地址
REST_HOST: str = "https://www.okx.com"

# 实盘Websocket API地址
PUBLIC_WEBSOCKET_HOST: str = "wss://ws.okx.com:8443/ws/v5/public"
PRIVATE_WEBSOCKET_HOST: str = "wss://ws.okx.com:8443/ws/v5/private"

# 模拟盘Websocket API地址
TEST_PUBLIC_WEBSOCKET_HOST: str = "wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999"
TEST_PRIVATE_WEBSOCKET_HOST: str = "wss://wspap.okx.com:8443/ws/v5/private?brokerId=9999"

# 委托状态映射
STATUS_OKX2VT: Dict[str, Status] = {
    "live": Status.NOTTRADED,
    "partially_filled": Status.PARTTRADED,
    "filled": Status.ALLTRADED,
    "canceled": Status.CANCELLED
}

# 委托类型映射
ORDERTYPE_OKX2VT: Dict[str, OrderType] = {
    "limit": OrderType.LIMIT,
    "fok": OrderType.FOK,
    "ioc": OrderType.FAK
}
ORDERTYPE_VT2OKX: Dict[OrderType, str] = {v: k for k, v in ORDERTYPE_OKX2VT.items()}

# 买卖方向映射
DIRECTION_OKX2VT: Dict[str, Direction] = {
    "buy": Direction.LONG,
    "sell": Direction.SHORT
}
DIRECTION_VT2OKX: Dict[Direction, str] = {v: k for k, v in DIRECTION_OKX2VT.items()}

# 数据频率映射
INTERVAL_VT2OKX: Dict[Interval, str] = {
    Interval.MINUTE: "1m",
    Interval.HOUR: "1H",
    Interval.DAILY: "1D",
}

# 产品类型映射
PRODUCT_OKX2VT: Dict[str, Product] = {
    "SWAP": Product.FUTURES,
    "SPOT": Product.SPOT,
    "FUTURES": Product.FUTURES
}
PRODUCT_VT2OKX: Dict[Product, str] = {v: k for k, v in PRODUCT_OKX2VT.items()}

# 合约数据全局缓存字典
symbol_contract_map: Dict[str, ContractData] = {}

# 本地委托号缓存集合
local_orderids: Set[str] = set()


class OkxGateway(BaseGateway):
    """
    vn.py用于对接OKX统一账户的交易接口。
    """

    default_name = "OKX"

    default_setting: Dict[str, Any] = {
        "API Key": "",
        "Secret Key": "",
        "Passphrase": "",
        "代理地址": "",
        "代理端口": "",
        "服务器": ["REAL", "TEST"]
    }

    exchanges: Exchange = [Exchange.OKX]

    def __init__(self, event_engine: EventEngine, gateway_name: str = "OKX") -> None:
        """构造函数"""
        super().__init__(event_engine, gateway_name)

        self.rest_api: "OkxRestApi" = OkxRestApi(self)
        self.ws_public_api: "OkxWebsocketPublicApi" = OkxWebsocketPublicApi(self)
        self.ws_private_api: "OkxWebsocketPrivateApi" = OkxWebsocketPrivateApi(self)

        self.orders: Dict[str, OrderData] = {}

    def connect(self, setting: dict) -> None:
        """连接交易接口"""
        key: str = setting["API Key"]
        secret: str = setting["Secret Key"]
        passphrase: str = setting["Passphrase"]
        proxy_host: str = setting["代理地址"]
        proxy_port: str = setting["代理端口"]
        server: str = setting["服务器"]

        if proxy_port.isdigit():
            proxy_port = int(proxy_port)
        else:
            proxy_port = 0

        self.rest_api.connect(
            key,
            secret,
            passphrase,
            proxy_host,
            proxy_port,
            server
        )
        self.ws_public_api.connect(
            proxy_host,
            proxy_port,
            server
        )
        self.ws_private_api.connect(
            key,
            secret,
            passphrase,
            proxy_host,
            proxy_port,
            server
        )

    def subscribe(self, req: SubscribeRequest) -> None:
        """订阅行情"""
        self.ws_public_api.subscribe(req)

    def send_order(self, req: OrderRequest) -> str:
        """委托下单"""
        return self.ws_private_api.send_order(req)

    def cancel_order(self, req: CancelRequest) -> None:
        """委托撤单"""
        self.ws_private_api.cancel_order(req)

    def query_account(self) -> None:
        """查询资金"""
        pass

    def query_position(self) -> None:
        """查询持仓"""
        pass

    def query_history(self, req: HistoryRequest) -> List[BarData]:
        """查询历史数据"""
        return self.rest_api.query_history(req)

    def close(self) -> None:
        """关闭连接"""
        self.rest_api.stop()
        self.ws_public_api.stop()
        self.ws_private_api.stop()

    def on_order(self, order: OrderData) -> None:
        """推送委托数据"""
        self.orders[order.orderid] = order  # 先做一次缓存
        super().on_order(order)

    def get_order(self, orderid: str) -> OrderData:
        """查询委托数据"""
        return self.orders.get(orderid, None)


class OkxRestApi(RestClient):
    """"""

    def __init__(self, gateway: OkxGateway) -> None:
        """构造函数"""
        super().__init__()

        self.gateway: OkxGateway = gateway
        self.gateway_name: str = gateway.gateway_name

        self.key: str = ""
        self.secret: str = ""
        self.passphrase: str = ""
        self.simulated: bool = False

    def sign(self, request: Request) -> Request:
        """生成欧易V5签名"""
        # 签名
        timestamp: str = generate_timestamp()
        request.data = json.dumps(request.data)

        if request.params:
            path: str = request.path + "?" + urlencode(request.params)
        else:
            path: str = request.path

        msg: str = timestamp + request.method + path + request.data
        signature: bytes = generate_signature(msg, self.secret)

        # 添加请求头
        request.headers = {
            "OK-ACCESS-KEY": self.key,
            "OK-ACCESS-SIGN": signature.decode(),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json"
        }

        if self.simulated:
            request.headers["x-simulated-trading"] = "1"

        return request

    def connect(
        self,
        key: str,
        secret: str,
        passphrase: str,
        proxy_host: str,
        proxy_port: int,
        server: str
    ) -> None:
        """连接REST服务器"""
        self.key = key
        self.secret = secret.encode()
        self.passphrase = passphrase

        if server == "TEST":
            self.simulated = True

        self.connect_time = int(datetime.now().strftime("%y%m%d%H%M%S"))

        self.init(REST_HOST, proxy_host, proxy_port)
        self.start()
        self.gateway.write_log("REST API启动成功")

        self.query_time()
        self.query_order()
        self.query_instrument()

    def query_order(self) -> None:
        """查询未成交委托"""
        self.add_request(
            "GET",
            "/api/v5/trade/orders-pending",
            callback=self.on_query_order,
        )

    def query_time(self) -> None:
        """查询时间"""
        self.add_request(
            "GET",
            "/api/v5/public/time",
            callback=self.on_query_time
        )

    def on_query_order(self, packet: dict, request: Request) -> None:
        """未成交委托查询回报"""
        for order_info in packet["data"]:
            order: OrderData = parse_order_data(
                order_info,
                self.gateway_name
            )
            self.gateway.on_order(order)

        self.gateway.write_log("委托信息查询成功")

    def query_instrument(self) -> None:
        """查询合约"""
        for inst_type in PRODUCT_OKX2VT.keys():
            self.add_request(
                "GET",
                "/api/v5/public/instruments",
                callback=self.on_query_instrument,
                params={"instType": inst_type}
            )

    def on_query_time(self, packet: dict, request: Request) -> None:
        """时间查询回报"""
        timestamp: int = int(packet["data"][0]["ts"])
        server_time: datetime = datetime.fromtimestamp(timestamp / 1000)
        local_time: datetime = datetime.now()
        msg: str = f"服务器时间：{server_time}，本机时间：{local_time}"
        self.gateway.write_log(msg)

    def on_query_instrument(self, packet: dict, request: Request) -> None:
        """合约查询回报"""
        data: list = packet["data"]

        for d in data:
            # 提取信息生成合约对象
            symbol: str = d["instId"]
            product: Product = PRODUCT_OKX2VT[d["instType"]]
            net_position: bool = True

            if product == Product.SPOT:
                size: float = 1
            else:
                size: float = float(d["ctMult"])

            contract: ContractData = ContractData(
                symbol=symbol,
                exchange=Exchange.OKX,
                name=symbol,
                product=product,
                size=size,
                pricetick=float(d["tickSz"]),
                min_volume=float(d["minSz"]),
                history_data=True,
                net_position=net_position,
                gateway_name=self.gateway_name,
            )

            # 缓存合约信息并推送
            symbol_contract_map[contract.symbol] = contract
            self.gateway.on_contract(contract)

        self.gateway.write_log(f"{d['instType']}合约信息查询成功")

    def on_error(
        self,
        exception_type: type,
        exception_value: Exception,
        tb: TracebackType,
        request: Request
    ) -> None:
        """触发异常回报"""
        msg: str = f"触发异常，状态码：{exception_type}，信息：{exception_value}"
        self.gateway.write_log(msg)

        sys.stderr.write(
            self.exception_detail(exception_type, exception_value, tb, request)
        )

    def query_history(self, req: HistoryRequest) -> List[BarData]:
        """
        查询历史数据

        K线数据每个粒度最多可获取最近1440条
        """
        buf: Dict[datetime, BarData] = {}
        end_time: str = ""
        path: str = "/api/v5/market/candles"

        for i in range(15):
            # 创建查询参数
            params: dict = {
                "instId": req.symbol,
                "bar": INTERVAL_VT2OKX[req.interval]
            }

            if end_time:
                params["after"] = end_time

            # 从服务器获取响应
            resp: Response = self.request(
                "GET",
                path,
                params=params
            )

            # 如果请求失败则终止循环
            if resp.status_code // 100 != 2:
                msg = f"获取历史数据失败，状态码：{resp.status_code}，信息：{resp.text}"
                self.gateway.write_log(msg)
                break
            else:
                data: dict = resp.json()

                if not data["data"]:
                    m = data["msg"]
                    msg = f"获取历史数据为空，{m}"
                    break

                for bar_list in data["data"]:
                    ts, o, h, l, c, vol, volcc, volCcyQuote, confirm = bar_list
                    dt = parse_timestamp(ts)
                    bar: BarData = BarData(
                        symbol=req.symbol,
                        exchange=req.exchange,
                        datetime=dt,
                        interval=req.interval,
                        volume=float(vol),
                        open_price=float(o),
                        high_price=float(h),
                        low_price=float(l),
                        close_price=float(c),
                        gateway_name=self.gateway_name
                    )
                    buf[bar.datetime] = bar

                begin: str = data["data"][-1][0]
                end: str = data["data"][0][0]
                msg: str = f"获取历史数据成功，{req.symbol} - {req.interval.value}，{parse_timestamp(begin)} - {parse_timestamp(end)}"
                self.gateway.write_log(msg)

                # 更新结束时间
                end_time = begin

        index: List[datetime] = list(buf.keys())
        index.sort()

        history: List[BarData] = [buf[i] for i in index]
        return history


class OkxWebsocketPublicApi(WebsocketClient):
    """"""

    def __init__(self, gateway: OkxGateway) -> None:
        """构造函数"""
        super().__init__()

        self.gateway: OkxGateway = gateway
        self.gateway_name: str = gateway.gateway_name

        self.subscribed: Dict[str, SubscribeRequest] = {}
        self.ticks: Dict[str, TickData] = {}

        self.callbacks: Dict[str, callable] = {
            "tickers": self.on_ticker,
            "books5": self.on_depth
        }

    def connect(
        self,
        proxy_host: str,
        proxy_port: int,
        server: str
    ) -> None:
        """连接Websocket公共频道"""
        if server == "REAL":
            self.init(PUBLIC_WEBSOCKET_HOST, proxy_host, proxy_port, 20)
        else:
            self.init(TEST_PUBLIC_WEBSOCKET_HOST, proxy_host, proxy_port, 20)

        self.start()

    def subscribe(self, req: SubscribeRequest) -> None:
        """订阅行情"""
        # 缓存订阅记录
        self.subscribed[req.vt_symbol] = req

        # 创建TICK对象
        tick: TickData = TickData(
            symbol=req.symbol,
            exchange=req.exchange,
            name=req.symbol,
            datetime=datetime.now(CHINA_TZ),
            gateway_name=self.gateway_name,
        )
        self.ticks[req.symbol] = tick

        # 发送订阅请求
        args: list = []
        for channel in ["tickers", "books5"]:
            args.append({
                "channel": channel,
                "instId": req.symbol
            })

        req: dict = {
            "op": "subscribe",
            "args": args
        }
        self.send_packet(req)

    def on_connected(self) -> None:
        """连接成功回报"""
        self.gateway.write_log("Websocket Public API连接成功")

        for req in list(self.subscribed.values()):
            self.subscribe(req)

    def on_disconnected(self) -> None:
        """连接断开回报"""
        self.gateway.write_log("Websocket Public API连接断开")

    def on_packet(self, packet: dict) -> None:
        """推送数据回报"""
        if "event" in packet:
            event: str = packet["event"]
            if event == "subscribe":
                return
            elif event == "error":
                code: str = packet["code"]
                msg: str = packet["msg"]
                self.gateway.write_log(f"Websocket Public API请求异常, 状态码：{code}, 信息：{msg}")
        else:
            channel: str = packet["arg"]["channel"]
            callback: callable = self.callbacks.get(channel, None)

            if callback:
                data: list = packet["data"]
                callback(data)

    def on_error(self, exception_type: type, exception_value: Exception, tb) -> None:
        """触发异常回报"""
        msg: str = f"公共频道触发异常，类型：{exception_type}，信息：{exception_value}"
        self.gateway.write_log(msg)

        sys.stderr.write(
            self.exception_detail(exception_type, exception_value, tb)
        )

    def on_ticker(self, data: list) -> None:
        """行情推送回报"""
        for d in data:
            tick: TickData = self.ticks[d["instId"]]
            tick.last_price = float(d["last"])
            tick.open_price = float(d["open24h"])
            tick.high_price = float(d["high24h"])
            tick.low_price = float(d["low24h"])
            tick.volume = float(d["vol24h"])

    def on_depth(self, data: list) -> None:
        """盘口推送回报"""
        for d in data:
            tick: TickData = self.ticks[d["instId"]]
            bids: list = d["bids"]
            asks: list = d["asks"]

            for n in range(min(5, len(bids))):
                price, volume, _, _ = bids[n]
                tick.__setattr__("bid_price_%s" % (n + 1), float(price))
                tick.__setattr__("bid_volume_%s" % (n + 1), float(volume))

            for n in range(min(5, len(asks))):
                price, volume, _, _ = asks[n]
                tick.__setattr__("ask_price_%s" % (n + 1), float(price))
                tick.__setattr__("ask_volume_%s" % (n + 1), float(volume))

            tick.datetime = parse_timestamp(d["ts"])
            self.gateway.on_tick(copy(tick))


class OkxWebsocketPrivateApi(WebsocketClient):
    """"""

    def __init__(self, gateway: OkxGateway) -> None:
        """构造函数"""
        super().__init__()

        self.gateway: OkxGateway = gateway
        self.gateway_name: str = gateway.gateway_name

        self.key: str = ""
        self.secret: str = ""
        self.passphrase: str = ""

        self.reqid: int = 0
        self.order_count: int = 0
        self.connect_time: int = 0

        self.callbacks: Dict[str, callable] = {
            "login": self.on_login,
            "orders": self.on_order,
            "account": self.on_account,
            "positions": self.on_position,
            "order": self.on_send_order,
            "cancel-order": self.on_cancel_order,
            "error": self.on_api_error
        }

        self.reqid_order_map: Dict[str, OrderData] = {}

    def connect(
        self,
        key: str,
        secret: str,
        passphrase: str,
        proxy_host: str,
        proxy_port: int,
        server: str
    ) -> None:
        """连接Websocket私有频道"""
        self.key = key
        self.secret = secret.encode()
        self.passphrase = passphrase

        self.connect_time = int(datetime.now().strftime("%y%m%d%H%M%S"))

        if server == "REAL":
            self.init(PRIVATE_WEBSOCKET_HOST, proxy_host, proxy_port, 20)
        else:
            self.init(TEST_PRIVATE_WEBSOCKET_HOST, proxy_host, proxy_port, 20)

        self.start()

    def on_connected(self) -> None:
        """连接成功回报"""
        self.gateway.write_log("Websocket Private API连接成功")
        self.login()

    def on_disconnected(self) -> None:
        """连接断开回报"""
        self.gateway.write_log("Websocket Private API连接断开")

    def on_packet(self, packet: dict) -> None:
        """推送数据回报"""
        if "event" in packet:
            cb_name: str = packet["event"]
        elif "op" in packet:
            cb_name: str = packet["op"]
        else:
            cb_name: str = packet["arg"]["channel"]

        callback: callable = self.callbacks.get(cb_name, None)
        if callback:
            callback(packet)

    def on_error(self, exception_type: type, exception_value: Exception, tb) -> None:
        """触发异常回报"""
        msg: str = f"私有频道触发异常，类型：{exception_type}，信息：{exception_value}"
        self.gateway.write_log(msg)

        sys.stderr.write(
            self.exception_detail(exception_type, exception_value, tb)
        )

    def on_api_error(self, packet: dict) -> None:
        """用户登录请求回报"""
        code: str = packet["code"]
        msg: str = packet["msg"]
        self.gateway.write_log(f"Websocket Private API请求失败, 状态码：{code}, 信息：{msg}")

    def on_login(self, packet: dict) -> None:
        """用户登录请求回报"""
        if packet["code"] == '0':
            self.gateway.write_log("Websocket Private API登录成功")
            self.subscribe_topic()
        else:
            self.gateway.write_log("Websocket Private API登录失败")

    def on_order(self, packet: dict) -> None:
        """委托更新推送"""
        data: list = packet["data"]
        for d in data:
            order: OrderData = parse_order_data(d, self.gateway_name)
            self.gateway.on_order(order)

            # 检查是否有成交
            if d["fillSz"] == "0":
                return

            # 将成交数量四舍五入到正确精度
            trade_volume: float = float(d["fillSz"])
            contract: ContractData = symbol_contract_map.get(order.symbol, None)
            if contract:
                trade_volume = round_to(trade_volume, contract.min_volume)

            trade: TradeData = TradeData(
                symbol=order.symbol,
                exchange=order.exchange,
                orderid=order.orderid,
                tradeid=d["tradeId"],
                direction=order.direction,
                offset=order.offset,
                price=float(d["fillPx"]),
                volume=trade_volume,
                datetime=parse_timestamp(d["uTime"]),
                gateway_name=self.gateway_name,
            )
            self.gateway.on_trade(trade)

    def on_account(self, packet: dict) -> None:
        """资金更新推送"""
        if len(packet["data"]) == 0:
            return
        buf: dict = packet["data"][0]
        for detail in buf["details"]:
            account: AccountData = AccountData(
                accountid=detail["ccy"],
                balance=float(detail["eq"]),
                gateway_name=self.gateway_name,
            )
            account.available = float(detail["availEq"]) if len(detail["availEq"]) != 0 else 0.0
            account.frozen = account.balance - account.available
            self.gateway.on_account(account)

    def on_position(self, packet: dict) -> None:
        """持仓更新推送"""
        data: list = packet["data"]
        for d in data:
            symbol: str = d["instId"]
            pos: int = float(d["pos"])
            price: float = get_float_value(d, "avgPx")
            pnl: float = get_float_value(d, "upl")

            position: PositionData = PositionData(
                symbol=symbol,
                exchange=Exchange.OKX,
                direction=Direction.NET,
                volume=pos,
                price=price,
                pnl=pnl,
                gateway_name=self.gateway_name,
            )
            self.gateway.on_position(position)

    def on_send_order(self, packet: dict) -> None:
        """委托下单回报"""
        data: list = packet["data"]

        # 请求本身格式错误（没有委托的回报数据）
        if packet["code"] != "0":
            if not data:
                order: OrderData = self.reqid_order_map[packet["id"]]
                order.status = Status.REJECTED
                self.gateway.on_order(order)
                return

        # 业务逻辑处理失败
        for d in data:
            code: str = d["sCode"]
            if code == "0":
                return

            orderid: str = d["clOrdId"]
            order: OrderData = self.gateway.get_order(orderid)
            if not order:
                return
            order.status = Status.REJECTED
            self.gateway.on_order(copy(order))

            msg: str = d["sMsg"]
            self.gateway.write_log(f"委托失败，状态码：{code}，信息：{msg}")

    def on_cancel_order(self, packet: dict) -> None:
        """委托撤单回报"""
        # 请求本身的格式错误
        if packet["code"] != "0":
            code: str = packet["code"]
            msg: str = packet["msg"]
            self.gateway.write_log(f"撤单失败，状态码：{code}，信息：{msg}")
            return

        # 业务逻辑处理失败
        data: list = packet["data"]
        for d in data:
            code: str = d["sCode"]
            if code == "0":
                return

            msg: str = d["sMsg"]
            self.gateway.write_log(f"撤单失败，状态码：{code}，信息：{msg}")

    def login(self) -> None:
        """用户登录"""
        timestamp: str = str(time.time())
        msg: str = timestamp + "GET" + "/users/self/verify"
        signature: bytes = generate_signature(msg, self.secret)

        okx_req: dict = {
            "op": "login",
            "args":
            [
                {
                    "apiKey": self.key,
                    "passphrase": self.passphrase,
                    "timestamp": timestamp,
                    "sign": signature.decode("utf-8")
                }
            ]
        }
        self.send_packet(okx_req)

    def subscribe_topic(self) -> None:
        """订阅委托、资金和持仓推送"""
        okx_req: dict = {
            "op": "subscribe",
            "args": [
                {
                    "channel": "orders",
                    "instType": "ANY"
                },
                {
                    "channel": "account"
                },
                {
                    "channel": "positions",
                    "instType": "ANY"
                },
            ]
        }
        self.send_packet(okx_req)

    def send_order(self, req: OrderRequest) -> str:
        """委托下单"""
        # 检查委托类型是否正确
        if req.type not in ORDERTYPE_VT2OKX:
            self.gateway.write_log(f"委托失败，不支持的委托类型：{req.type.value}")
            return

        # 检查合约代码是否正确
        contract: ContractData = symbol_contract_map.get(req.symbol, None)
        if not contract:
            self.gateway.write_log(f"委托失败，找不到该合约代码{req.symbol}")
            return

        # 生成本地委托号
        self.order_count += 1
        count_str = str(self.order_count).rjust(6, "0")
        orderid = f"{self.connect_time}{count_str}"

        # 生成委托请求
        args: dict = {
            "instId": req.symbol,
            "clOrdId": orderid,
            "side": DIRECTION_VT2OKX[req.direction],
            "ordType": ORDERTYPE_VT2OKX[req.type],
            "px": str(req.price),
            "sz": str(req.volume)
        }

        if contract.product == Product.SPOT:
            args["tdMode"] = "cash"
        else:
            args["tdMode"] = "cross"

        self.reqid += 1
        okx_req: dict = {
            "id": str(self.reqid),
            "op": "order",
            "args": [args]
        }
        self.send_packet(okx_req)

        # 推送提交中事件
        order: OrderData = req.create_order_data(orderid, self.gateway_name)
        self.gateway.on_order(order)
        return order.vt_orderid

    def cancel_order(self, req: CancelRequest) -> None:
        """委托撤单"""
        args: dict = {"instId": req.symbol}

        # 检查是否为本地委托号
        if req.orderid in local_orderids:
            args["clOrdId"] = req.orderid
        else:
            args["ordId"] = req.orderid

        self.reqid += 1
        okx_req: dict = {
            "id": str(self.reqid),
            "op": "cancel-order",
            "args": [args]
        }
        self.send_packet(okx_req)


def generate_signature(msg: str, secret_key: str) -> bytes:
    """生成签名"""
    return base64.b64encode(hmac.new(secret_key, msg.encode(), hashlib.sha256).digest())


def generate_timestamp() -> str:
    """生成时间戳"""
    now: datetime = datetime.utcnow()
    timestamp: str = now.isoformat("T", "milliseconds")
    return timestamp + "Z"


def parse_timestamp(timestamp: str) -> datetime:
    """解析回报时间戳"""
    dt: datetime = datetime.fromtimestamp(int(timestamp) / 1000)
    return dt.replace(tzinfo=CHINA_TZ)


def get_float_value(data: dict, key: str) -> float:
    """获取字典中对应键的浮点数值"""
    data_str: str = data.get(key, "")
    if not data_str:
        return 0.0
    return float(data_str)


def parse_order_data(data: dict, gateway_name: str) -> OrderData:
    """解析委托回报数据"""
    order_id: str = data["clOrdId"]
    if order_id:
        local_orderids.add(order_id)
    else:
        order_id: str = data["ordId"]

    order: OrderData = OrderData(
        symbol=data["instId"],
        exchange=Exchange.OKX,
        type=ORDERTYPE_OKX2VT[data["ordType"]],
        orderid=order_id,
        direction=DIRECTION_OKX2VT[data["side"]],
        offset=Offset.NONE,
        traded=float(data["accFillSz"]),
        price=float(data["px"]),
        volume=float(data["sz"]),
        datetime=parse_timestamp(data["cTime"]),
        status=STATUS_OKX2VT[data["state"]],
        gateway_name=gateway_name,
    )
    return order
