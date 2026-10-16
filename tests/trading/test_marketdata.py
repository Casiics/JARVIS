"""Live market data: n8n fetches, deterministic code validates, the session trades on time.

The provider's answer is untrusted. One bad bar refuses the whole answer, a still-forming bar
is never traded, and history only primes the strategy.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from mod_trading import __main__ as cli
from mod_trading.feed import read_bars
from mod_trading.marketdata import (
    MCP_TOOL,
    BarRecorder,
    HttpResponse,
    LiveFeed,
    MarketDataError,
    McpBarSource,
    parse_chart,
)
from mod_trading.schemas import Bar
from trading_risk import KillSwitch

OPEN = datetime(2026, 10, 16, 13, 30, tzinfo=UTC)  # 09:30 New York


def chart(
    closes: list[float | None],
    *,
    symbol: str = "SPY",
    start: datetime = OPEN,
    granularity: str = "1m",
    high_offset: float = 0.5,
) -> dict[str, object]:
    ts = [int((start + timedelta(minutes=i)).timestamp()) for i in range(len(closes))]
    quote = {
        "open": closes,
        "high": [None if c is None else c + high_offset for c in closes],
        "low": [None if c is None else c - 0.5 for c in closes],
        "close": closes,
        "volume": [None if c is None else 1000 for c in closes],
    }
    return {
        "chart": {
            "result": [
                {
                    "meta": {"symbol": symbol, "dataGranularity": granularity},
                    "timestamp": ts,
                    "indicators": {"quote": [quote]},
                }
            ],
            "error": None,
        }
    }


def raw(closes: list[float | None], **kw: object) -> str:
    return json.dumps(chart(closes, **kw))  # type: ignore[arg-type]


# --- parsing ------------------------------------------------------------------------------------


def test_closed_bars_carry_their_close_time() -> None:
    now = OPEN + timedelta(minutes=3, seconds=10)
    bars = parse_chart(raw([500.0, None, 501.25, 502.0]), "SPY", now=now)
    # The empty minute is skipped and the fourth bar (closes 13:34) is still forming.
    assert [b.ts for b in bars] == [OPEN + timedelta(minutes=1), OPEN + timedelta(minutes=3)]
    assert bars[1].close == Decimal("501.250000") and bars[1].volume == 1000


def test_a_body_wrapped_by_n8n_is_accepted() -> None:
    wrapped = json.dumps([chart([500.0])])
    assert len(parse_chart(wrapped, "SPY", now=OPEN + timedelta(hours=1))) == 1


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("<html>blocked</html>", "not JSON"),
        (json.dumps({"chart": {"result": None, "error": {"code": "Not Found"}}}), "error"),
        (raw([500.0], symbol="QQQ"), "another instrument"),
        (raw([500.0], granularity="5m"), "bar size"),
        (raw([500.0, 501.0], high_offset=-2.0), "inconsistent bar"),
        (json.dumps({"something": "else"}), "not a chart"),
    ],
)
def test_bad_answers_are_refused_whole(body: str, reason: str) -> None:
    with pytest.raises(MarketDataError, match=reason):
        parse_chart(body, "SPY", now=OPEN + timedelta(hours=1))


def test_columns_of_different_length_are_refused() -> None:
    data = chart([500.0, 501.0])
    data["chart"]["result"][0]["indicators"]["quote"][0]["close"] = [500.0]  # type: ignore[index]
    with pytest.raises(MarketDataError, match="length"):
        parse_chart(json.dumps(data), "SPY", now=OPEN + timedelta(hours=1))


# --- MCP ----------------------------------------------------------------------------------------


class FakeGateway:
    def __init__(self, tool_result: dict[str, object], status: int = 200) -> None:
        self.tool_result = tool_result
        self.status = status
        self.calls: list[tuple[dict[str, object], dict[str, str]]] = []

    def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> HttpResponse:
        message = json.loads(body)
        self.calls.append((message, headers))
        if self.status != 200:
            return HttpResponse(self.status, {}, b"")
        if "id" not in message:
            return HttpResponse(202, {}, b"")
        result = (
            {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}
            if message["method"] == "initialize"
            else self.tool_result
        )
        reply = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result})
        sse = f"event: message\ndata: {reply}\n\n"
        return HttpResponse(
            200,
            {"content-type": "text/event-stream", "mcp-session-id": "s-1"},
            sse.encode(),
        )


def test_bars_come_from_the_mcp_tool() -> None:
    gateway = FakeGateway({"content": [{"type": "text", "text": raw([500.0, 501.0])}]})
    bars = McpBarSource("http://mcp-gateway-trading:8080/mcp/marketdata", gateway).bars(
        "SPY", OPEN + timedelta(hours=1)
    )
    assert len(bars) == 2
    methods = [m.get("method") for m, _ in gateway.calls]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    call, headers = gateway.calls[-1]
    assert call["params"] == {"name": MCP_TOOL, "arguments": {"symbol": "SPY"}}
    assert headers["mcp-session-id"] == "s-1"


@pytest.mark.parametrize(
    "gateway",
    [
        FakeGateway({}, status=403),  # the policy gateway refused
        FakeGateway({"isError": True, "content": [{"type": "text", "text": "timeout"}]}),
        FakeGateway({"content": []}),
        FakeGateway(
            {"content": [{"type": "text", "text": "NodeOperationError: Service unavailable"}]}
        ),
    ],
)
def test_gateway_and_tool_failures_raise(gateway: FakeGateway) -> None:
    with pytest.raises(MarketDataError):
        McpBarSource("http://x/mcp/marketdata", gateway).raw("SPY")


@pytest.mark.parametrize("symbol", ["../v7/quote", "spy now", "", "A" * 30])
def test_only_plain_symbols_are_sent(symbol: str) -> None:
    gateway = FakeGateway({})
    with pytest.raises(MarketDataError, match="symbol"):
        McpBarSource("http://x/mcp/marketdata", gateway).raw(symbol)
    assert gateway.calls == []


# --- live feed ----------------------------------------------------------------------------------


class ScriptedSource:
    """Each poll returns the next answer; an exception instance is raised instead."""

    def __init__(self, answers: list[list[Bar] | Exception]) -> None:
        self.answers = answers

    def bars(self, symbol: str, now: datetime) -> list[Bar]:
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def bars(n: int, start: int = 0) -> list[Bar]:
    return parse_chart(
        raw([500.0 + i for i in range(start + n)]), "SPY", now=OPEN + timedelta(hours=2)
    )[start:]


def test_history_primes_and_only_new_bars_are_streamed() -> None:
    errors: list[str] = []
    recorded: list[Bar] = []
    source = ScriptedSource([bars(3), bars(5), MarketDataError("gateway unreachable"), bars(6)])
    feed = LiveFeed(
        source,
        "SPY",
        Decimal(4),
        sleep=lambda _s: None,
        on_error=errors.append,
        record=recorded.append,
        max_polls=3,
    )
    assert len(feed.warmup()) == 3
    streamed = [bar for bar, _quote in feed.stream()]
    assert [b.close for b in streamed] == [
        Decimal("503.000000"),
        Decimal("504.000000"),
        Decimal("505.000000"),
    ]
    assert recorded == streamed
    assert errors == ["gateway unreachable"]


def test_recorded_bars_can_be_backtested(tmp_path: Path) -> None:
    record = BarRecorder(tmp_path)
    for bar in bars(3):
        record(bar)
    [path] = tmp_path.iterdir()
    assert read_bars(path) == bars(3)


# --- command line -------------------------------------------------------------------------------


def test_paper_live_refuses_until_the_switch_is_released(tmp_path: Path) -> None:
    assert (
        cli.main(["--data-dir", str(tmp_path), "paper-live", "--instrument", "SPY", "--once"]) == 3
    )


def test_paper_live_polls_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    now = (datetime.now(UTC) - timedelta(minutes=1)).replace(second=0, microsecond=0)
    start = now - timedelta(minutes=40)
    answer = raw([500.0 + (i % 7) for i in range(40)], start=start)

    class Source:
        def __init__(self, url: str) -> None:
            assert url.endswith("/mcp/marketdata")

        def bars(self, symbol: str, now: datetime) -> list[Bar]:
            return parse_chart(answer, symbol, now=now)

    monkeypatch.setattr(cli, "McpBarSource", Source)
    data = ["--data-dir", str(tmp_path)]
    assert cli.main([*data, "kill-switch", "release"]) == 0
    assert cli.main([*data, "paper-live", "--instrument", "SPY", "--once"]) == 0
    out = capsys.readouterr().out
    # The first poll is history only; nothing new arrived within the same poll.
    assert "SPY: 40 bars of history" in out and "bars 0, intents 0" in out


def test_service_mode_waits_for_the_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Empty:
        def __init__(self, url: str) -> None:
            pass

        def bars(self, symbol: str, now: datetime) -> list[Bar]:
            return []

    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        KillSwitch(tmp_path / "killswitch").release()  # a human, some time later

    monkeypatch.setattr(cli, "McpBarSource", Empty)
    monkeypatch.setattr(time, "sleep", sleep)
    args = ["--data-dir", str(tmp_path), "paper-live", "--instrument", "SPY", "--once"]
    assert cli.main([*args, "--wait-for-release"]) == 0
    assert slept and "waiting for a human" in capsys.readouterr().out
