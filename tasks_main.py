"""Headless periodic worker; never starts the desktop or sends WeChat messages."""
import argparse
import asyncio
import os
from pathlib import Path
import sys


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--data')
    args=parser.parse_args()
    if args.data:os.environ['WECHAT_HELPER_DATA']=str(Path(args.data).resolve())
    from helper import storage, memory_store as store, scheduler, calendar_sync
    from helper.wechat import wechat
    source=wechat.current.get('path')
    if not source:return 0
    account=store.account_key(Path(source))
    config=scheduler.configuration(account)
    if not any(config[p] for p in ('day','week','month')) and not calendar_sync.settings(account,False)['auto_sync']:return 0
    wechat.connect(source)
    if wechat.status['state']!='ready':
        key=store.run_started(account,'','connection','后台微信连接')
        store.finish_run(key,'error','无法读取微信账号，请在工作台检查连接或重新提取密钥')
        return 1
    result=asyncio.run(scheduler.tick(wechat))
    return 1 if result.get('errors') else 0


if __name__=='__main__':
    try:sys.exit(main())
    except Exception:
        # Metadata-only diagnostic; no chat content or credentials in logs.
        from helper.storage import DATA
        (DATA/'background-error.log').write_text('后台周期整理失败，请在应用的自动化页面检查设置。',encoding='utf-8')
        sys.exit(1)
