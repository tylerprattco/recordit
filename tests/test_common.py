import os
import stat
import sys

import pytest

from recordit import common


@pytest.fixture
def port_file(tmp_path, monkeypatch):
    path = tmp_path / "recordit.port"
    monkeypatch.setattr(common, "PORT_FILE", path)
    return path


def test_port_file_round_trip(port_file):
    common.write_port_file(51234, "abc123")
    assert common.read_port_file() == (51234, "abc123")


def test_port_file_from_a_daemon_without_tokens(port_file):
    port_file.write_text("51234")
    assert common.read_port_file() == (51234, "")


@pytest.mark.parametrize("contents", ["", "not-a-port abc", "\n"])
def test_unreadable_port_file(port_file, contents):
    port_file.write_text(contents)
    assert common.read_port_file() is None


def test_missing_port_file(port_file):
    assert common.read_port_file() is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_port_file_is_private(port_file):
    common.write_port_file(51234, "abc123")
    assert stat.S_IMODE(os.stat(port_file).st_mode) == 0o600
