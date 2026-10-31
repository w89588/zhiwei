"""Optional current-user Windows Task Scheduler entry, installed only from settings."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from . import storage


def task_name():
    suffix=hashlib.sha256(str(storage.DATA.resolve()).encode()).hexdigest()[:10]
    return 'WechatHelperMemory-'+suffix


def command():
    if getattr(sys,'frozen',False):
        executable=Path(sys.executable).parent/'WechatHelperTasks.exe'
        if not executable.exists():raise ValueError('当前安装缺少后台任务程序，请使用完整 1.0 安装包')
        args=[str(executable)]
    else:
        # Task Scheduler launches the interpreter directly, without a parent
        # process that can supply CREATE_NO_WINDOW.
        interpreter=Path(sys.executable).with_name('pythonw.exe')
        if not interpreter.exists():raise ValueError('后台执行需要无控制台的 pythonw.exe，请使用完整安装包')
        args=[str(interpreter),str(Path(__file__).resolve().parent.parent/'tasks_main.py')]
    return subprocess.list2cmdline(args+['--once','--data',str(storage.DATA.resolve())])


def status():
    return {'enabled':bool(storage.read_json('background.json',{}).get('enabled',False)),
            'supported':os.name=='nt','task_name':task_name()}


def configure(enabled):
    if os.name!='nt':raise ValueError('后台执行仅支持 Windows')
    def run(arguments,timeout):
        try:return subprocess.run(arguments,capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=timeout)
        except (OSError,subprocess.TimeoutExpired) as error:raise ValueError('无法操作 Windows 任务计划，请检查当前用户权限或稍后重试') from error
    if enabled:
        registered_command=command()
        arguments=['schtasks.exe','/Create','/TN',task_name(),'/TR',registered_command,'/SC','MINUTE','/MO','15','/IT','/F']
    else:
        result=run(['schtasks.exe','/Query','/TN',task_name()],15)
        arguments=['schtasks.exe','/Delete','/TN',task_name(),'/F'] if result.returncode==0 else None
    if arguments:
        result=run(arguments,30)
        if result.returncode:raise ValueError('Windows 后台任务设置失败，请检查当前用户的任务计划权限')
    storage.write_json('background.json',{'enabled':enabled,'command':registered_command if enabled else ''})
    return status()


def refresh_registration():
    """Move an opted-in task to this installation after an upgrade or move."""
    if os.name!='nt' or not getattr(sys,'frozen',False):return False
    settings=storage.read_json('background.json',{})
    if not settings.get('enabled'):return False
    try:
        if settings.get('command')==command():return False
        configure(True)
    except (ValueError,OSError):
        # Keep the opt-in and old registration intact; retry on the next launch.
        # Diagnostics contain neither chat content nor credentials.
        try:(storage.DATA/'background-registration-error.log').write_text('后台任务路径更新失败，请在自动化页面重新开启后台执行。',encoding='utf-8')
        except OSError:pass
        return False
    try:(storage.DATA/'background-registration-error.log').unlink(missing_ok=True)
    except OSError:pass
    return True
