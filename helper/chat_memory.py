"""Account-scoped legacy background and summaries, with one-time safe migration."""
from . import memory_store as store, storage


def read(wechat, name):
    account=store.account_key(wechat.root)
    value=store.get_setting(account,name,None)
    if value is not None:return value
    with storage.WRITE_LOCK:
        owner=storage.read_json('legacy-memory-owner.json',{}).get('account')
        if not owner:
            path=wechat.current.get('path') or wechat.root
            owner=store.account_key(path)
            storage.write_json('legacy-memory-owner.json',{'account':owner})
        value=storage.read_json(name+'.json',{}) if owner==account else {}
        store.set_setting(account,name,value)
    return value


def write(wechat,name,value):
    store.set_setting(store.account_key(wechat.root),name,value)
