import os
from pathlib import Path

STATE_DIR = Path.home() / ".recordit"
LOG_FILE = STATE_DIR / "recordit.log"

# The daemon listens on a loopback TCP port (rather than a Unix domain
# socket) so the same code works on Windows as well as macOS/Linux. It picks
# an ephemeral free port at startup and records it here for clients to find,
# along with a random token that every request must carry. Any local process
# can connect to a loopback port, so the token -- in a file only this user
# can read -- is what stops other users' processes from driving the daemon
# (e.g. starting a recording, or writing a WAV file to an arbitrary path).
PORT_FILE = STATE_DIR / "recordit.port"
HOST = "127.0.0.1"


def read_port_file():
    """Return (port, token), or None if no daemon has written the file.

    A daemon from before tokens were added writes only the port; it ignores
    the token, so an empty one is returned to keep talking to it.
    """
    try:
        parts = PORT_FILE.read_text().split()
        return int(parts[0]), (parts[1] if len(parts) > 1 else "")
    except (FileNotFoundError, ValueError, IndexError):
        return None


def write_port_file(port, token):
    # Created owner-read/write only, so other users can't read the token.
    # (On Windows the mode is ignored, but the home directory is already
    # private to its user.)
    fd = os.open(PORT_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"{port} {token}\n")

SAMPLE_RATE = 44100
SUBTYPE = "PCM_16"
DTYPE = "int16"
BLOCKSIZE = 2048  # frames per callback; kept modest to stay cheap on CPU

# Used only for the capture/playback stream pair while -monitor is active.
# Much smaller than BLOCKSIZE to minimize monitoring latency, at the cost of
# more frequent callbacks (higher CPU) and less buffering headroom (more
# risk of clicks/dropouts under heavy CPU load). Recording to disk is
# unaffected either way.
MONITOR_BLOCKSIZE = 256
MONITOR_LATENCY = "low"

# Length of the fade applied to the recording at each pause and resume, so
# the splice doesn't click.
PAUSE_FADE_FRAMES = SAMPLE_RATE // 100  # 10 ms

IDLE_TIMEOUT = 30 * 60  # seconds of inactivity before the daemon shuts itself down
