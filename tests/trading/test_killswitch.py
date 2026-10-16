"""The kill switch fails closed and only 'released' releases it."""

from __future__ import annotations

import stat
from pathlib import Path

from tests.trading.helpers import T0
from trading_risk import KillSwitch


def test_missing_state_is_engaged(tmp_path: Path) -> None:
    switch = KillSwitch(tmp_path / "killswitch")
    assert switch.engaged()
    assert "fail closed" in switch.state().reason


def test_release_and_engage(tmp_path: Path) -> None:
    switch = KillSwitch(tmp_path / "ks")
    switch.release()
    assert not switch.engaged()
    switch.engage("daily_loss", now=T0)
    state = switch.state()
    assert state.engaged and state.reason == "daily_loss" and state.since == T0
    assert stat.S_IMODE((tmp_path / "ks").stat().st_mode) == 0o600


def test_garbage_and_forged_release_stay_engaged(tmp_path: Path) -> None:
    path = tmp_path / "ks"
    switch = KillSwitch(path)
    for text in ["", "RELEASED", "off", "{}", '{"engaged": false}', "released please"]:
        path.write_text(text)
        assert switch.engaged(), text


def test_unreadable_state_is_engaged(tmp_path: Path) -> None:
    (tmp_path / "ks").mkdir()  # a directory where the file should be
    assert KillSwitch(tmp_path / "ks").engaged()
