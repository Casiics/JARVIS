"""Market regime snapshots: computed from daily bars, one entry per instrument, replaced daily."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jarvis_core.contracts.classification import DataClassification
from jarvis_core.contracts.events import Taint
from jarvis_core.curation.store import FileCurationStore
from mod_trading.curation.__main__ import main
from mod_trading.curation.regime import regime_items

START = datetime(2026, 1, 2, 21, 0, tzinfo=UTC)


def write_bars(path: Path, instrument: str, closes: list[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        out = csv.writer(fh)
        out.writerow(("ts", "instrument", "open", "high", "low", "close", "volume"))
        for day, close in enumerate(closes):
            ts = (START + timedelta(days=day)).isoformat()
            out.writerow((ts, instrument, close, close, close, close, 1000))


def rising(n: int, start: float = 100.0, step: float = 0.5) -> list[float]:
    return [start + step * i + (0.3 if i % 2 else -0.3) for i in range(n)]


def test_snapshot_facts(tmp_path: Path) -> None:
    write_bars(tmp_path / "QQQ.csv", "QQQ", rising(220))
    write_bars(tmp_path / "SPY.csv", "SPY", rising(220, step=0.2))
    write_bars(tmp_path / "IWM.csv", "IWM", rising(30))  # too short: skipped
    write_bars(tmp_path / "TSLA.csv", "TSLA", rising(220))  # not on the watchlist
    (tmp_path / "broken.csv").write_text("nonsense\n1,2\n", encoding="utf-8")
    items = {i.subject: i for i in regime_items(tmp_path)}
    assert set(items) == {"regime:qqq", "regime:spy"}
    qqq = items["regime:qqq"]
    assert qqq.classification is DataClassification.INTERNAL and qqq.taint is Taint.NONE
    assert "uptrend" in qqq.summary
    assert any(f.startswith("above the 200 day average") for f in qqq.facts)
    assert any(f.startswith("20 day return vs SPY: +") for f in qqq.facts)
    assert not any("vs SPY" in f for f in items["regime:spy"].facts)


def test_daily_runs_replace_the_entry(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "trading"
    write_bars(data / "daily" / "QQQ.csv", "QQQ", rising(80))
    assert main(["--data-dir", str(data), "run"]) == 0
    first = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert first["stored"] == 1 and first["review"] == 0

    assert main(["--data-dir", str(data), "run"]) == 0  # nothing new: nothing collected
    write_bars(data / "daily" / "QQQ.csv", "QQQ", rising(82))  # two more days in one go
    assert main(["--data-dir", str(data), "run"]) == 0
    second = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert second["merged"] == 1 and second["stored"] == 0

    entries = FileCurationStore(data / "curation").entries("market-regime")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.version == 2 and entry.subject == "regime:qqq"
    assert entry.facts[0].endswith((START + timedelta(days=81)).date().isoformat())
