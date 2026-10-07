# recordit

recordit is a lightweight terminal-based audio recorder. It records audio from your system's
default input device, your system's current output (via a loopback device),
or a specific device you pick — all from a single command that's fast enough
to use mid-session alongside CPU/RAM-heavy audio software (DAWs, virtual
instruments, etc).

A small background daemon keeps the audio device open and idling between
recordings, so after the first use, starting a new recording is near-instant
instead of paying audio device setup costs every time.

## Demo

<img width="525" height="361" alt="recordit" src="https://github.com/user-attachments/assets/8beeb65b-60ad-4e2b-b094-8d3986434a2b" />




## Requirements

- Python 3.8+
- macOS, Linux, or Windows
- For `--output`/`--device` with a non-input device: a loopback-capable
  virtual audio device, e.g. [BlackHole](https://github.com/ExistentialAudio/BlackHole)
  on macOS.

## Install

```
git clone https://github.com/tylerprattco/recordit.git
cd recordit
pip install -e .
```

This installs the `recordit` command. Its audio libraries (`sounddevice` and
`soundfile`) bundle PortAudio and libsndfile on macOS and Windows, so
there's nothing else to install there. On Linux, install PortAudio from your
package manager first, e.g. `sudo apt install libportaudio2`.

To update later, run `git pull` in the same folder.

## Usage

```
recordit take1
```

Starts recording immediately from your default input device, saving to
`take1.wav` in your current directory. Leave out the name (just `recordit`)
and it's named after the time it started, e.g.
`recordit 2026-10-06 at 12.28.02 PM.wav`; this works with the flags below
too (`recordit --output`).

### Short flags

Each flag has a single-dash short form:

| Flag         | Short |
|--------------|-------|
| `--output`   | `-o`  |
| `--device`   | `-d`  |
| `--monitor`  | `-m`  |
| `--showfile` | `-s`  |

For example, `recordit take1 -o -m` is the same as
`recordit take1 --output --monitor`.

To get an MP3 instead, end the name in `.mp3`:

```
recordit take1.mp3
```

It records a WAV as usual (hidden, as `.take1.recording.wav`), then when you
stop, converts it to a 320 kbps MP3 with [ffmpeg](https://ffmpeg.org) and
deletes the WAV. This needs ffmpeg installed with MP3 support (e.g.
`brew install ffmpeg` on macOS). If the conversion fails, the WAV is kept as
`take1.wav` instead (or left under its hidden name if `take1.wav` already
exists), so the recording is never lost.

While it's recording, a live timer
and scrolling waveform show, with clickable controls underneath:

```
● recording  01:23  ▁▂▅▇█▆▃▂▁▁▂▄▆▇▅▃▂▁▂▃▅▇▆▄▂▁
  [ ⏸ ]   [ ⏹ ]   [ ✕ ]
```

| Click / key          | Effect                                     |
|----------------------|--------------------------------------------|
| `⏸` / space or `p`   | Pause (becomes `▶`; click again to resume) |
| `⏹` / `s` or Shift+S | Stop and save                              |
| `✕` / `x` or Shift+X | Stop and discard the file                  |
| Ctrl+C               | Stop and save                              |

Paused time isn't recorded, and the timer and waveform freeze while paused
(with `--monitor`, you still hear the source). The waveform has one column
per 0.1s of audio, scaled linearly by peak amplitude like a DAW waveform, and the
last one stays on screen after you stop.

Clicking uses your terminal's mouse reporting (supported by macOS Terminal,
iTerm2, Windows Terminal and most Linux terminals). While recording, the
terminal sends clicks to recordit, so to select text hold Option (macOS) or
Shift (Windows Terminal, most Linux terminals) while dragging. In a terminal
without mouse reporting, such as the classic Windows console window, the
buttons still show and the keyboard shortcuts work, but clicks do nothing.

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

### Showing the file when done

```
recordit take1 --showfile
```

Once the recording is saved, opens your default file browser on its folder
(Finder selects the file on macOS, Explorer on Windows; elsewhere the folder
is opened with `xdg-open`). Nothing opens if you discard the recording.


### Help

```
recordit --help
```

Lists all flags plus the in-session recording controls.

## Notes

- Files are saved into whichever directory you ran `recordit` from.
- The background daemon auto-shuts-down after 30 minutes of inactivity,
  releasing the audio device.
- The daemon only accepts commands carrying a random token it generates at
  startup and stores in `~/.recordit/recordit.port`, readable only by your
  user account, so other users' programs on the same machine can't control
  it.
- Windows support has had light testing so far; if you hit issues there,
  please open an issue.

## Contributing

recordit is a personal project, maintained on a best-effort basis. Bug
reports and pull requests are welcome; for anything bigger than a small
fix, please open an issue first to talk it through.

To run the tests:

```
pip install -e ".[test]"
pytest
```

They also run automatically on macOS, Windows and Linux for every pull
request. The tests don't need a microphone; they feed synthetic audio
straight to the recorder.

## License

[MIT](LICENSE)
