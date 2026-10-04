from pathlib import Path

STATE_DIR = Path.home() / ".recordit"
LOG_FILE = STATE_DIR / "recordit.log"

# The daemon listens on a loopback TCP port (rather than a Unix domain
# socket) so the same code works on Windows as well as macOS/Linux. It picks
# an ephemeral free port at startup and records it here for clients to find.
PORT_FILE = STATE_DIR / "recordit.port"
HOST = "127.0.0.1"

SAMPLE_RATE = 44100
SUBTYPE = "PCM_16"
DTYPE = "int16"
BLOCKSIZE = 2048  # frames per callback; kept modest to stay cheap on CPU

# Used only for the capture/playback stream pair while --monitor is active.
# Much smaller than BLOCKSIZE to minimize monitoring latency, at the cost of
# more frequent callbacks (higher CPU) and less buffering headroom (more
# risk of clicks/dropouts under heavy CPU load). Recording to disk is
# unaffected either way.
MONITOR_BLOCKSIZE = 256
MONITOR_LATENCY = "low"

IDLE_TIMEOUT = 30 * 60  # seconds of inactivity before the daemon shuts itself down
