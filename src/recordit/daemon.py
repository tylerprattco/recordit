"""Background daemon that keeps the default input device open and idling,
and opens a secondary stream on demand for --output or --device recordings.

Keeping the input stream open across recordings avoids paying the slow
device/permission negotiation cost on every `recordit` invocation. The
daemon drops incoming audio when idle (negligible CPU) and only writes to
disk while a recording session is active.

Devices (and the default output) can change while the daemon is running
(e.g. switching to BlackHole, or plugging in an interface), but PortAudio
only enumerates devices once per process at startup -- querying it again
from within this same long-running process returns the same stale answer,
and spawning a fresh helper process to re-query hangs in some environments
(nested audio-subprocess spawning is unreliable). So each --output/--device
request instead reinitializes PortAudio in-process (forcing a fresh device
scan) right here in the daemon, then reopens both streams.
"""

import json
import queue
import signal
import socket
import sys
import threading
import time
from collections import deque

import sounddevice as sd
import soundfile as sf

from .common import (
    BLOCKSIZE,
    DTYPE,
    HOST,
    IDLE_TIMEOUT,
    MONITOR_BLOCKSIZE,
    MONITOR_LATENCY,
    PORT_FILE,
    SAMPLE_RATE,
    STATE_DIR,
    SUBTYPE,
)

NO_LOOPBACK_ERROR = (
    "No loopback-capable output device found. Set your system output to a virtual "
    "loopback device (e.g. BlackHole) -- or an aggregate/multi-output device that "
    "includes one -- then try again."
)


class Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.session = None
        self.last_activity = time.time()

        self.input_stream = None
        self.input_channels = 0

        # Holds whichever non-default device a recording currently targets:
        # the default-output loopback device for --output, or a specific
        # device by name for --device.
        self.secondary_stream = None
        self.secondary_channels = 0

        # Live playback of the captured audio to the system's current
        # output device, for --monitor.
        self.monitor_stream = None
        self.monitor_channels = 0

    def input_callback(self, indata, frames, time_info, status):
        session = self.session
        if session is not None and session["mode"] == "input":
            session["queue"].put(indata.copy())
            self._record_level(session, indata)

    def secondary_callback(self, indata, frames, time_info, status):
        session = self.session
        if session is not None and session["mode"] in ("output", "device"):
            session["queue"].put(indata.copy())
            self._record_level(session, indata)
            monitor_queue = session.get("monitor_queue")
            if monitor_queue is not None:
                monitor_queue.put(indata[:, : self.monitor_channels].copy())

    @staticmethod
    def _record_level(session, indata):
        # Peak magnitude of this block, for the client's live waveform.
        # Taken from max/min rather than abs() since abs(-32768) overflows int16.
        session["levels"].append(max(int(indata.max()), -int(indata.min())))

    def drain_levels(self):
        """Return the per-block peak levels captured since the last call."""
        session = self.session
        if session is None:
            return None
        levels = session["levels"]
        drained = []
        while True:
            try:
                drained.append(levels.popleft())
            except IndexError:
                return drained

    def open_input_stream(self):
        info = sd.query_devices(kind="input")
        channels = min(int(info["max_input_channels"]), 2) or 1
        stream = sd.InputStream(
            device=info["index"],
            samplerate=SAMPLE_RATE,
            channels=channels,
            dtype=DTYPE,
            blocksize=BLOCKSIZE,
            callback=self.input_callback,
        )
        stream.start()
        self.input_stream = stream
        self.input_channels = channels

    def _close_streams(self):
        if self.input_stream is not None:
            self.input_stream.stop()
            self.input_stream.close()
            self.input_stream = None
        if self.secondary_stream is not None:
            self.secondary_stream.stop()
            self.secondary_stream.close()
            self.secondary_stream = None
        if self.monitor_stream is not None:
            self.monitor_stream.stop()
            self.monitor_stream.close()
            self.monitor_stream = None

    def _resolve_target_device(self, mode, device_name):
        if mode == "output":
            return sd.query_devices(kind="output")
        matches = [d for d in sd.query_devices() if d["name"] == device_name]
        if not matches:
            raise LookupError(f"Device '{device_name}' not found.")
        return matches[0]

    def _ensure_secondary_stream(self, mode, device_name, want_monitor):
        """Refresh the device list and (re)open the input + secondary streams.

        PortAudio caches its device list for the lifetime of this process,
        so this forces a reinitialize to see devices/defaults that changed
        after the daemon started (e.g. the user switched to BlackHole, or
        plugged in an interface). Must be called while holding self.lock,
        with no active recording session.
        """
        self._close_streams()
        try:
            sd._terminate()
            sd._initialize()
        except Exception as exc:
            return 0, f"Could not refresh audio devices: {exc}"

        try:
            self.open_input_stream()
        except Exception as exc:
            return 0, f"Could not reopen the input device: {exc}"

        try:
            info = self._resolve_target_device(mode, device_name)
        except LookupError as exc:
            return 0, str(exc)
        except Exception as exc:
            return 0, f"Could not determine the target device: {exc}"

        channels = min(int(info["max_input_channels"]), 2)
        if channels == 0:
            if mode == "output":
                return 0, NO_LOOPBACK_ERROR
            return 0, f"'{info['name']}' has no input channels and can't be recorded from."

        stream = sd.InputStream(
            device=info["index"],
            samplerate=SAMPLE_RATE,
            channels=channels,
            dtype=DTYPE,
            blocksize=MONITOR_BLOCKSIZE if want_monitor else BLOCKSIZE,
            latency=MONITOR_LATENCY if want_monitor else None,
            callback=self.secondary_callback,
        )
        stream.start()
        self.secondary_stream = stream
        self.secondary_channels = channels

        if want_monitor:
            err = self._open_monitor_stream(info, channels)
            if err:
                self._close_streams()
                return 0, err

        return channels, None

    def _open_monitor_stream(self, capture_info, capture_channels):
        try:
            output_info = sd.query_devices(kind="output")
        except Exception as exc:
            return f"Could not determine the system's current output device for monitoring: {exc}"

        if output_info["index"] == capture_info["index"]:
            return (
                "Can't monitor to the same device you're recording from -- the system's "
                "current output device is the loopback device itself, so playing back to "
                "it would feed straight back into the recording. Pick a different device "
                "with --device, or set up a Multi-Output Device that also includes real "
                "speakers/headphones."
            )

        monitor_channels = min(capture_channels, int(output_info["max_output_channels"]))
        if monitor_channels == 0:
            return "The system's current output device has no output channels available for monitoring."

        try:
            monitor_stream = sd.OutputStream(
                device=output_info["index"],
                samplerate=SAMPLE_RATE,
                channels=monitor_channels,
                dtype=DTYPE,
                blocksize=MONITOR_BLOCKSIZE,
                latency=MONITOR_LATENCY,
            )
            monitor_stream.start()
        except Exception as exc:
            return f"Could not open the monitor output device: {exc}"

        self.monitor_stream = monitor_stream
        self.monitor_channels = monitor_channels
        return None

    def start(self, filename, mode="input", device_name=None, monitor=False):
        with self.lock:
            if self.session is not None:
                return False, "Already recording"

            if mode in ("output", "device"):
                channels, err = self._ensure_secondary_stream(mode, device_name, monitor)
                if err:
                    return False, err
            else:
                if monitor:
                    return False, "--monitor only applies to --output or --device recordings."
                channels = self.input_channels

            try:
                sound_file = sf.SoundFile(
                    filename, mode="w", samplerate=SAMPLE_RATE, channels=channels, subtype=SUBTYPE
                )
            except Exception as exc:
                return False, f"Could not open output file: {exc}"

            q = queue.Queue()
            stop_event = threading.Event()

            def writer():
                while True:
                    try:
                        chunk = q.get(timeout=0.5)
                    except queue.Empty:
                        if stop_event.is_set() and q.empty():
                            return
                        continue
                    sound_file.write(chunk)

            thread = threading.Thread(target=writer, daemon=True)
            thread.start()

            monitor_queue = None
            monitor_thread = None
            if monitor and self.monitor_stream is not None:
                monitor_queue = queue.Queue()

                def monitor_writer():
                    while True:
                        try:
                            chunk = monitor_queue.get(timeout=0.5)
                        except queue.Empty:
                            if stop_event.is_set() and monitor_queue.empty():
                                return
                            continue
                        try:
                            self.monitor_stream.write(chunk)
                        except Exception:
                            pass

                monitor_thread = threading.Thread(target=monitor_writer, daemon=True)
                monitor_thread.start()

            self.session = {
                "sound_file": sound_file,
                "queue": q,
                "stop_event": stop_event,
                "thread": thread,
                "filename": filename,
                "start_time": time.time(),
                "mode": mode,
                "monitor_queue": monitor_queue,
                "monitor_thread": monitor_thread,
                "levels": deque(maxlen=1000),
            }
            self.last_activity = time.time()
            return True, None

    def stop(self):
        with self.lock:
            session = self.session
            if session is None:
                return None, "Not recording"
            self.session = None

        session["stop_event"].set()
        session["thread"].join(timeout=5)
        session["sound_file"].close()
        if session["monitor_thread"] is not None:
            session["monitor_thread"].join(timeout=5)
        self.last_activity = time.time()
        duration = time.time() - session["start_time"]
        return (session["filename"], duration), None

    def is_idle(self):
        return self.session is None

    def shutdown(self):
        with self.lock:
            self._close_streams()


def _handle_client(conn, recorder):
    with conn:
        try:
            data = conn.recv(65536).decode()
            request = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            conn.sendall(json.dumps({"ok": False, "error": "bad request"}).encode())
            return

        cmd = request.get("cmd")
        if cmd == "start":
            ok, err = recorder.start(
                request["filename"],
                request.get("mode", "input"),
                request.get("device_name"),
                request.get("monitor", False),
            )
            conn.sendall(json.dumps({"ok": ok, "error": err}).encode())
        elif cmd == "stop":
            result, err = recorder.stop()
            if err:
                conn.sendall(json.dumps({"ok": False, "error": err}).encode())
            else:
                filename, duration = result
                conn.sendall(json.dumps({"ok": True, "filename": filename, "duration": duration}).encode())
        elif cmd == "levels":
            levels = recorder.drain_levels()
            if levels is None:
                conn.sendall(json.dumps({"ok": False, "error": "Not recording"}).encode())
            else:
                conn.sendall(json.dumps({"ok": True, "levels": levels}).encode())
        elif cmd == "ping":
            conn.sendall(json.dumps({"ok": True}).encode())
        else:
            conn.sendall(json.dumps({"ok": False, "error": "unknown command"}).encode())


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if PORT_FILE.exists():
        try:
            stale_port = int(PORT_FILE.read_text().strip())
        except ValueError:
            stale_port = None
        if stale_port is not None:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.settimeout(0.3)
            try:
                probe.connect((HOST, stale_port))
                probe.close()
                sys.stderr.write("recordit daemon is already running\n")
                sys.exit(1)
            except OSError:
                probe.close()
        PORT_FILE.unlink()  # stale port file from a crashed daemon

    recorder = Recorder()
    try:
        recorder.open_input_stream()
    except Exception as exc:
        sys.stderr.write(f"No default input device available: {exc}\n")
        sys.exit(1)

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind((HOST, 0))
    server.listen(8)
    PORT_FILE.write_text(str(server.getsockname()[1]))

    shutdown_event = threading.Event()

    def shut_down(*_args):
        shutdown_event.set()
        try:
            server.close()
        except OSError:
            pass

    signal.signal(signal.SIGTERM, shut_down)
    signal.signal(signal.SIGINT, shut_down)

    def idle_watchdog():
        while not shutdown_event.is_set():
            time.sleep(30)
            if recorder.is_idle() and (time.time() - recorder.last_activity) > IDLE_TIMEOUT:
                shut_down()
                return

    threading.Thread(target=idle_watchdog, daemon=True).start()

    print("recordit daemon ready", flush=True)
    while not shutdown_event.is_set():
        try:
            conn, _ = server.accept()
        except OSError:
            break
        threading.Thread(target=_handle_client, args=(conn, recorder), daemon=True).start()

    if not recorder.is_idle():
        recorder.stop()
    recorder.shutdown()
    try:
        PORT_FILE.unlink()
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    main()
