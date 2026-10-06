"""recordit - lightning-fast background WAV recorder.

Usage:
    recordit take1            # record from the default input device
    recordit take1 --output   # record the system's current output instead
                               # (requires a loopback-capable output device,
                               # e.g. BlackHole)
    recordit take1 --device   # list input/output devices and pick one
    recordit take1 --output --monitor   # also play the capture live to
                               # your speakers (needs a separate real
                               # output device from the one being captured)

    While recording, click the pause/stop/delete buttons under the
    waveform, or press space to pause/resume, s to stop and save, or x to
    stop and discard the file. (Where clicks aren't supported, e.g. on
    Windows, type stop or delete and press Enter instead.)

The file is saved into the directory you ran the command from. A small
daemon keeps the audio devices open in the background across runs, so
after the first use, starting a recording is near-instant.
"""

import argparse
import json
import math
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

try:
    import termios
    import tty
except ImportError:  # Windows: falls back to typed stop/delete
    termios = tty = None

from .common import HOST, LOG_FILE, PORT_FILE, STATE_DIR

DAEMON_SPAWN_TIMEOUT = 20  # seconds to wait for a freshly spawned daemon to open the device
QUICK_CONNECT_TIMEOUT = 0.3  # seconds to detect whether a daemon is already running
STOP_WORD = "stop"
DELETE_WORD = "delete"

WAVE_POLL_INTERVAL = 0.1  # seconds per waveform column
WAVE_CHARS = "▁▂▃▄▅▆▇█"
WAVE_FLOOR_DB = -60.0  # peaks at or below this show as the lowest bar
FULL_SCALE = 32768  # int16 full scale

PAUSE_ICON = "⏸"
PLAY_ICON = "▶"
STOP_ICON = "⏹"
DELETE_ICON = "✕"
# xterm mouse reporting: button press/release events, SGR-encoded coordinates.
MOUSE_ON = "\x1b[?1000h\x1b[?1006h"
MOUSE_OFF = "\x1b[?1006l\x1b[?1000l"


def _normalize_wav_name(filename):
    if not filename.lower().endswith(".wav"):
        filename += ".wav"
    return filename


def _request(sock, payload, recv_timeout):
    sock.sendall(json.dumps(payload).encode())
    sock.settimeout(recv_timeout)
    return json.loads(sock.recv(65536).decode())


def _try_connect(timeout):
    try:
        port = int(PORT_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((HOST, port))
    except OSError:
        sock.close()
        return None
    return sock


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
    sock = _try_connect(QUICK_CONNECT_TIMEOUT)
    if sock is not None:
        return sock

    _spawn_daemon()
    deadline = time.time() + DAEMON_SPAWN_TIMEOUT
    while time.time() < deadline:
        sock = _try_connect(0.2)
        if sock is not None:
            return sock
        time.sleep(0.1)

    tail = ""
    try:
        tail = LOG_FILE.read_text()[-500:]
    except FileNotFoundError:
        pass
    sys.exit(f"Failed to start the recordit daemon.\n{tail}")


def _send_start(abspath, mode, device_name=None, monitor=False):
    sock = _connect_spawning_if_needed()
    payload = {"cmd": "start", "filename": str(abspath), "mode": mode, "monitor": monitor}
    if device_name is not None:
        payload["device_name"] = device_name
    try:
        # Generous timeout: covers the rare case where this call just spawned
        # the daemon and it's still finishing device setup.
        return _request(sock, payload, recv_timeout=DAEMON_SPAWN_TIMEOUT)
    finally:
        sock.close()


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
    sock = _try_connect(1)
    if sock is None:
        return {"ok": False, "error": "No active recording."}
    try:
        return _request(sock, {"cmd": cmd}, recv_timeout=5)
    finally:
        sock.close()


def _format_elapsed(seconds):
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes:02d}:{secs:02d}"


def _fetch_levels():
    sock = _try_connect(0.2)
    if sock is None:
        return None
    try:
        reply = _request(sock, {"cmd": "levels"}, recv_timeout=0.5)
    except (OSError, ValueError):
        return None
    finally:
        sock.close()
    return reply.get("levels") if reply.get("ok") else None


def _level_char(peak):
    if peak <= 0:
        return WAVE_CHARS[0]
    db = 20 * math.log10(peak / FULL_SCALE)
    fraction = (db - WAVE_FLOOR_DB) / -WAVE_FLOOR_DB
    index = round(fraction * (len(WAVE_CHARS) - 1))
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
        self.position_wanted = True
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
        with self.lock:
            if self.stopped.is_set():
                return
            frame = f"\x1b[1A\r{prefix}{wave}\x1b[K\x1b[1B\r{buttons}\x1b[K"
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
        if final == b"M" and int(button) == 0:  # left button press
            action = panel.button_at(int(col), int(row))
            if action == "pause":
                _toggle_pause(panel)
            elif action is not None:
                return action
    elif kind == "position":
        panel.button_row = int(value[0])
    return None


def _read_controls(panel):
    fd = sys.stdin.fileno()
    buf = b""
    while True:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if not ready:
            if buf == b"\x1b":
                buf = b""  # a lone Escape keypress, not the start of a sequence
            continue
        data = os.read(fd, 1024)
        if not data:
            return "stop"
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
    fd = sys.stdin.fileno()
    saved_tty = termios.tcgetattr(fd)
    saved_winch = signal.getsignal(signal.SIGWINCH)

    def on_resize(*_args):
        panel.position_wanted = True

    tty.setcbreak(fd)
    signal.signal(signal.SIGWINCH, on_resize)
    # Reserve the waveform line; enable SGR mouse reporting; hide the cursor.
    sys.stderr.write(f"\n{MOUSE_ON}\x1b[?25l")
    sys.stderr.flush()
    display = threading.Thread(target=panel.run_display, daemon=True)
    display.start()
    try:
        try:
            return _read_controls(panel)
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
        signal.signal(signal.SIGWINCH, saved_winch)
        termios.tcsetattr(fd, termios.TCSADRAIN, saved_tty)


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


def _run_typed_controls():
    """Fallback when clicks can't be read (e.g. Windows): wait for a typed
    stop/delete; return the action."""
    stop_display = threading.Event()
    draw_lock = threading.Lock()
    show_waveform = sys.stderr.isatty()
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


def _controls_clickable():
    return termios is not None and sys.stdin.isatty() and sys.stderr.isatty()


def do_record(filename, mode, device_name=None, monitor=False):
    if not filename:
        sys.exit("recordit requires a filename, e.g. `recordit take1`")

    abspath = Path(_normalize_wav_name(filename)).expanduser().resolve()

    reply = _send_start(abspath, mode, device_name, monitor)
    if not reply.get("ok"):
        sys.exit(f"Failed to start recording: {reply.get('error')}")

    if _controls_clickable():
        action = _run_control_panel()
    else:
        # Without clickable buttons, nothing else on screen says how to stop.
        print(f"Type {STOP_WORD} and press Enter to stop (Ctrl+C also stops), or {DELETE_WORD} to stop and discard.")
        action = _run_typed_controls()

    reply = _send_command("stop")
    if not reply.get("ok"):
        sys.exit(reply.get("error") or "Failed to stop recording.")

    if action == "delete":
        Path(reply["filename"]).unlink(missing_ok=True)
        print(f"Recording deleted: {reply['filename']}")
    else:
        print(f"Saved to {reply['filename']} ({reply['duration']:.1f}s)")


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
            f"Where clicks aren't supported (e.g. Windows), type {STOP_WORD} or {DELETE_WORD}\n"
            "and press Enter instead.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("name", nargs="?", help="output filename; recording starts immediately")
    parser.add_argument(
        "--output",
        action="store_true",
        help="record the system's current output instead of the input device "
        "(requires a loopback-capable output device, e.g. BlackHole)",
    )
    parser.add_argument(
        "--device",
        action="store_true",
        help="list available input/output devices and choose one to record from",
    )
    parser.add_argument(
        "--monitor",
        action="store_true",
        help="also play the captured audio live to the system's current output device "
        "(only valid with --output or --device, e.g. to hear a BlackHole loopback capture)",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.name:
        parser.print_help()
        sys.exit(1)

    if args.output and args.device:
        sys.exit("Use either --output or --device, not both.")

    if args.monitor and not (args.output or args.device):
        sys.exit("--monitor only applies to --output or --device recordings.")

    if args.device:
        device_name = prompt_for_device()
        do_record(args.name, "device", device_name, args.monitor)
    else:
        do_record(args.name, "output" if args.output else "input", monitor=args.monitor)


if __name__ == "__main__":
    main()
