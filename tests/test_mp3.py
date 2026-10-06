"""Recording to .mp3: a WAV is recorded, then converted with ffmpeg on stop."""

import shutil
import subprocess

import numpy as np
import pytest
import soundfile as sf

from recordit import cli

SAMPLE_RATE = 44100


def write_tone(path, seconds=2.0, channels=2):
    t = np.arange(int(SAMPLE_RATE * seconds))
    tone = (0.3 * np.sin(2 * np.pi * 440 * t / SAMPLE_RATE)).astype(np.float32)
    sf.write(str(path), np.repeat(tone[:, None], channels, axis=1), SAMPLE_RATE, subtype="PCM_16")


class FakeSession:
    """Stands in for the daemon and the controls: "records" a tone to the
    path the CLI asks for, and ends with the given action."""

    def __init__(self, monkeypatch, action="stop", ffmpeg="/usr/bin/ffmpeg"):
        self.started_with = None
        self.converted = []
        monkeypatch.setattr(cli, "_send_start", self.send_start)
        monkeypatch.setattr(cli, "_send_command", self.send_command)
        monkeypatch.setattr(cli.terminal, "enable_vt_output", lambda: True)
        monkeypatch.setattr(cli.terminal, "raw_input_supported", lambda: True)
        monkeypatch.setattr(cli.sys.stderr, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(cli, "_run_control_panel", lambda: action)
        monkeypatch.setattr(cli.shutil, "which", lambda name: ffmpeg if name == "ffmpeg" else None)

    def send_start(self, path, mode, device_name, monitor):
        self.started_with = path
        write_tone(path)
        return {"ok": True}

    def send_command(self, cmd):
        assert cmd == "stop"
        return {"ok": True, "filename": str(self.started_with), "duration": 2.0}


@pytest.fixture
def fake_conversion(monkeypatch):
    """Replace ffmpeg with a copy, recording what was converted."""
    calls = []

    def convert(wav_path, mp3_path):
        calls.append((wav_path, mp3_path))
        shutil.copy(wav_path, mp3_path)
        return None

    monkeypatch.setattr(cli, "_convert_to_mp3", convert)
    return calls


def test_mp3_records_hidden_wav_then_converts_and_removes_it(tmp_path, monkeypatch, capsys, fake_conversion):
    session = FakeSession(monkeypatch)
    cli.do_record(str(tmp_path / "take1.mp3"), "input")

    assert session.started_with == tmp_path / ".take1.recording.wav"
    assert fake_conversion == [(tmp_path / ".take1.recording.wav", tmp_path / "take1.mp3")]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["take1.mp3"]
    assert f"Saved to {tmp_path / 'take1.mp3'} (2.0s)" in capsys.readouterr().out


def test_mp3_recording_leaves_an_existing_wav_alone(tmp_path, monkeypatch, fake_conversion):
    (tmp_path / "take1.wav").write_bytes(b"the user's own file")
    FakeSession(monkeypatch)
    cli.do_record(str(tmp_path / "take1.mp3"), "input")
    assert (tmp_path / "take1.wav").read_bytes() == b"the user's own file"
    assert (tmp_path / "take1.mp3").exists()


def test_failed_conversion_keeps_the_recording_as_a_wav(tmp_path, monkeypatch):
    FakeSession(monkeypatch)
    monkeypatch.setattr(cli, "_convert_to_mp3", lambda wav, mp3: "Unknown encoder 'libmp3lame'")
    with pytest.raises(SystemExit) as exc:
        cli.do_record(str(tmp_path / "take1.mp3"), "input")
    assert "Unknown encoder 'libmp3lame'" in str(exc.value)
    assert str(tmp_path / "take1.wav") in str(exc.value)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["take1.wav"]
    assert sf.info(str(tmp_path / "take1.wav")).duration == pytest.approx(2.0)


def test_failed_conversion_never_overwrites_an_existing_wav(tmp_path, monkeypatch):
    (tmp_path / "take1.wav").write_bytes(b"the user's own file")
    FakeSession(monkeypatch)
    monkeypatch.setattr(cli, "_convert_to_mp3", lambda wav, mp3: "boom")
    with pytest.raises(SystemExit) as exc:
        cli.do_record(str(tmp_path / "take1.mp3"), "input")
    assert (tmp_path / "take1.wav").read_bytes() == b"the user's own file"
    assert (tmp_path / ".take1.recording.wav").exists()
    assert str(tmp_path / ".take1.recording.wav") in str(exc.value)


def test_delete_discards_without_converting(tmp_path, monkeypatch, capsys, fake_conversion):
    FakeSession(monkeypatch, action="delete")
    cli.do_record(str(tmp_path / "take1.mp3"), "input")
    assert fake_conversion == []
    assert list(tmp_path.iterdir()) == []
    assert f"Recording deleted: {tmp_path / 'take1.mp3'}" in capsys.readouterr().out


def test_mp3_without_ffmpeg_refuses_before_recording(tmp_path, monkeypatch):
    session = FakeSession(monkeypatch, ffmpeg=None)
    with pytest.raises(SystemExit) as exc:
        cli.do_record(str(tmp_path / "take1.mp3"), "input")
    assert "needs ffmpeg" in str(exc.value)
    assert session.started_with is None


def test_wav_recordings_are_not_converted(tmp_path, monkeypatch, fake_conversion):
    session = FakeSession(monkeypatch)
    cli.do_record(str(tmp_path / "take1"), "input")
    assert session.started_with == tmp_path / "take1.wav"
    assert fake_conversion == []


# --- with the real ffmpeg ---


def _ffmpeg_can_encode_mp3():
    try:
        result = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    except OSError:
        return False
    return result.returncode == 0 and "libmp3lame" in result.stdout


needs_ffmpeg = pytest.mark.skipif(not _ffmpeg_can_encode_mp3(), reason="needs a working ffmpeg with libmp3lame")

MPEG1_LAYER3_KBPS = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]


def mp3_frame_bitrates(path):
    """Bitrates (kbps) declared by the audio frame headers in an MP3 file."""
    data = path.read_bytes()
    pos = 0
    if data[:3] == b"ID3":  # skip an ID3v2 tag
        pos = 10 + int.from_bytes(bytes(b & 0x7F for b in data[6:10]), "big")
    bitrates = set()
    while pos + 4 <= len(data):
        if data[pos] != 0xFF or data[pos + 1] & 0xE0 != 0xE0:
            pos += 1
            continue
        kbps = MPEG1_LAYER3_KBPS[data[pos + 2] >> 4]
        sample_rate = [44100, 48000, 32000][(data[pos + 2] >> 2) & 3]
        bitrates.add(kbps)
        pos += 144 * kbps * 1000 // sample_rate + ((data[pos + 2] >> 1) & 1)
    return bitrates


@needs_ffmpeg
@pytest.mark.parametrize("channels", [1, 2])
def test_conversion_makes_a_clean_320kbps_mp3(tmp_path, channels):
    wav, mp3 = tmp_path / "in.wav", tmp_path / "out.mp3"
    write_tone(wav, channels=channels)
    assert cli._convert_to_mp3(wav, mp3) is None

    # The Xing/Info header frame can declare a different bitrate; every
    # audio frame must be 320.
    assert mp3_frame_bitrates(mp3) - {0} <= {320} and 320 in mp3_frame_bitrates(mp3)
    info = sf.info(str(mp3))
    assert (info.format, info.channels) == ("MP3", channels)
    assert info.duration == pytest.approx(2.0, abs=0.1)
    decoded, _ = sf.read(str(mp3), always_2d=True)
    original, _ = sf.read(str(wav), always_2d=True)
    steepest = np.abs(np.diff(original, axis=0)).max()
    assert np.abs(np.diff(decoded, axis=0)).max() < 2 * steepest  # no clicks


@needs_ffmpeg
def test_conversion_reports_ffmpeg_errors(tmp_path):
    error = cli._convert_to_mp3(tmp_path / "missing.wav", tmp_path / "out.mp3")
    assert isinstance(error, str) and error  # ffmpeg's own message; wording varies by build
    assert not (tmp_path / "out.mp3").exists()
