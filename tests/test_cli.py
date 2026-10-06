from datetime import datetime

import pytest

from recordit import cli

# Screen row the tests pretend the button row is on, and the column of each
# button's icon (see cli._button_layout).
ROW = 10
PAUSE_COL, STOP_COL, DELETE_COL = 5, 13, 21


def press(col, row=ROW):
    return f"\x1b[<0;{col};{row}M".encode()


def release(col, row=ROW):
    return f"\x1b[<0;{col};{row}m".encode()


def click(col, row=ROW):
    return press(col, row) + release(col, row)


class FakeInput:
    """Stands in for terminal.RawInput: hands out queued chunks, then EOF."""

    def __init__(self, *chunks):
        self.chunks = list(chunks)

    def read(self, timeout):
        return self.chunks.pop(0) if self.chunks else None


@pytest.fixture
def panel():
    panel = cli._ControlPanel()
    panel.button_row = ROW
    return panel


@pytest.fixture
def daemon_commands(monkeypatch):
    """Record commands sent to the daemon instead of sending them."""
    sent = []

    def fake_send(cmd):
        sent.append(cmd)
        return {"ok": True}

    monkeypatch.setattr(cli, "_send_command", fake_send)
    return sent


@pytest.mark.parametrize(
    "now, expected",
    [
        (datetime(2026, 10, 6, 12, 28, 2), "recordit 2026-10-06 at 12.28.02 PM"),
        (datetime(2026, 10, 6, 9, 5, 3), "recordit 2026-10-06 at 9.05.03 AM"),
        (datetime(2026, 1, 2, 0, 5, 9), "recordit 2026-01-02 at 12.05.09 AM"),
        (datetime(2026, 1, 2, 23, 59, 59), "recordit 2026-01-02 at 11.59.59 PM"),
    ],
)
def test_default_filename(monkeypatch, now, expected):
    class FixedDatetime:
        @staticmethod
        def now():
            return now

    monkeypatch.setattr(cli, "datetime", FixedDatetime)
    assert cli._default_filename() == expected


@pytest.mark.parametrize("name, expected", [("take1", "take1.wav"), ("take1.wav", "take1.wav"), ("a.WAV", "a.WAV")])
def test_normalize_wav_name(name, expected):
    assert cli._normalize_wav_name(name) == expected


def test_level_char_is_linear_in_amplitude():
    assert cli._level_char(0) == "▁"
    assert cli._level_char(cli.FULL_SCALE) == "█"
    assert cli._level_char(cli.FULL_SCALE // 2) == "▅"  # -6 dBFS: mid-height
    assert cli._level_char(cli.FULL_SCALE // 10) == "▂"  # -20 dBFS: low
    bars = [cli._level_char(peak) for peak in range(0, cli.FULL_SCALE + 1, 512)]
    assert bars == sorted(bars, key=cli.WAVE_CHARS.index)


def test_parse_input_events():
    assert cli._parse_input(b"sx") == ("key", b"s", b"x")
    assert cli._parse_input(press(13) + b"s") == ("mouse", (b"0", b"13", b"10", b"M"), b"s")
    assert cli._parse_input(b"\x1b[10;1R") == ("position", (b"10", b"1"), b"")
    assert cli._parse_input(b"\x1b[A") == ("ignore", (), b"")  # arrow key
    assert cli._parse_input(b"\x1bx") == ("ignore", None, b"")  # Alt+x


@pytest.mark.parametrize("partial", [b"\x1b", b"\x1b[", b"\x1b[<0;13;1"])
def test_parse_input_waits_for_rest_of_sequence(partial):
    assert cli._parse_input(partial) is None


@pytest.mark.parametrize(
    "chunks, action",
    [
        ([b"s"], "stop"),
        ([b"S"], "stop"),
        ([b"x"], "delete"),
        ([b"\x04"], "stop"),  # Ctrl+D
        ([], "stop"),  # stdin closed
        ([click(STOP_COL)], "stop"),
        ([click(DELETE_COL)], "delete"),
        ([click(STOP_COL + 2)], "stop"),  # clicks land on the nearest icon
        ([press(DELETE_COL)[:5], press(DELETE_COL)[5:] + release(DELETE_COL)], "delete"),
        ([b"\x1b[A", b"q", b"s"], "stop"),  # unbound keys are ignored
    ],
)
def test_read_controls(panel, chunks, action):
    assert cli._read_controls(panel, FakeInput(*chunks)) == action


def test_button_acts_on_release_not_press(panel):
    # Pressing ✕ alone must not delete: input then ends, which stops and saves.
    assert cli._read_controls(panel, FakeInput(press(DELETE_COL))) == "stop"


@pytest.mark.parametrize("release_at", [(60, ROW), (DELETE_COL, ROW + 3), (STOP_COL, ROW)])
def test_click_cancelled_by_releasing_elsewhere(panel, release_at):
    chunks = [press(DELETE_COL), release(*release_at), b"s"]
    assert cli._read_controls(panel, FakeInput(*chunks)) == "stop"


def test_clicks_on_other_rows_ignored(panel):
    assert cli._read_controls(panel, FakeInput(click(DELETE_COL, row=ROW - 1), b"s")) == "stop"


def test_position_report_sets_button_row():
    panel = cli._ControlPanel()
    assert cli._read_controls(panel, FakeInput(b"\x1b[7;1R", click(DELETE_COL, row=7))) == "delete"


def test_pause_toggles_with_click_and_keys(panel, daemon_commands):
    cli._read_controls(panel, FakeInput(click(PAUSE_COL)))
    assert panel.paused
    cli._read_controls(panel, FakeInput(b" "))
    assert not panel.paused
    cli._read_controls(panel, FakeInput(b"p"))
    assert panel.paused
    assert daemon_commands == ["pause", "resume", "pause"]


def test_pause_failure_leaves_state_alone(panel, monkeypatch):
    monkeypatch.setattr(cli, "_send_command", lambda cmd: {"ok": False, "error": "Not recording"})
    cli._read_controls(panel, FakeInput(b" "))
    assert not panel.paused


def test_timer_freezes_while_paused(panel, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(cli.time, "time", lambda: clock[0])
    panel.run_started = 100.0
    clock[0] = 103.0
    panel.set_paused(True)
    clock[0] = 110.0
    assert panel.elapsed() == 3.0
    panel.set_paused(False)
    clock[0] = 112.0
    assert panel.elapsed() == 5.0
