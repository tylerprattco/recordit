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

    Type stop and press Enter to stop and save (Ctrl+C / Ctrl+D also stop),
    or type delete to stop and discard the file.

The file is saved into the directory you ran the command from. A small
daemon keeps the audio devices open in the background across runs, so
after the first use, starting a recording is near-instant.
"""

import argparse
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from .common import LOG_FILE, SOCK_PATH, STATE_DIR

DAEMON_SPAWN_TIMEOUT = 20  # seconds to wait for a freshly spawned daemon to open the device
QUICK_CONNECT_TIMEOUT = 0.3  # seconds to detect whether a daemon is already running
STOP_WORD = "stop"
DELETE_WORD = "delete"


def _normalize_wav_name(filename):
    if not filename.lower().endswith(".wav"):
        filename += ".wav"
    return filename


def _request(sock, payload, recv_timeout):
    sock.sendall(json.dumps(payload).encode())
    sock.settimeout(recv_timeout)
    return json.loads(sock.recv(65536).decode())


def _try_connect(timeout):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(SOCK_PATH))
    except OSError:
        sock.close()
        return None
    return sock


def _spawn_daemon():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "recordit.daemon"]
    with open(LOG_FILE, "a") as log:
        subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)


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


def _send_stop():
    sock = _try_connect(1)
    if sock is None:
        return {"ok": False, "error": "No active recording."}
    try:
        return _request(sock, {"cmd": "stop"}, recv_timeout=5)
    finally:
        sock.close()


def _format_elapsed(seconds):
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes:02d}:{secs:02d}"


def _run_progress_display(start_time, stop_display):
    while not stop_display.is_set():
        elapsed = _format_elapsed(time.time() - start_time)
        sys.stderr.write(f"\r● recording  {elapsed}  ")
        sys.stderr.flush()
        stop_display.wait(1)
    sys.stderr.write("\r" + " " * 24 + "\r")
    sys.stderr.flush()


def do_record(filename, mode, device_name=None, monitor=False):
    if not filename:
        sys.exit("recordit requires a filename, e.g. `recordit take1`")

    abspath = Path(_normalize_wav_name(filename)).expanduser().resolve()

    reply = _send_start(abspath, mode, device_name, monitor)
    if not reply.get("ok"):
        sys.exit(f"Failed to start recording: {reply.get('error')}")

    if mode == "output":
        source_label = "system output"
    elif mode == "device":
        source_label = device_name
    else:
        source_label = "input device"
    monitor_note = " (monitoring to speakers)" if monitor else ""
    print(f"Recording from {source_label}{monitor_note} -> {abspath}")
    print(f"Type {STOP_WORD} and press Enter to stop (Ctrl+C also stops), or {DELETE_WORD} to stop and discard.")

    stop_display = threading.Event()
    progress = threading.Thread(target=_run_progress_display, args=(time.time(), stop_display), daemon=True)
    progress.start()

    action = "stop"
    try:
        while True:
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            stripped = line.strip()
            if stripped == STOP_WORD:
                break
            if stripped == DELETE_WORD:
                action = "delete"
                break
    finally:
        stop_display.set()
        progress.join(timeout=2)

    reply = _send_stop()
    if not reply.get("ok"):
        sys.exit(reply.get("error") or "Failed to stop recording.")

    if action == "delete":
        Path(reply["filename"]).unlink(missing_ok=True)
        print(f"Recording deleted: {reply['filename']}")
    else:
        print(f"Recording stopped. Saved to {reply['filename']} ({reply['duration']:.1f}s)")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="recordit",
        description="Record audio to a WAV file.",
        epilog=(
            "While recording:\n"
            f"  {STOP_WORD}     stop and save\n"
            f"  {DELETE_WORD}   stop and discard the file\n"
            "  Ctrl+C / Ctrl+D   also stop and save\n"
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
