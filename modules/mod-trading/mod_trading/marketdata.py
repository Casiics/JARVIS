"""Live market data for paper trading, through the trading domain's n8n MCP tool.

n8n-trading makes the outbound call (`core/n8n-workflows/trading-marketdata.json`, and
egress-trading allows only the provider's host). This module speaks MCP to
mcp-gateway-trading and treats the answer as untrusted external data: it is size-capped and
parsed with strict models, and a payload with one inconsistent bar is refused as a whole, so a
strategy never trades on half-valid data.

Provider: Yahoo Finance's chart endpoint (1-minute bars of the regular session, no account and
no key). Its bars carry their open time; a bar here carries its close time, and the bar that is
still forming is never returned.
"""

from __future__ import annotations

import csv
import http.client
import json
import re
import time
import urllib.parse
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jarvis_core.workload import WorkloadConnection, WorkloadTlsError
from mod_trading.feed import quote_from_bar
from mod_trading.schemas import Bar, Quote

MCP_TOOL = "get_bars"
DAILY_TOOL = "get_daily"
MCP_PROTOCOL = "2025-03-26"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
# How long after the provider's bar timestamp (its open) a bar closes. A daily bar opens at
# 09:30 New York time and closes at 16:00.
BAR_SECONDS = {"1m": 60, "1d": 6 * 3600 + 1800}
SYMBOL = r"^[A-Z0-9.\-^=]{1,20}$"


class MarketDataError(Exception):
    """The data source failed or sent something unusable. Callers trade nothing on it."""


# --- provider payload ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class _Quotes(_Strict):
    open: list[float | None]
    high: list[float | None]
    low: list[float | None]
    close: list[float | None]
    volume: list[int | None]


class _Indicators(_Strict):
    quote: list[_Quotes] = Field(min_length=1, max_length=1)


class _Meta(_Strict):
    symbol: str
    dataGranularity: str


class _Result(_Strict):
    meta: _Meta
    timestamp: list[int] = Field(default_factory=list, max_length=4000)
    indicators: _Indicators


class _Chart(_Strict):
    result: list[_Result] | None = None
    error: object | None = None


class _ChartResponse(_Strict):
    chart: _Chart


def _price(value: float) -> Decimal:
    return Decimal(repr(value)).quantize(Decimal("0.000001"))


def parse_chart(raw: str, instrument: str, *, now: datetime, granularity: str = "1m") -> list[Bar]:
    """Closed bars for `instrument`, oldest first. Raises MarketDataError otherwise."""
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise MarketDataError("response is not JSON") from exc
    # n8n's HTTP Request tool may wrap the provider's body in a one-element list.
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    try:
        chart = _ChartResponse.model_validate(data).chart
    except ValidationError as exc:
        raise MarketDataError("response is not a chart") from exc
    if chart.error is not None or not chart.result:
        raise MarketDataError("provider reported an error")
    result = chart.result[0]
    if result.meta.symbol.upper() != instrument.upper():
        raise MarketDataError("response is for another instrument")
    if result.meta.dataGranularity != granularity:
        raise MarketDataError("response has the wrong bar size")
    q = result.indicators.quote[0]
    columns = (q.open, q.high, q.low, q.close, q.volume)
    if any(len(c) != len(result.timestamp) for c in columns):
        raise MarketDataError("response columns differ in length")
    bars: list[Bar] = []
    for i, opened in enumerate(result.timestamp):
        o, h, low, c, v = (col[i] for col in columns)
        if o is None or h is None or low is None or c is None:
            continue  # the provider leaves minutes without trades empty
        closed = datetime.fromtimestamp(opened + BAR_SECONDS[granularity], UTC)
        if closed > now:
            continue  # still forming
        try:
            bars.append(
                Bar(
                    instrument=instrument,
                    ts=closed,
                    open=_price(o),
                    high=_price(h),
                    low=_price(low),
                    close=_price(c),
                    volume=Decimal(v or 0),
                )
            )
        except (ValidationError, InvalidOperation) as exc:
            raise MarketDataError(f"inconsistent bar at {closed.isoformat()}") from exc
    times = [b.ts for b in bars]
    if times != sorted(set(times)):
        raise MarketDataError("bars are out of order or repeated")
    return bars


# --- MCP transport ------------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


Post = Callable[[str, bytes, dict[str, str]], HttpResponse]


GATEWAY_ID = "spiffe://jarvis.home/trading/mcp-gateway"


def workload_post(cert_dir: Path, peer: str = GATEWAY_ID) -> Post:
    """POST over mutual TLS with this service's workload certificate (jarvis_core.workload).

    The gateway sits on the domain's internal network, so no proxy is used, and it must
    present the trading gateway's SPIFFE ID. The certificate is read again for every request,
    so a renewal by pki-issue takes effect without a restart.
    """

    def post(url: str, body: bytes, headers: dict[str, str]) -> HttpResponse:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise MarketDataError("market data gateway must be an https URL")
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        try:
            conn = WorkloadConnection(
                parts.hostname, parts.port or 443, cert_dir=cert_dir, peer=peer
            )
            try:
                conn.request("POST", path, body=body, headers=headers)
                resp = conn.getresponse()
                payload = resp.read(MAX_RESPONSE_BYTES + 1)
                status, received = resp.status, {k.lower(): v for k, v in resp.getheaders()}
            finally:
                conn.close()
        except WorkloadTlsError as exc:
            raise MarketDataError(f"market data gateway refused: {exc}") from exc
        except (http.client.HTTPException, OSError) as exc:
            raise MarketDataError("market data gateway unreachable") from exc
        if status != 200:
            return HttpResponse(status, {}, b"")
        return HttpResponse(status, received, payload)

    return post


def _message(response: HttpResponse, request_id: int) -> dict[str, object]:
    if response.status != 200:
        raise MarketDataError(f"market data gateway answered HTTP {response.status}")
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise MarketDataError("market data response too large")
    text = response.body.decode("utf-8", "replace")
    if "text/event-stream" in response.headers.get("content-type", ""):
        lines = [line.removeprefix("data:").strip() for line in text.splitlines()]
        candidates = [line for line in lines if line.startswith("{")]
    else:
        candidates = [text]
    for candidate in candidates:
        try:
            message = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    raise MarketDataError("no answer to the market data request")


class McpBarSource:
    """Asks the n8n MCP tool `get_bars` for today's 1-minute bars of one symbol."""

    def __init__(self, url: str, post: Post) -> None:
        self._url = url
        self._post = post

    def _send(self, payload: dict[str, object], session: str | None) -> HttpResponse:
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        }
        if session:
            headers["mcp-session-id"] = session
        return self._post(self._url, json.dumps(payload).encode(), headers)

    def raw(self, symbol: str, tool: str = MCP_TOOL) -> str:
        if not _symbol_ok(symbol):
            raise MarketDataError("symbol not allowed")
        init = self._send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": "jarvis-trading-paper", "version": "0.1.0"},
                },
            },
            None,
        )
        _message(init, 1)
        session = init.headers.get("mcp-session-id")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
        call = self._send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool, "arguments": {"symbol": symbol}},
            },
            session,
        )
        result = _message(call, 2).get("result")
        if not isinstance(result, dict) or result.get("isError"):
            raise MarketDataError("market data tool failed")
        content = result.get("content")
        if not isinstance(content, list) or not content or not isinstance(content[0], dict):
            raise MarketDataError("market data tool returned nothing")
        text = content[0].get("text")
        if not isinstance(text, str):
            raise MarketDataError("market data tool returned no text")
        if not text.lstrip().startswith(("{", "[")):
            # n8n reports a failed HTTP call as error text, not as isError. The text may quote
            # the provider or the proxy, so it is not passed on.
            raise MarketDataError("provider unreachable or refused the request (see n8n-trading)")
        return text

    def bars(self, symbol: str, now: datetime) -> list[Bar]:
        return parse_chart(self.raw(symbol), symbol, now=now)

    def daily(self, symbol: str, now: datetime) -> list[Bar]:
        """Up to ten years of daily bars, for backtests."""
        return parse_chart(self.raw(symbol, DAILY_TOOL), symbol, now=now, granularity="1d")


def _symbol_ok(symbol: str) -> bool:
    return re.fullmatch(SYMBOL, symbol) is not None


# --- live stream --------------------------------------------------------------------------------


class BarSource(Protocol):
    def bars(self, symbol: str, now: datetime) -> list[Bar]: ...


class BarRecorder:
    """Appends bars to <dir>/<instrument>-<date>.csv (or one fixed file), readable by `backtest`."""

    FIELDS = ("ts", "instrument", "open", "high", "low", "close", "volume")

    def __init__(self, directory: Path, name: str | None = None) -> None:
        self._dir = directory
        self._name = name

    def __call__(self, bar: Bar) -> None:
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._dir / (self._name or f"{bar.instrument}-{bar.ts.date().isoformat()}.csv")
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if new:
                writer.writerow(self.FIELDS)
            writer.writerow(
                [bar.ts.isoformat(), bar.instrument, bar.open, bar.high, bar.low, bar.close,
                 bar.volume]
            )  # fmt: skip


@dataclass
class LiveFeed:
    """Polls a bar source and yields each closed bar once, as the session's (bar, quote) stream.

    The first poll returns today's history through `warmup` instead of the stream: those bars
    are too old to trade on and only prime the strategy.
    """

    source: BarSource
    instrument: str
    spread_bps: Decimal
    poll_s: float = 60.0
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], None] = time.sleep
    on_error: Callable[[str], None] = lambda _msg: None
    record: Callable[[Bar], None] = lambda _bar: None
    max_polls: int | None = None
    last: datetime | None = None

    def warmup(self) -> Sequence[Bar]:
        try:
            history = self.source.bars(self.instrument, self.clock())
        except MarketDataError as exc:
            self.on_error(f"{self.instrument}: {exc}")
            return []
        if history:
            self.last = history[-1].ts
        return history

    def poll(self) -> list[tuple[Bar, Quote]]:
        """One request: the bars that closed since the last one, oldest first."""
        try:
            bars = self.source.bars(self.instrument, self.clock())
        except MarketDataError as exc:
            self.on_error(f"{self.instrument}: {exc}")
            return []
        fresh: list[tuple[Bar, Quote]] = []
        for bar in bars:
            if self.last is not None and bar.ts <= self.last:
                continue
            self.last = bar.ts
            self.record(bar)
            fresh.append((bar, quote_from_bar(bar, self.spread_bps)))
        return fresh

    def stream(self) -> Iterator[tuple[Bar, Quote]]:
        return _poll_loop([self], self.poll_s, self.sleep, self.max_polls)


@dataclass
class BasketFeed:
    """Several live feeds polled in turn; each round's new bars are yielded in time order."""

    feeds: Sequence[LiveFeed]
    poll_s: float = 60.0
    sleep: Callable[[float], None] = time.sleep
    max_polls: int | None = None

    def warmup(self) -> list[Bar]:
        return [bar for feed in self.feeds for bar in feed.warmup()]

    def stream(self) -> Iterator[tuple[Bar, Quote]]:
        return _poll_loop(self.feeds, self.poll_s, self.sleep, self.max_polls)


def _poll_loop(
    feeds: Sequence[LiveFeed],
    poll_s: float,
    sleep: Callable[[float], None],
    max_polls: int | None,
) -> Iterator[tuple[Bar, Quote]]:
    polls = 0
    while max_polls is None or polls < max_polls:
        polls += 1
        fresh = [item for feed in feeds for item in feed.poll()]
        fresh.sort(key=lambda item: (item[0].ts, item[0].instrument))
        yield from fresh
        if max_polls is not None and polls >= max_polls:
            return
        try:
            sleep(poll_s)
        except KeyboardInterrupt:
            return
