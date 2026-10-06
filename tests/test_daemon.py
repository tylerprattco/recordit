import json
import socket
import threading

import numpy as np
import pytest
import soundfile as sf

from recordit import daemon
from recordit.common import PAUSE_FADE_FRAMES, SAMPLE_RATE


def sine(seconds, freq=1000, amplitude=16384):
    t = np.arange(int(SAMPLE_RATE * seconds))
    return (amplitude * np.sin(2 * np.pi * freq * t / SAMPLE_RATE)).astype(np.int16)[:, None]


def max_jump(samples):
    return int(np.abs(np.diff(samples.astype(np.int64), axis=0)).max())


class Feeder:
    """Feeds a signal to a Recorder's input callback block by block, the way
    the audio stream would."""

    def __init__(self, recorder, signal, blocksize):
        self.recorder, self.signal, self.blocksize, self.pos = recorder, signal, blocksize, 0

    def blocks(self, count, blocksize=None):
        blocksize = blocksize or self.blocksize
        for _ in range(count):
            block = self.signal[self.pos : self.pos + blocksize]
            self.recorder.input_callback(block, len(block), None, None)
            self.pos += blocksize


@pytest.fixture
def recorder():
    recorder = daemon.Recorder()
    recorder.input_channels = 1  # as if the input stream had opened mono
    return recorder


def start(recorder, path):
    ok, err = recorder.start(str(path))
    assert ok, err


@pytest.mark.parametrize("blocksize", [2048, 256])  # normal, and --monitor's smaller blocks
def test_pause_and_resume_fade_without_clicks(recorder, tmp_path, blocksize):
    signal = sine(3)
    feed = Feeder(recorder, signal, blocksize)
    start(recorder, tmp_path / "take.wav")
    feed.blocks(10)
    recorder.set_paused(True)
    feed.blocks(10)
    recorder.set_paused(False)
    feed.blocks(10)
    recorder.set_paused(True)
    feed.blocks(1, blocksize=100)  # resume partway through a fade-out
    recorder.set_paused(False)
    feed.blocks(5)
    recorder.stop()

    recorded, _ = sf.read(str(tmp_path / "take.wav"), dtype="int16", always_2d=True)
    # No sample-to-sample step is bigger than the sine's own steepest one;
    # a hard cut at a pause would jump by up to the full amplitude.
    assert max_jump(recorded) <= max_jump(signal)


def test_duration_excludes_paused_time(recorder, tmp_path):
    feed = Feeder(recorder, sine(3), 2048)
    start(recorder, tmp_path / "take.wav")
    feed.blocks(20)
    recorder.set_paused(True)
    feed.blocks(20)
    recorder.set_paused(False)
    feed.blocks(20)
    (_, duration), err = recorder.stop()
    assert err is None
    # 40 blocks recorded, plus the fade-out (whose final, zero-gain frame is
    # dropped as part of the pause).
    assert duration == pytest.approx((40 * 2048 + PAUSE_FADE_FRAMES - 1) / SAMPLE_RATE, abs=0.1 / SAMPLE_RATE)
    assert sf.info(str(tmp_path / "take.wav")).frames == round(duration * SAMPLE_RATE)


def test_levels_track_block_peaks(recorder, tmp_path):
    start(recorder, tmp_path / "take.wav")
    recorder.input_callback(np.array([[-32768], [5]], dtype=np.int16), 2, None, None)
    recorder.input_callback(np.array([[10], [-20]], dtype=np.int16), 2, None, None)
    # -32768 must not overflow (abs() of it in int16 is still negative).
    assert recorder.drain_levels() == [32768, 20]
    assert recorder.drain_levels() == []
    recorder.stop()
    assert recorder.drain_levels() is None


def test_no_levels_while_paused(recorder, tmp_path):
    feed = Feeder(recorder, sine(1), 2048)
    start(recorder, tmp_path / "take.wav")
    recorder.set_paused(True)
    feed.blocks(1)  # carries the fade-out
    recorder.drain_levels()
    feed.blocks(5)
    assert recorder.drain_levels() == []
    recorder.stop()


def test_pause_without_recording_is_an_error(recorder):
    assert recorder.set_paused(True) == "Not recording"


def test_second_start_is_refused(recorder, tmp_path):
    start(recorder, tmp_path / "a.wav")
    assert recorder.start(str(tmp_path / "b.wav")) == (False, "Already recording")
    recorder.stop()


TOKEN = "a" * 32


def ask(recorder, payload):
    """Send one request through daemon._handle_client and return the reply."""
    client, server = socket.socketpair()
    handler = threading.Thread(target=daemon._handle_client, args=(server, recorder, TOKEN))
    handler.start()
    with client:
        client.sendall(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        reply = json.loads(client.recv(65536).decode())
    handler.join(timeout=5)
    return reply


@pytest.mark.parametrize("token", [None, "", "b" * 32, TOKEN[:-1], 12345])
def test_requests_without_the_token_are_refused(recorder, token):
    payload = {"cmd": "ping"}
    if token is not None:
        payload["token"] = token
    assert ask(recorder, payload) == {"ok": False, "error": "not authorized"}


def test_request_with_the_token_is_served(recorder):
    assert ask(recorder, {"cmd": "ping", "token": TOKEN}) == {"ok": True}


def test_unauthorized_start_writes_nothing(recorder, tmp_path):
    target = tmp_path / "planted.wav"
    ask(recorder, {"cmd": "start", "filename": str(target), "token": "wrong"})
    assert not target.exists()
    assert recorder.is_idle()


@pytest.mark.parametrize("payload", [b"not json", b"[1, 2]", b"\xff\xfe"])
def test_malformed_requests_are_rejected(recorder, payload):
    assert ask(recorder, payload) == {"ok": False, "error": "bad request"}
