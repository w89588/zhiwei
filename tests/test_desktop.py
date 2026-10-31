"""Startup registration and single-instance behavior; never edit the real Run key."""
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from helper import autostart
from helper.single_instance import SingleInstance


@pytest.fixture
def registry(monkeypatch):
    values = {}

    class Key:
        def __init__(self, path): self.path = path
        def __enter__(self): return self
        def __exit__(self, *args): pass

    def query(key, name):
        try: return values[(key.path, name)], 1
        except KeyError: raise FileNotFoundError from None

    def delete(key, name):
        try: del values[(key.path, name)]
        except KeyError: raise FileNotFoundError from None

    fake = SimpleNamespace(
        HKEY_CURRENT_USER=1, KEY_SET_VALUE=2, REG_SZ=1,
        OpenKey=lambda root, path, *args: Key(path),
        CreateKeyEx=lambda root, path, *args: Key(path),
        QueryValueEx=query, DeleteValue=delete,
        SetValueEx=lambda key, name, reserved, kind, value: values.update({(key.path, name): value}),
    )
    monkeypatch.setitem(sys.modules, 'winreg', fake)
    monkeypatch.setattr(autostart.sys, 'platform', 'win32')
    return values


def test_toggle_roundtrip_keeps_other_startup_entries(registry, monkeypatch):
    registry[(autostart.RUN_KEY, 'AnotherApp')] = 'other.exe'
    monkeypatch.setattr(autostart, 'startup_command', lambda: '"C:\\Program Files\\WechatHelper.exe" --startup')
    assert not autostart.startup_status()['enabled']
    assert autostart.set_autostart(True)['enabled']
    assert '--startup' in registry[(autostart.RUN_KEY, autostart.VALUE_NAME)]
    assert not autostart.set_autostart(False)['enabled']
    assert autostart.set_autostart(False)['enabled'] is False
    assert registry == {(autostart.RUN_KEY, 'AnotherApp'): 'other.exe'}


def test_windows_disable_is_reported_and_not_overridden(registry, monkeypatch):
    approved = (autostart.APPROVED_KEY, autostart.VALUE_NAME)
    registry[approved] = b'\x03' + b'\x00' * 11
    monkeypatch.setattr(autostart, 'startup_command', lambda: 'app.exe --startup')
    assert autostart.set_autostart(True) == {'supported': True, 'enabled': True, 'blocked': True}
    assert registry[approved][0] == 3


def test_frozen_command_quotes_space_and_contains_only_startup(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'Program Files' / 'WechatHelper.exe'))
    command = autostart.startup_command()
    assert command.startswith('"') and command.endswith('" --startup')


def test_source_command_uses_absolute_script_and_pythonw(monkeypatch, tmp_path):
    pythonw = tmp_path / 'pythonw.exe'
    pythonw.touch()
    monkeypatch.setattr(sys, 'frozen', False, raising=False)
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'python.exe'))
    command = autostart.startup_command()
    assert str(pythonw) in command and str(Path(autostart.__file__).resolve().parent.parent / 'client_main.py') in command
    assert command.endswith('--startup')


def test_failed_write_does_not_report_success(registry, monkeypatch):
    monkeypatch.setattr(autostart, 'startup_command', lambda: 'app.exe --startup')
    def denied(*args): raise PermissionError('denied')
    monkeypatch.setattr(sys.modules['winreg'], 'CreateKeyEx', denied)
    with pytest.raises(ValueError, match='无法修改'):
        autostart.set_autostart(True)
    assert not autostart.startup_status()['enabled']


def test_switch_rejects_truthy_strings(registry):
    with pytest.raises(ValueError): autostart.set_autostart('false')
    assert not registry


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows named handles')
def test_second_instance_activates_first_and_release_allows_restart(tmp_path):
    first, second = SingleInstance(tmp_path), SingleInstance(tmp_path)
    stop, shown = threading.Event(), threading.Event()
    thread = threading.Thread(target=first.listen, args=(stop, shown.set))
    try:
        assert first.primary and not second.primary
        thread.start()
        second.activate()
        assert shown.wait(3)
    finally:
        stop.set()
        if thread.is_alive(): thread.join(3)
        second.close(); first.close()
    restarted = SingleInstance(tmp_path)
    try: assert restarted.primary
    finally: restarted.close()
