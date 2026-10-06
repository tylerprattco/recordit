"""Terminal setup for the live recording controls: escape-sequence (VT)
output, and unbuffered key/mouse input, on both Unix (termios) and Windows
(console modes).

Both platforms end up speaking the same protocol -- xterm escape sequences
out, and keys, SGR mouse reports and cursor position reports in as bytes --
so the control panel in cli.py doesn't need to know which one it's on.
"""

import sys
import time

if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    termios = tty = None
else:
    import os
    import select

    try:
        import termios
        import tty
    except ImportError:
        termios = tty = None

# Windows console handles and mode flags.
STD_INPUT_HANDLE = -10
STD_ERROR_HANDLE = -12
ENABLE_PROCESSED_INPUT = 0x0001  # Ctrl+C raises KeyboardInterrupt
ENABLE_LINE_INPUT = 0x0002
ENABLE_ECHO_INPUT = 0x0004
ENABLE_QUICK_EDIT_MODE = 0x0040  # console grabs clicks for text selection
ENABLE_EXTENDED_FLAGS = 0x0080  # required to change ENABLE_QUICK_EDIT_MODE
ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200  # mouse/position reports arrive as escape sequences
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004  # output escape sequences are interpreted


def _console_mode(std_handle):
    """Return (handle, mode) for a Windows standard handle; mode is None if
    the handle isn't a console."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(std_handle)
    mode = wintypes.DWORD()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return handle, None
    return handle, mode.value


def enable_vt_output():
    """Make escape sequences written to stderr be interpreted rather than
    printed; return whether they will be.

    Always the case on Unix terminals. On Windows it needs Windows 10 or
    later: Windows Terminal has it on already, while the classic console
    window has to have it switched on here.
    """
    if sys.platform != "win32":
        return True
    handle, mode = _console_mode(STD_ERROR_HANDLE)
    if mode is None:
        return False
    if mode & ENABLE_VIRTUAL_TERMINAL_PROCESSING:
        return True
    return bool(ctypes.windll.kernel32.SetConsoleMode(handle, mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING))


def raw_input_supported():
    if not sys.stdin.isatty():
        return False
    if sys.platform == "win32":
        return _console_mode(STD_INPUT_HANDLE)[1] is not None
    return termios is not None


class RawInput:
    """Context manager that puts stdin in unbuffered, no-echo mode, so
    keypresses, mouse reports and cursor position reports can be read as
    they arrive. Ctrl+C still raises KeyboardInterrupt."""

    def __enter__(self):
        if sys.platform == "win32":
            self._handle, self._saved = _console_mode(STD_INPUT_HANDLE)
            mode = self._saved & ~(ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT | ENABLE_QUICK_EDIT_MODE)
            mode |= ENABLE_PROCESSED_INPUT | ENABLE_EXTENDED_FLAGS | ENABLE_VIRTUAL_TERMINAL_INPUT
            ctypes.windll.kernel32.SetConsoleMode(self._handle, mode)
        else:
            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc_info):
        if sys.platform == "win32":
            ctypes.windll.kernel32.SetConsoleMode(self._handle, self._saved)
        else:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
        return False

    def read(self, timeout):
        """Return the input that arrives within timeout seconds (b"" if
        none), or None once stdin is closed."""
        if sys.platform == "win32":
            # Console handles can't be select()ed, so poll.
            deadline = time.monotonic() + timeout
            while not msvcrt.kbhit():
                if time.monotonic() >= deadline:
                    return b""
                time.sleep(0.01)
            chars = []
            while msvcrt.kbhit():
                chars.append(msvcrt.getwch())
            return "".join(chars).encode("utf-8", "replace")
        ready, _, _ = select.select([self._fd], [], [], timeout)
        if not ready:
            return b""
        return os.read(self._fd, 1024) or None
