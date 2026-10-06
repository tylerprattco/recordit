"""recordit - lightning-fast background WAV recorder.

Usage:
    recordit                  # record to a timestamped file, e.g.
                               # "recordit 2026-10-06 at 12.28.02 PM.wav"
    recordit take1            # record from the default input device
    recordit take1 --output   # record the system's current output instead
                               # (requires a loopback-capable output device,
                               # e.g. BlackHole)
    recordit take1 --device   # list input/output devices and pick one
    recordit take1 --showfile # when saved, open the file browser to the
                               # recording's folder
    recordit take1 --output --monitor   # also play the capture live to
                               # your speakers (needs a separate real
                               # output device from the one being captured)

    Each flag also has a short form: -o, -d, -m, -s (--output, --device,
    --monitor, --showfile).

    While recording, click the pause/stop/delete buttons under the
    waveform, or press space to pause/resume, s to stop and save, or x to
    stop and discard the file. (If input is piped rather than typed, use
    stop or delete + Enter instead.)

The file is saved into the directory you ran the command from. A small
daemon keeps the audio devices open in the background across runs, so
after the first use, starting a recording is near-instant.
"""

import argparse
import json
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from . import terminal
from .common import HOST, LOG_FILE, STATE_DIR, read_port_file

DAEMON_SPAWN_TIMEOUT = 20  # seconds to wait for a freshly spawned daemon to open the device
QUICK_CONNECT_TIMEOUT = 0.3  # seconds to detect whether a daemon is already running
STOP_WORD = "stop"
DELETE_WORD = "delete"
MP3_BITRATE = "320k"

WAVE_POLL_INTERVAL = 0.1  # seconds per waveform column
WAVE_CHARS = "▁▂▃▄▅▆▇█"
FULL_SCALE = 32768  # int16 full scale

PAUSE_ICON = "⏸"
PLAY_ICON = "▶"
STOP_ICON = "⏹"
DELETE_ICON = "✕"
# xterm mouse reporting: button press/release events, SGR-encoded coordinates.
MOUSE_ON = "\x1b[?1000h\x1b[?1006h"
MOUSE_OFF = "\x1b[?1006l\x1b[?1000l"


def _default_filename():
    """e.g. "recordit 2026-10-06 at 12.28.02 PM", in the style of macOS
    screenshots. Built by hand since strftime's unpadded-hour flag isn't
    portable to Windows."""
    now = datetime.now()
    hour = now.hour % 12 or 12
    return f"recordit {now:%Y-%m-%d} at {hour}.{now:%M.%S %p}"


def _normalize_filename(filename):
    """Keep a .wav or .mp3 extension; otherwise record a WAV."""
    if not filename.lower().endswith((".wav", ".mp3")):
        filename += ".wav"
    return filename


def _temporary_wav_path(mp3_path):
    """Where to record before converting to mp3_path: hidden, and named so
    it can't overwrite the user's own take1.wav."""
    return mp3_path.with_name(f".{mp3_path.stem}.recording.wav")


def _convert_to_mp3(wav_path, mp3_path):
    """Encode wav_path to a 320 kbps MP3 with ffmpeg; return None on success,
    or an error message."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(wav_path), "-codec:a", "libmp3lame", "-b:a", MP3_BITRATE, str(mp3_path),
    ]  # fmt: skip
    try:
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except KeyboardInterrupt:
        return "cancelled"
    except OSError as exc:
        return str(exc)
    if result.returncode != 0:
        lines = result.stderr.strip().splitlines()
        return lines[0].strip() if lines else f"ffmpeg exited with status {result.returncode}"
    return None


def _keep_wav(wav_path, mp3_path):
    """After a failed conversion, give the recording a visible name (take1.wav,
    unless that's taken) and return where it is."""
    visible = mp3_path.with_suffix(".wav")
    if visible.exists():
        return wav_path
    wav_path.rename(visible)
    return visible


def _show_in_file_browser(path):
    """Open the default file browser on path's folder, selecting the file where
    the platform supports it; return None on success, or an error message."""
    if sys.platform == "darwin":
        cmd = ["open", "-R", str(path)]
    elif sys.platform == "win32":
        cmd = ["explorer", f"/select,{path}"]
    else:
        cmd = ["xdg-open", str(path.parent)]
    try:
        # explorer exits nonzero even on success, so only launch failures count.
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=sys.platform != "win32")
    except (OSError, subprocess.CalledProcessError) as exc:
        return str(exc)
    return None


def _request(conn, payload, recv_timeout):
    sock, token = conn
    sock.sendall(json.dumps(dict(payload, token=token)).encode())
    sock.settimeout(recv_timeout)
    return json.loads(sock.recv(65536).decode())


def _try_connect(timeout):
    """Connect to the running daemon; return (socket, token) or None."""
    port_info = read_port_file()
    if port_info is None:
        return None
    port, token = port_info
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((HOST, port))
    except OSError:
        sock.close()
        return None
    return sock, token


def _spawn_daemon():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "recordit.daemon"]
    popen_kwargs = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        popen_kwargs["start_new_session"] = True
    with open(LOG_FILE, "a") as log:
        subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL, **popen_kwargs)


def _connect_spawning_if_needed():
    conn = _try_connect(QUICK_CONNECT_TIMEOUT)
    if conn is not None:
        return conn

    _spawn_daemon()
    deadline = time.time() + DAEMON_SPAWN_TIMEOUT
    while time.time() < deadline:
        conn = _try_connect(0.2)
        if conn is not None:
            return conn
        time.sleep(0.1)

    tail = ""
    try:
        tail = LOG_FILE.read_text()[-500:]
    except FileNotFoundError:
        pass
    sys.exit(f"Failed to start the recordit daemon.\n{tail}")


def _send_start(abspath, mode, device_name=None, monitor=False):
    conn = _connect_spawning_if_needed()
    payload = {"cmd": "start", "filename": str(abspath), "mode": mode, "monitor": monitor}
    if device_name is not None:
        payload["device_name"] = device_name
    try:
        # Generous timeout: covers the rare case where this call just spawned
        # the daemon and it's still finishing device setup.
        return _request(conn, payload, recv_timeout=DAEMON_SPAWN_TIMEOUT)
    finally:
        conn[0].close()


def prompt_for_device():
    """List input/output devices and prompt the user to pick one by number.

    Runs in this (top-level) process rather than the daemon, so it always
    reflects the live device list.
    """
    import sounddevice as sd

    devices = sd.query_devices()
    numbered = []

    print("Input devices:")
    for d in devices:
        if d["max_input_channels"] > 0:
            numbered.append(d)
            print(f"  {len(numbered)}) {d['name']}")

    print("\nOutput devices:")
    for d in devices:
        if d["max_output_channels"] > 0:
            numbered.append(d)
            print(f"  {len(numbered)}) {d['name']}")

    if not numbered:
        sys.exit("No audio devices found.")

    try:
        choice = input("\nSelect a device number: ").strip()
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nCancelled.")

    if not choice.isdigit() or not (1 <= int(choice) <= len(numbered)):
        sys.exit(f"Invalid selection: {choice!r}")

    return numbered[int(choice) - 1]["name"]


def _send_command(cmd):
    conn = _try_connect(1)
    if conn is None:
        return {"ok": False, "error": "No active recording."}
    try:
        return _request(conn, {"cmd": cmd}, recv_timeout=5)
    finally:
        conn[0].close()


def _format_elapsed(seconds):
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes:02d}:{secs:02d}"


def _fetch_levels():
    conn = _try_connect(0.2)
    if conn is None:
        return None
    try:
        reply = _request(conn, {"cmd": "levels"}, recv_timeout=0.5)
    except (OSError, ValueError):
        return None
    finally:
        conn[0].close()
    return reply.get("levels") if reply.get("ok") else None


def _level_char(peak):
    # Linear in amplitude, like a DAW waveform, so dynamics read clearly:
    # -20 dBFS is a low bar and only peaks near full scale reach the top.
    index = round(peak / FULL_SCALE * (len(WAVE_CHARS) - 1))
    return WAVE_CHARS[min(max(index, 0), len(WAVE_CHARS) - 1)]


def _append_levels(columns):
    """Add one waveform column: the loudest block the daemon captured since
    the last poll, so the scroll speed doesn't depend on the stream blocksize."""
    levels = _fetch_levels()
    if levels:
        columns.append(_level_char(max(levels)))


def _waveform_text(columns, prefix):
    wave_width = max(shutil.get_terminal_size().columns - len(prefix) - 1, 0)
    while len(columns) > wave_width:
        columns.popleft()
    return "".join(columns)


def _button_layout(paused):
    """Return the button row text and each button's (icon column, action).

    Columns are 1-based, as mouse reports use. Clicks are matched to the
    nearest icon, so terminals that draw the icons double-width still hit
    the right button.
    """
    buttons = [("pause", PLAY_ICON if paused else PAUSE_ICON), ("stop", STOP_ICON), ("delete", DELETE_ICON)]
    text = "  "
    targets = []
    for action, icon in buttons:
        if targets:
            text += "   "
        targets.append((len(text) + 3, action))
        text += f"[ {icon} ]"
    return text, targets


class _ControlPanel:
    """The timer/waveform line plus a row of clickable pause/stop/delete
    buttons below it, with the (hidden) cursor parked on the button row."""

    def __init__(self):
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.paused = False
        self.recorded_before_run = 0.0
        self.run_started = time.time()
        self.button_row = None  # screen row, learned from a cursor position report
        self.pressed_button = None
        self.position_wanted = True
        self.terminal_size = None
        self.columns = deque()

    def elapsed(self):
        if self.paused:
            return self.recorded_before_run
        return self.recorded_before_run + time.time() - self.run_started

    def set_paused(self, paused):
        with self.lock:
            if paused:
                self.recorded_before_run = self.elapsed()
            else:
                self.run_started = time.time()
            self.paused = paused

    def button_at(self, col, row):
        if row != self.button_row:
            return None
        _, targets = _button_layout(self.paused)
        if not targets[0][0] - 3 <= col <= targets[-1][0] + 5:
            return None
        return min(targets, key=lambda target: abs(target[0] - col))[1]

    def draw(self):
        label = "⏸ paused" if self.paused else "● recording"
        prefix = f"{label:<11}  {_format_elapsed(self.elapsed())}  "
        wave = _waveform_text(self.columns, prefix)
        buttons, _ = _button_layout(self.paused)
        size = shutil.get_terminal_size()
        with self.lock:
            if self.stopped.is_set():
                return
            frame = f"\x1b[1A\r{prefix}{wave}\x1b[K\x1b[1B\r{buttons}\x1b[K"
            if size != self.terminal_size:
                # A resize can move the button row (and there's no resize
                # signal on Windows), so re-learn its position.
                self.terminal_size = size
                self.position_wanted = True
            if self.position_wanted:
                # Ask the terminal where the button row is; the reply
                # arrives on stdin and is handled by _read_controls.
                frame += "\x1b[6n"
                self.position_wanted = False
            sys.stderr.write(frame)
            sys.stderr.flush()

    def run_display(self):
        while not self.stopped.is_set():
            if not self.paused:
                _append_levels(self.columns)
            self.draw()
            self.stopped.wait(WAVE_POLL_INTERVAL)


_MOUSE_REPORT = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
_POSITION_REPORT = re.compile(rb"\x1b\[(\d+);(\d+)R")
_OTHER_CSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")


def _parse_input(buf):
    """Split one event off the front of buf: (kind, value, rest), or None
    if buf holds only the start of an escape sequence."""
    if not buf.startswith(b"\x1b"):
        return "key", buf[:1], buf[1:]
    for pattern, kind in ((_MOUSE_REPORT, "mouse"), (_POSITION_REPORT, "position"), (_OTHER_CSI, "ignore")):
        match = pattern.match(buf)
        if match:
            return kind, match.groups(), buf[match.end():]
    if len(buf) < 2 or buf[1:2] == b"[":
        return None
    return "ignore", None, buf[2:]  # Alt+key and other two-byte escapes


def _toggle_pause(panel):
    paused = not panel.paused
    reply = _send_command("pause" if paused else "resume")
    if reply.get("ok"):
        panel.set_paused(paused)
        panel.draw()


def _handle_event(panel, kind, value):
    """Act on one input event; return "stop"/"delete" to end the recording."""
    if kind == "key":
        key = value.lower()
        if key in (b" ", b"p"):
            _toggle_pause(panel)
        elif key in (b"s", b"\x04"):
            return "stop"
        elif key == b"x":
            return "delete"
    elif kind == "mouse":
        button, col, row, final = value
        if int(button) != 0:  # left button only
            return None
        target = panel.button_at(int(col), int(row))
        if final == b"M":  # press
            panel.pressed_button = target
            return None
        # Act on release, like a normal button: it can be cancelled by
        # dragging off before letting go, and the release is consumed here
        # rather than reaching the terminal after mouse reporting is turned
        # off (iTerm2 treats a stray release as a click that selects the
        # whole command's output).
        pressed, panel.pressed_button = panel.pressed_button, None
        if target is None or target != pressed:
            return None
        if target == "pause":
            _toggle_pause(panel)
        else:
            return target
    elif kind == "position":
        panel.button_row = int(value[0])
    return None


def _read_controls(panel, raw_input):
    buf = b""
    while True:
        data = raw_input.read(0.2)
        if data is None:
            return "stop"
        if not data:
            if buf == b"\x1b":
                buf = b""  # a lone Escape keypress, not the start of a sequence
            continue
        buf += data
        while buf:
            parsed = _parse_input(buf)
            if parsed is None:
                break
            kind, value, buf = parsed
            action = _handle_event(panel, kind, value)
            if action:
                return action


def _run_control_panel():
    """Show the clickable controls until the user stops; return the action."""
    panel = _ControlPanel()
    with terminal.RawInput() as raw_input:
        # Reserve the waveform line; enable SGR mouse reporting; hide the cursor.
        sys.stderr.write(f"\n{MOUSE_ON}\x1b[?25l")
        sys.stderr.flush()
        display = threading.Thread(target=panel.run_display, daemon=True)
        display.start()
        try:
            try:
                return _read_controls(panel, raw_input)
            except KeyboardInterrupt:
                return "stop"
        finally:
            with panel.lock:
                panel.stopped.set()
            display.join(timeout=2)
            # Clear the button row (the final waveform stays above it) and
            # restore the terminal.
            sys.stderr.write(f"\r\x1b[K{MOUSE_OFF}\x1b[?25h")
            sys.stderr.flush()


def _run_waveform_display(start_time, stop_display, draw_lock):
    """Draw the timer plus a scrolling peak waveform on the line above the
    input line, leaving the cursor where the user types stop/delete."""
    columns = deque()
    while True:
        _append_levels(columns)
        prefix = f"● recording  {_format_elapsed(time.time() - start_time)}  "
        wave = _waveform_text(columns, prefix)
        with draw_lock:
            if stop_display.is_set():
                return
            # Save cursor, move up to the waveform line, redraw, restore.
            sys.stderr.write(f"\x1b7\x1b[1A\r{prefix}{wave}\x1b[K\x1b8")
            sys.stderr.flush()
        stop_display.wait(WAVE_POLL_INTERVAL)


def _run_progress_display(start_time, stop_display):
    while not stop_display.is_set():
        elapsed = _format_elapsed(time.time() - start_time)
        sys.stderr.write(f"\r● recording  {elapsed}  ")
        sys.stderr.flush()
        stop_display.wait(1)
    sys.stderr.write("\r" + " " * 24 + "\r")
    sys.stderr.flush()


def _run_typed_controls(show_waveform):
    """Fallback when keys/clicks can't be read one at a time (e.g. input is
    piped): wait for a typed stop/delete; return the action."""
    stop_display = threading.Event()
    draw_lock = threading.Lock()
    if show_waveform:
        # Reserve a line above the input line for the waveform.
        sys.stderr.write("\n")
        sys.stderr.flush()
        progress = threading.Thread(
            target=_run_waveform_display, args=(time.time(), stop_display, draw_lock), daemon=True
        )
    else:
        progress = threading.Thread(target=_run_progress_display, args=(time.time(), stop_display), daemon=True)
    progress.start()

    try:
        while True:
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                with draw_lock:
                    stop_display.set()
                print()
                return "stop"
            stripped = line.strip()
            if stripped in (STOP_WORD, DELETE_WORD):
                # Stop drawing before anything else, so the final waveform
                # stays in place above the entered command.
                with draw_lock:
                    stop_display.set()
                return "delete" if stripped == DELETE_WORD else "stop"
            if show_waveform:
                # Erase the unrecognized line so the input line stays
                # directly below the waveform.
                with draw_lock:
                    sys.stderr.write("\x1b[1A\x1b[2K")
                    sys.stderr.flush()
    finally:
        stop_display.set()
        progress.join(timeout=2)


def do_record(filename, mode, device_name=None, monitor=False, show_file=False):
    if not filename:
        filename = _default_filename()

    target = Path(_normalize_filename(filename)).expanduser().resolve()
    # MP3s are recorded as a WAV first, then converted with ffmpeg on stop.
    to_mp3 = target.suffix.lower() == ".mp3"
    if to_mp3 and shutil.which("ffmpeg") is None:
        sys.exit(
            "Recording an .mp3 needs ffmpeg, which wasn't found. Install it "
            "(e.g. `brew install ffmpeg`), or record a .wav instead."
        )
    record_path = _temporary_wav_path(target) if to_mp3 else target

    reply = _send_start(record_path, mode, device_name, monitor)
    if not reply.get("ok"):
        sys.exit(f"Failed to start recording: {reply.get('error')}")

    escapes_work = sys.stderr.isatty() and terminal.enable_vt_output()
    if escapes_work and terminal.raw_input_supported():
        action = _run_control_panel()
    else:
        # Without clickable buttons, nothing else on screen says how to stop.
        print(f"Type {STOP_WORD} and press Enter to stop (Ctrl+C also stops), or {DELETE_WORD} to stop and discard.")
        action = _run_typed_controls(show_waveform=escapes_work)

    reply = _send_command("stop")
    if not reply.get("ok"):
        sys.exit(reply.get("error") or "Failed to stop recording.")

    if action == "delete":
        record_path.unlink(missing_ok=True)
        print(f"Recording deleted: {target}")
        return

    if to_mp3:
        print("Converting to MP3...", end="", flush=True)
        error = _convert_to_mp3(record_path, target)
        print("\r\x1b[K" if sys.stdout.isatty() else "", end="")
        if error is not None:
            kept = _keep_wav(record_path, target)
            sys.exit(f"MP3 conversion failed ({error}). The recording was kept as {kept}")
        record_path.unlink(missing_ok=True)
    print(f"Saved to {target} ({reply['duration']:.1f}s)")
    if show_file:
        error = _show_in_file_browser(target)
        if error is not None:
            print(f"Couldn't open the file browser: {error}", file=sys.stderr)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="recordit",
        description="Record audio to a WAV file.",
        epilog=(
            "While recording, click the buttons under the waveform or press:\n"
            "  space / p   pause or resume\n"
            "  s           stop and save\n"
            "  x           stop and discard the file\n"
            "  Ctrl+C      also stops and saves\n"
            f"If input is piped rather than typed, use {STOP_WORD} or {DELETE_WORD} + Enter instead.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "name",
        nargs="?",
        help="output filename; recording starts immediately. Ending it in .mp3 "
        "records a WAV, then converts it to a 320 kbps MP3 with ffmpeg when you "
        'stop (default: a timestamp, e.g. "recordit 2026-10-06 at 12.28.02 PM.wav")',
    )
    parser.add_argument(
        "-o",
        "--output",
        action="store_true",
        help="record the system's current output instead of the input device "
        "(requires a loopback-capable output device, e.g. BlackHole)",
    )
    parser.add_argument(
        "-d",
        "--device",
        action="store_true",
        help="list available input/output devices and choose one to record from",
    )
    parser.add_argument(
        "-m",
        "--monitor",
        action="store_true",
        help="also play the captured audio live to the system's current output device "
        "(only valid with --output or --device, e.g. to hear a BlackHole loopback capture)",
    )
    parser.add_argument(
        "-s",
        "--showfile",
        action="store_true",
        help="when the recording is saved, open the default file browser to the folder it's in",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.output and args.device:
        sys.exit("Use either --output or --device, not both.")

    if args.monitor and not (args.output or args.device):
        sys.exit("--monitor only applies to --output or --device recordings.")

    if args.device:
        device_name = prompt_for_device()
        do_record(args.name, "device", device_name, args.monitor, args.showfile)
    else:
        do_record(args.name, "output" if args.output else "input", monitor=args.monitor, show_file=args.showfile)


if __name__ == "__main__":
    main()
