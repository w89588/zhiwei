"""Exercise filtering before encryption, using SQLite and the real V2 receiver."""
import hashlib
import os
import shutil
import sqlite3
from contextlib import closing

import pytest

from backend.platform_app import create_app
from helper import storage
from helper.platform_sync import PlatformUploader
from test_integration import desktop
from test_platform import platform, user, key, mcp, UI
from test_server import live_server


CONTACT_SCHEMA = '''CREATE TABLE contact(
    username TEXT,nick_name TEXT,remark TEXT,
    local_type INTEGER,flag INTEGER,verify_flag INTEGER,delete_flag INTEGER)'''


def add_message(path, user, sender='friend', kind=1, content='合成聊天消息'):
    table = 'Msg_' + hashlib.md5(user.encode()).hexdigest()
    with closing(sqlite3.connect(path)) as con, con:
        con.execute('CREATE TABLE IF NOT EXISTS Name2Id(user_name TEXT)')
        sender_id = con.execute('INSERT INTO Name2Id VALUES (?)', (sender,)).lastrowid
        if sender != user:
            con.execute('INSERT INTO Name2Id VALUES (?)', (user,))
        con.execute(f'CREATE TABLE IF NOT EXISTS [{table}](local_id INTEGER,real_sender_id INTEGER,local_type INTEGER,create_time INTEGER,message_content TEXT)')
        local_id = con.execute(f'SELECT COALESCE(MAX(local_id),0)+1 FROM [{table}]').fetchone()[0]
        con.execute(f'INSERT INTO [{table}] VALUES (?,?,?,?,?)', (local_id, sender_id, kind, 1728000100+local_id, content))


@pytest.fixture
def mixed_chats(desktop, monkeypatch):
    _, _, _, w, tmp = desktop
    for folder in ('contact', 'message', 'session'):
        (w.root/folder).mkdir(parents=True, exist_ok=True)
    for name, (_, path) in list(w.cache.items()):
        shutil.copy2(path, w.root/name)
    # Real WeChat contact fields: a row also exists for non-friend group members.
    contact_types = [
        ('friend', 1, 3, 0, 0), ('enterprise@openim', 5, 1, 0, 0),
        ('group@chatroom', 2, 2, 0, 0), ('group_member', 3, 4, 0, 0),
        ('stranger', 0, 0, 0, 0), ('deleted', 1, 3, 0, 1),
        ('gh_official', 1, 3, 0, 0), ('legacy_official', 1, 3, 24, 0),
        ('notification_messages', 1, 3, 0, 0), ('brandsessionholder', 1, 3, 0, 0),
        ('filehelper', 1, 3, 0, 0), ('unknown_type', 7, 1, 0, 0),
        ('no_friend_flag', 1, 0, 0, 0), ('unknown_flags', 1, None, None, None),
    ]
    with closing(sqlite3.connect(w.root/'contact/contact.db')) as con, con:
        con.execute(CONTACT_SCHEMA)
        con.executemany('INSERT INTO contact VALUES (?,?,?,?,?,?,?)',
                        [(name, name, '备注不代表好友', *flags) for name, *flags in contact_types])
    for name, *_ in contact_types:
        if name != 'friend':
            add_message(w.root/'message/message_0.db', name, sender='group_member')
    # A group need not be saved in the address book. A historical private chat
    # absent from the address book must not be admitted via Name2Id or SessionTable.
    add_message(w.root/'message/message_0.db', 'archived@chatroom', sender='group_member')
    add_message(w.root/'message/message_0.db', 'not_in_contacts')
    add_message(w.root/'message/message_0.db', 'friend', kind=49,
                content='<msg><appmsg><title>好友转发的文章</title><url>https://mp.weixin.qq.com/s/synthetic</url></appmsg></msg>')
    with closing(sqlite3.connect(w.root/'session/session.db')) as con, con:
        con.execute('CREATE TABLE SessionTable(username TEXT,unread_count INTEGER,summary TEXT,last_timestamp INTEGER)')
        con.execute("INSERT INTO SessionTable VALUES ('session_only_stranger',1,'不应上传的预览',1728000100)")
    biz = w.root/'message/biz_message_0.db'
    add_message(biz, 'biz_only')
    add_message(biz, 'friend', content='公众号库内的推送不应上传')
    # Simulate an old process/cache that already indexed business-message shards.
    w.cache['message/biz_message_0.db'] = (None, biz)
    with closing(sqlite3.connect(biz)) as con:
        w.tables['message/biz_message_0.db'] = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    uploader = PlatformUploader()
    monkeypatch.setattr('helper.platform_sync.platform_uploader', uploader)
    return w, uploader


def test_message_types_and_shared_link_survive_client_server_and_mcp(mixed_chats, platform):
    w, uploader = mixed_chats
    database = w.root/'message/message_0.db'
    kinds = [1, 3, 34, 42, 43, 47, 48, 50, 10000, 10002, 9999]
    for kind in kinds:
        add_message(database, 'friend', kind=kind, content=f'合成类型{kind}')
    subtype = (5 << 32) | 49
    raw = ('<msg><appmsg><title>跨端分享</title><des>链接摘要</des>'
           '<url>https://example.com/shared?x=1&amp;y=2</url></appmsg></msg>')
    add_message(database, 'friend', kind=subtype, content=raw)
    w.sync()
    local = w.messages('friend', limit=100, query='example.com/shared')
    assert local['total'] == 1 and local['items'][0]['extra']['url'].endswith('x=1&y=2')
    assert local['items'][0]['extra']['local_type'] == str(subtype)
    web, _ = user(platform)
    _, service, _ = platform
    credentials = web.post('/api/v2/clients', headers=UI, json={'name':'媒体类型测试'}).json()
    with live_server(create_app(service, start_worker=False)) as url:
        uploader.configure(w, url, credentials['app_key'], credentials['app_secret'], True, 'all')
        result = uploader.sync(w, full=True)
        assert result['state'] == 'done', result
    sid = next(item['id'] for item in web.get('/api/v2/sessions').json()['items'] if item['source_id'] == 'friend')
    items = web.get(f'/api/v2/sessions/{sid}/messages', params={'limit':100}).json()['items']
    assert set(kinds + [49]) <= {item['type'] for item in items}
    assert next(item for item in items if item['text'] == '跨端分享')['extra']['local_type'] == str(subtype)
    found = web.get(f'/api/v2/sessions/{sid}/messages', params={'q':'example.com/shared'}).json()
    assert found['total'] == 1 and found['items'][0]['extra']['des'] == '链接摘要'
    assert web.post(f'/api/v2/sessions/{sid}/listener', headers=UI, json={'enabled':True}).status_code == 200
    token = key(web, sessions=[sid])['key']
    response = mcp(web, token, 'search_messages', {'session_id':sid, 'query':'example.com/shared'}).json()['result']
    assert not response.get('isError') and 'https://example.com/shared' in str(response)


@pytest.mark.parametrize('scope', ['all', 'selected'])
def test_only_groups_and_address_book_friends_cross_upload_boundary(mixed_chats, platform, monkeypatch, scope):
    from helper import platform_sync
    w, uploader = mixed_chats
    web, _ = user(platform)
    _, service, _ = platform
    credentials = web.post('/api/v2/clients', headers=UI, json={'name': 'filter test'}).json()
    payloads = []
    original_envelope = platform_sync.envelope

    def capture(config, payload):
        payloads.append(payload)
        return original_envelope(config, payload)

    monkeypatch.setattr(platform_sync, 'envelope', capture)
    with live_server(create_app(service, start_worker=False)) as url:
        uploader.configure(w, url, credentials['app_key'], credentials['app_secret'], True, scope)
        w.sync()
        if scope == 'selected':
            storage.write_json('workspace.json', {
                os.path.normcase(str(w.root.resolve())): {s['id']: {'note': ''} for s in w.sessions()}
            })
        result = uploader.sync(w)
        assert result['state'] == 'done', result
        assert uploader.changed_messages == 10  # Seven friend messages, two groups, one enterprise friend.
        allowed = {'friend', 'enterprise@openim', 'group@chatroom', 'archived@chatroom'}
        manifests = [p for p in payloads if p['kind'] == 'manifest']
        assert {s['id'] for p in manifests for s in p['sessions']} == allowed
        assert {c['id'] for p in manifests for c in p['contacts']} == allowed - {'archived@chatroom'}
        message_payloads = [p for p in payloads if p['kind'] == 'messages']
        assert {p['session'] for p in message_payloads} == allowed
        assert all(m['database'].startswith('message/message_') for p in message_payloads for m in p['items'])
        assert any(m['sender_id'] == 'group_member' for p in message_payloads if p['session'].endswith('@chatroom') for m in p['items'])
        assert any(m['text'] == '好友转发的文章' for p in message_payloads for m in p['items'])
        sessions = web.get('/api/v2/sessions').json()['items']
        assert len(sessions) == 4 and sum(s['message_count'] for s in sessions) == 10
        assert web.get('/api/v2/contacts').json()['total'] == 3
        # Business shards are neither newly opened nor usable through a stale cache.
        assert 'biz_message_0.db' not in {p.name for p in w.files()}
        assert w.upload_batch('friend', 'message/biz_message_0.db', platform=True) == []
        for denied in ('stranger', 'group_member', 'deleted', 'gh_official', 'legacy_official', 'not_in_contacts'):
            with pytest.raises(ValueError, match='授权范围'):
                w.upload_batch(denied, 'message/message_0.db', platform=True)
        # Full history and overlap retries enforce the same scope and deduplicate.
        assert uploader.sync(w, full=True)['state'] == 'done'
        assert uploader.changed_messages == 10


def test_contact_changes_take_effect_on_next_snapshot(mixed_chats):
    w, uploader = mixed_chats
    uploader.configure(w, 'http://127.0.0.1:8787', 'app_'+'a'*32, 'secret_'+'b'*32, True, 'all')
    w.sync()
    assert uploader.allowed(w, 'friend') and not uploader.allowed(w, 'stranger')
    with closing(sqlite3.connect(w.root/'contact/contact.db')) as con, con:
        con.execute("UPDATE contact SET delete_flag=1 WHERE username='friend'")
        con.execute("UPDATE contact SET local_type=1,flag=3 WHERE username='stranger'")
    w.sync()
    assert not uploader.allowed(w, 'friend') and uploader.allowed(w, 'stranger')
    w.root.joinpath('contact/contact.db').unlink()
    w.sync()
    with pytest.raises(ValueError, match='通讯录读取失败'):
        uploader.allowed(w, 'stranger')


@pytest.mark.parametrize('failure', ['missing', 'schema', 'snapshot'])
def test_contact_failure_sends_no_manifest_or_messages(mixed_chats, monkeypatch, failure):
    w, uploader = mixed_chats
    uploader.configure(w, 'http://127.0.0.1:8787', 'app_'+'a'*32, 'secret_'+'b'*32, True, 'all')
    w.sync()
    w.sync_contacts()  # Prime a valid cache before losing the classification source.
    path = w.root/'contact/contact.db'
    if failure == 'missing':
        path.unlink()
    elif failure == 'schema':
        with closing(sqlite3.connect(path)) as con, con:
            con.execute('ALTER TABLE contact DROP COLUMN flag')
    else:
        path.write_bytes(b'invalid sqlite snapshot')
    calls = []
    monkeypatch.setattr(uploader, 'client', lambda config: calls.append(config))
    result = uploader.sync(w)
    assert result['state'] == 'error' and result['uploaded'] == 0
    assert not calls  # Failure must happen before registration or any upload.


def test_new_account_does_not_reuse_previous_friend_classification(mixed_chats):
    w, uploader = mixed_chats
    uploader.configure(w, 'http://127.0.0.1:8787', 'app_'+'a'*32, 'secret_'+'b'*32, True, 'all')
    w.sync()
    assert w.can_sync_session('friend')
    second = w.root.parent.parent/'wxid_second_abcd'/'db_storage'
    (second/'contact').mkdir(parents=True)
    with closing(sqlite3.connect(second/'contact/contact.db')) as con, con:
        con.execute(CONTACT_SCHEMA)
        con.execute("INSERT INTO contact VALUES ('friend','同名陌生人','',3,4,0,0)")
    w.connect(second, persist=False)
    assert w.status['state'] == 'ready'
    assert not w.can_sync_session('friend')
