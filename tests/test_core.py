import hashlib
import hmac
import struct
import sqlite3
from pathlib import Path
import pytest
from Crypto.Cipher import AES
from helper.crypto import PAGE,HEADER,mac_key,decrypt_page,snapshot,wal_checksum
from helper.wechat import Wechat,decode,parse_content
import zstandard

def encrypted_page(number,key,salt):
    iv=bytes(range(16));start=16 if number==1 else 0
    plain=bytes([number])*(PAGE-80-start)
    cipher=AES.new(key,AES.MODE_CBC,iv).encrypt(plain)
    mac=mac_key(key,salt)
    sig=hmac.new(mac,cipher+iv+struct.pack('<I',number),hashlib.sha512).digest()
    return (salt if number==1 else b'')+cipher+iv+sig

def test_page_hmac_rejects_corruption():
    key=bytes(range(32));salt=bytes(range(16))
    for number in (1,2):
        page=encrypted_page(number,key,salt)
        assert len(decrypt_page(page,number,key,mac_key(key,salt)))==PAGE
        broken=bytearray(page);broken[100]^=1
        with pytest.raises(ValueError):decrypt_page(bytes(broken),number,key,mac_key(key,salt))

def test_wal_only_committed_frames(tmp_path):
    key=bytes(range(32));salt=bytes(range(16));source=tmp_path/'source.db';target=tmp_path/'target.db'
    source.write_bytes(encrypted_page(1,key,salt)+encrypted_page(2,key,salt))
    header=struct.pack('>IIIIII',0x377f0682,3007000,PAGE,1,77,88)
    checksum=wal_checksum(header,(0,0),'<');wal=header+struct.pack('>II',*checksum)
    for number,commit in [(2,2),(3,0)]:
        body=encrypted_page(number,key,salt)
        first=struct.pack('>II',number,commit)
        checksum=wal_checksum(first+body,checksum,'<')
        wal+=first+header[16:24]+struct.pack('>II',*checksum)+body
    Path(str(source)+'-wal').write_bytes(wal)
    snapshot(source,target,key)
    assert target.stat().st_size==PAGE*2
    assert target.read_bytes().startswith(HEADER)

def test_compressed_xml_and_group_content():
    compressed=zstandard.ZstdCompressor().compress('甲:\n你好，世界'.encode())
    assert parse_content(compressed,1,True)[:2]==('甲','你好，世界')
    sender,text,extra=parse_content('<msg><appmsg><title>方案</title><refermsg><displayname>甲</displayname><content>旧消息</content></refermsg></appmsg></msg>',49)
    assert '方案' in text and '旧消息' in text


def test_shared_link_uses_outer_appmsg_fields():
    raw = ('<msg><appmsg><title>分享标题</title><des>内容摘要</des>'
           '<url>https://example.com/article?a=1&amp;b=2</url>'
           '<refermsg><title>引用标题</title><url>https://wrong.example/</url>'
           '<content>旧消息</content><displayname>甲</displayname></refermsg></appmsg></msg>')
    sender, text, extra = parse_content('wxid_friend:\n'+raw, 49, True)
    assert sender == 'wxid_friend' and '分享标题' in text and '旧消息' in text
    assert extra['des'] == '内容摘要' and extra['url'] == 'https://example.com/article?a=1&b=2'

def test_cross_shard_paging_and_compressed_search(tmp_path):
    user='friend';table='Msg_'+hashlib.md5(user.encode()).hexdigest(); w=Wechat();w.root=tmp_path/'wxid_me_abcd'/'db_storage';w.names={'friend':'朋友'}
    for shard in (0,1):
        path=tmp_path/f'{shard}.db';con=sqlite3.connect(path)
        con.execute('CREATE TABLE Name2Id(user_name TEXT)');con.executemany('INSERT INTO Name2Id VALUES (?)',[('wxid_me',),('friend',)])
        con.execute(f'CREATE TABLE [{table}](local_id INTEGER,real_sender_id INTEGER,local_type INTEGER,create_time INTEGER,message_content BLOB)')
        for i in range(5):
            con.execute(f'INSERT INTO [{table}] VALUES (?,?,?,?,?)',(i,1 if i%2 else 2,1,100+2*i+shard,zstandard.ZstdCompressor().compress(f"测试{i}分片{shard}".encode())))
        con.commit();con.close();rel=f'message/message_{shard}.db';w.cache[rel]=(None,path);w.tables[rel]={table,'Name2Id'}
    first=w.messages(user,0,3);second=w.messages(user,3,3)
    assert first['total']==10 and len(first['items'])==3
    assert not set(m['id'] for m in first['items'])&set(m['id'] for m in second['items'])
    assert [m['timestamp'] for m in first['items']]==[107,108,109]
    assert w.messages(user,0,100,'测试2')['total']==2
    assert w.messages(user,0,100,start=104,end=106)['total']==3
    assert any(m['is_me'] for m in w.messages(user,0,100)['items'])

def test_api_token_and_origin_checks():
    from fastapi.testclient import TestClient
    from helper.server import app,TOKEN
    client=TestClient(app)
    assert client.get('/api/status').status_code==401
    assert client.get('/api/status',headers={'X-Helper-Token':TOKEN}).status_code==200
    assert client.post('/api/ai/test',headers={'X-Helper-Token':TOKEN,'Origin':'https://evil.example'}).status_code==403

def test_image_decoding_validates_v2_and_old_xor():
    from helper.media import decode_dat, mime
    from Crypto.Util.Padding import pad
    image=b'\xff\xd8\xff'+b'example image data'+b'\xff\xd9'
    encoded=bytes(v^0x65 for v in image)
    assert decode_dat(encoded)[0]==image
    key=b'1234567890abcdef';size=len(image)
    data=b'\x07\x08V2\x08\x07'+struct.pack('<II',size,0)+b'\x00'+AES.new(key,AES.MODE_ECB).encrypt(pad(image,16))
    assert decode_dat(data,key)[0]==image
    with pytest.raises(ValueError):decode_dat(data)
    with pytest.raises(ValueError):decode_dat(b'')
    assert mime(b'RIFF1234WAVE') is None
    assert mime(b'RIFF1234WEBP') == 'image/webp'


def test_image_resolution_uses_selected_account_and_prefers_original(tmp_path, monkeypatch):
    from helper import media
    monkeypatch.setattr(media, 'read_json', lambda *args: {})
    readers = []
    image_hash = 'a' * 32
    table = 'Msg_' + hashlib.md5(b'friend').hexdigest()
    for account, suffix in [('one', b'one'), ('two', b'two')]:
        reader = Wechat()
        reader.root = tmp_path / account / 'db_storage'
        reader.root.mkdir(parents=True)
        db = reader.root / 'message.db'
        with sqlite3.connect(db) as con:
            con.execute(f'CREATE TABLE [{table}](local_id INTEGER,local_type INTEGER,message_content TEXT,server_id INTEGER,create_time INTEGER)')
            con.execute(f'INSERT INTO [{table}] VALUES (1,3,?,10,100)', (image_hash,))
        reader.cache['message/message_0.db'] = (None, db)
        reader.tables['message/message_0.db'] = {table}
        folder = reader.root.parent / 'msg' / 'attach' / hashlib.md5(b'friend').hexdigest() / '2026-10' / 'Img'
        folder.mkdir(parents=True)
        for name, value in [(image_hash + '.dat', suffix), (image_hash + '_t.dat', b'thumbnail')]:
            image = b'\xff\xd8\xff' + value + b'\xff\xd9'
            (folder / name).write_bytes(bytes(v ^ 0x65 for v in image))
        readers.append(reader)
    assert media.image_for('friend', 1, 'message/message_0.db', readers[0])[0] == b'\xff\xd8\xffone\xff\xd9'
    assert media.image_for('friend', 1, 'message/message_0.db', readers[1])[0] == b'\xff\xd8\xfftwo\xff\xd9'


def test_media_xml_metadata():
    _, _, image = parse_content('<msg><img length="123" width="640" height="480"/></msg>', 3)
    assert image['img_length'] == 123 and image['img_width'] == 640
    _, _, video = parse_content('<msg><videomsg length="456" playlength="12" width="oops"/></msg>', 43)
    assert video['videomsg_playlength'] == 12 and 'videomsg_width' not in video
    _, _, attachment = parse_content('<msg><appmsg><title>报告</title><appattach><totallen>50</totallen><fileext>pdf</fileext></appattach></appmsg></msg>', 49)
    assert attachment['appattach/totallen'] == '50' and attachment['appattach/fileext'] == 'pdf'


def test_image_key_derivation_is_verified_against_account_samples(tmp_path, monkeypatch):
    from helper import media, storage
    from Crypto.Util.Padding import pad
    monkeypatch.setattr(storage, 'DATA', tmp_path/'data')
    storage.DATA.mkdir()
    monkeypatch.setenv('APPDATA', str(tmp_path/'appdata'))
    kv = tmp_path/'appdata'/'Tencent'/'xwechat'/'net'/'kvcomm'; kv.mkdir(parents=True)
    (kv/'key_1234567_test.statistic').write_bytes(b'')
    w = Wechat(); w.root = tmp_path/'wxid_sample_abcd'/'db_storage'; w.root.mkdir(parents=True)
    folder = w.root.parent/'msg'/'attach'/'a'/'2026-10'/'Img'; folder.mkdir(parents=True)
    key = hashlib.md5(b'1234567wxid_sample').hexdigest()[:16].encode()
    for n in range(2):
        data = b'\xff\xd8\xffsample'+bytes([n])+b'\xff\xd9'
        header = b'\x07\x08V2\x08\x07'+struct.pack('<II',len(data),0)+b'\x00'
        (folder/f'{n}_t.dat').write_bytes(header+AES.new(key,AES.MODE_ECB).encrypt(pad(data,16)))
    assert media.find_image_key(w) == {'ok':True}
    assert storage.read_json('image-keys.bin', {}, True)[str(w.root)] == key.hex()
    assert storage.read_json('image-xor-keys.bin', {}, True)[str(w.root)] == (1234567 & 255)


def test_empty_workspace_skips_message_snapshots(tmp_path, monkeypatch):
    from helper import storage
    monkeypatch.setattr(storage, 'DATA', tmp_path/'data')
    monkeypatch.setattr('helper.wechat.DATA', tmp_path/'data')
    storage.DATA.mkdir()
    root=tmp_path/'wxid_account'/'db_storage'
    for folder in ('contact','session','message'):(root/folder).mkdir(parents=True)
    with sqlite3.connect(root/'contact'/'contact.db') as con:
        con.execute('CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT)')
        con.execute("INSERT INTO contact VALUES ('friend','朋友','')")
    with sqlite3.connect(root/'session'/'session.db') as con:
        con.execute('CREATE TABLE SessionTable(username TEXT,unread_count INTEGER,summary TEXT,last_timestamp INTEGER)')
        con.execute("INSERT INTO SessionTable VALUES ('friend',0,'预览',100)")
    table='Msg_'+hashlib.md5(b'friend').hexdigest()
    with sqlite3.connect(root/'message'/'message_0.db') as con:
        con.execute('CREATE TABLE Name2Id(user_name TEXT)')
        con.execute("INSERT INTO Name2Id VALUES ('friend')")
        con.execute(f'CREATE TABLE [{table}](local_id INTEGER,real_sender_id INTEGER,local_type INTEGER,create_time INTEGER,message_content TEXT)')
        con.execute(f"INSERT INTO [{table}] VALUES (1,1,1,100,'已选择的消息')")
    w=Wechat();w.root=root;w.sync()
    assert set(w.cache)=={'contact/contact.db','session/session.db'}
    assert w.sessions()[0]['id']=='friend' and w.workspace_sessions()==[]
    assert not list((storage.DATA/'cache').rglob('message_0.db'))
    w.update_workspace('friend',True);w.sync()
    assert 'message/message_0.db' in w.cache
    assert w.messages('friend')['items'][0]['text']=='已选择的消息'
    w.update_workspace('friend',False);w.sync()
    assert set(w.cache)=={'contact/contact.db','session/session.db'}
