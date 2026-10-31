"""Current-user Windows startup registration, shared with the installer."""
import subprocess
import sys
from pathlib import Path

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
APPROVED_KEY = r'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run'
VALUE_NAME = 'Zhiwei'


def startup_command():
    executable = Path(sys.executable).resolve()
    if getattr(sys, 'frozen', False):
        args = [str(executable), '--startup']
    else:
        pythonw = executable.with_name('pythonw.exe')
        if not pythonw.is_file():
            raise ValueError('未找到后台启动所需的 pythonw.exe，请使用安装版客户端')
        args = [str(pythonw), str(Path(__file__).resolve().parent.parent / 'client_main.py'), '--startup']
    command = subprocess.list2cmdline(args)
    # Windows Run entries have a 260-character command limit.
    if len(command) > 260:
        raise ValueError('程序路径过长，请移到较短的路径后再开启开机自启')
    return command


def startup_status():
    if sys.platform != 'win32':
        return {'supported': False, 'enabled': False, 'blocked': False}
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            command, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except FileNotFoundError:
        command = ''
    blocked = False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as key:
            approval, _ = winreg.QueryValueEx(key, VALUE_NAME)
            blocked = isinstance(approval, bytes) and approval[:1] in (b'\x03', b'\x07')
    except FileNotFoundError:
        pass
    return {'supported': True, 'enabled': bool(command), 'blocked': bool(command) and blocked}


def set_autostart(enabled):
    if type(enabled) is not bool:
        raise ValueError('开机自启设置必须为开启或关闭')
    if sys.platform != 'win32':
        raise ValueError('开机自启仅支持 Windows')
    import winreg
    try:
        if enabled:
            command = startup_command()
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
        return startup_status()
    except OSError as error:
        raise ValueError('无法修改开机自启设置，请检查当前 Windows 用户权限') from error
