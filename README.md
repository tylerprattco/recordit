# recordit

A lightweight terminal WAV recorder. Records 44.1kHz/16-bit audio from your
default input device, your system's current output (via a loopback device),
or a specific device you pick — all from a single command that's fast enough
to use mid-session alongside CPU/RAM-heavy audio software (DAWs, virtual
instruments, etc).

A small background daemon keeps the audio device open and idling between
recordings, so after the first use, starting a new recording is near-instant
instead of paying audio device setup costs every time.

## Requirements

- Python 3.8+
- macOS, Linux, or Windows
- For `--output`/`--device` with a non-input device: a loopback-capable
  virtual audio device, e.g. [BlackHole](https://github.com/ExistentialAudio/BlackHole)
  on macOS.

## Install

```
pip install -e .
```

This installs the `recordit` command (via `sounddevice` and `soundfile`,
which bundle PortAudio/libsndfile — no separate system install needed).

## Usage

```
recordit take1
```

Starts recording immediately from your default input device, saving to
`take1.wav` in your current directory. While it's recording, a live timer
and scrolling waveform show, with clickable controls underneath:

```
● recording  01:23  ▁▂▅▇█▆▃▂▁▁▂▄▆▇▅▃▂▁▂▃▅▇▆▄▂▁
  [ ⏸ ]   [ ⏹ ]   [ ✕ ]
```

| Click / key          | Effect                                     |
|----------------------|--------------------------------------------|
| `⏸` / space or `p`   | Pause (becomes `▶`; click again to resume) |
| `⏹` / `s`            | Stop and save                              |
| `✕` / `x`            | Stop and discard the file                  |
| Ctrl+C               | Stop and save                              |

Paused time isn't recorded, and the timer and waveform freeze while paused
(with `--monitor`, you still hear the source). The waveform has one column
per 0.1s of audio, scaled from -60 dBFS (lowest bar) to full scale, and the
last one stays on screen after you stop.

Clicking uses your terminal's mouse reporting (supported by macOS Terminal,
iTerm2 and most Linux terminals). While recording, the terminal sends clicks
to recordit, so to select text hold Option (macOS) or Shift (most Linux
terminals) while dragging. Where clicks can't be read (e.g. on Windows),
type `stop` or `delete` and press Enter instead.

### Recording system audio instead of the mic

```
recordit take1 --output
```

Records whatever is currently going to your system's default output device,
instead of the input device. This only works if that output device is (or
includes) a loopback-capable device such as BlackHole — a normal speaker
output has no input side and can't be captured this way.

### Picking a specific device

```
recordit take1 --device
```

Lists all input and output devices, numbered, and prompts you to pick one:

```
Input devices:
  1) Built-in Microphone
  2) BlackHole 2ch

Output devices:
  3) Built-in Output
  4) BlackHole 2ch

Select a device number:
```

Useful for recording from a specific virtual/loopback device (e.g. a Pro
Tools Aggregate I/O or BlackHole channel) regardless of what your system
default is set to.

### Monitoring while recording a loopback device

```
recordit take1 --output --monitor
recordit take1 --device --monitor
```

If you're recording from a loopback device (so you can't hear it directly —
e.g. your system output is set straight to BlackHole), `--monitor` also
plays the captured audio live to your actual output hardware so you can
listen while it records. Only valid combined with `--output` or `--device`.

If the device you'd monitor to turns out to be the exact same device you're
recording from (e.g. `--output` with BlackHole as your only system output),
`recordit` refuses rather than create a feedback loop — in that case, either
use `--device` to record BlackHole explicitly while your real output stays
separate, or set up a Multi-Output Device that includes real speakers.

### Help

```
recordit --help
```

Lists all flags plus the in-session `stop`/`delete` controls.

## Notes

- Files are saved into whichever directory you ran `recordit` from, even if
  you later run `stop`/`delete` from a different shell.
- The background daemon auto-shuts-down after 30 minutes of inactivity,
  releasing the audio device.
- Windows support (TCP-loopback IPC, detached process spawn) has been
  written to be cross-platform but not yet tested on an actual Windows
  machine — if you hit issues there, please report them.
