from pathlib import Path

STATE_DIR = Path.home() / ".recordit"
SOCK_PATH = STATE_DIR / "recordit.sock"
LOG_FILE = STATE_DIR / "recordit.log"

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
