import base64
import hashlib
import json

from sqlalchemy import select

from backend.platform_db import media_chunks, media_objects
from helper.platform_sync import envelope
from test_platform import UI, device, key, mcp, platform, seed, upload, user
from test_integration import desktop


def test_emoji_is_fetched_once_cached_and_available_to_authorized_mcp(platform, monkeypatch):
    import httpx
    alice, _ = user(platform)
    bob, _ = user(platform, 'bob')
    config = device(alice)
    sid, _ = seed(alice, config)
    gif = b'GIF89a' + b'synthetic emoji'
    md5 = hashlib.md5(gif).hexdigest()
    raw = f'<msg><emoji md5="{md5}" cdnurl="http://mmbiz.qpic.cn/mmemoticon/test"/></msg>'
    message = {'id':'message/message_0.db:88', 'database':'message/message_0.db', 'local_id':88,
               'timestamp':1704067288, 'type':47, 'text':'[表情]', 'raw':raw}
    assert upload(alice, config, {'kind':'messages','source':'a'*64,'session':'friend','items':[message]}).status_code == 200
    original_client = httpx.Client
    requests = []
    def handle(request):
        requests.append(str(request.url))
        return httpx.Response(200, content=gif)
    monkeypatch.setattr('backend.emoji_media.httpx.Client',
                        lambda **kw: original_client(transport=httpx.MockTransport(handle), **kw))
    item = next(x for x in alice.get(f'/api/v2/sessions/{sid}/messages').json()['items'] if x['local_id'] == 88)
    url = '/api/v2/messages/'+item['evidence_id']+'/media'
    assert bob.get(url).status_code == 404 and not requests
    response = alice.get(url)
    assert response.status_code == 200 and response.content == gif
    assert response.headers['content-type'] == 'image/gif' and response.headers['content-disposition'].startswith('inline')
    assert requests == ['https://mmbiz.qpic.cn/mmemoticon/test']
    assert alice.get('/api/v2/messages/'+item['evidence_id']).json()['media']['kind'] == 47
    assert alice.post(f'/api/v2/sessions/{sid}/listener',headers=UI,json={'enabled':True}).status_code == 200
    token = key(alice, sessions=[sid])['key']
    result = mcp(alice, token, 'get_media', {'evidence_id':item['evidence_id']}).json()['result']
    value = result.get('structuredContent') or json.loads(result['content'][0]['text'])
    assert base64.b64decode(value['data']) == gif and len(requests) == 1


def test_emoji_rejects_untrusted_addresses_and_checksum_mismatch(monkeypatch):
    import httpx
    import pytest
    from backend.emoji_media import fetch_emoji
    client = httpx.Client
    calls = []
    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=b'GIF89a' + b'wrong')
    monkeypatch.setattr('backend.emoji_media.httpx.Client',
                        lambda **kw: client(transport=httpx.MockTransport(handle), **kw))
    expected = 'a' * 32
    for url in ('http://127.0.0.1/private', 'https://mmbiz.qpic.cn.evil.test/x',
                'https://user@mmbiz.qpic.cn/x', 'https://mmbiz.qpic.cn:8443/x'):
        with pytest.raises(ValueError):
            fetch_emoji(f'<msg><emoji md5="{expected}" cdnurl="{url}"/></msg>')
    assert not calls
    with pytest.raises(ValueError):
        fetch_emoji(f'<msg><emoji md5="{expected}" cdnurl="https://mmbiz.qpic.cn/x"/></msg>')
    assert len(calls) == 1


def test_emoji_retries_transient_network_error(monkeypatch):
    import httpx
    from backend.emoji_media import fetch_emoji
    gif = b'GIF89a' + b'test'
    client, calls = httpx.Client, []
    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError('temporary failure', request=request)
        return httpx.Response(200, content=gif)
    monkeypatch.setattr('backend.emoji_media.httpx.Client',
                        lambda **kw: client(transport=httpx.MockTransport(handle), **kw))
    raw = f'<msg><emoji md5="{hashlib.md5(gif).hexdigest()}" cdnurl="https://emoji.qpic.cn/x"/></msg>'
    assert fetch_emoji(raw) == (gif, 'image/gif') and len(calls) == 2


def test_spreadsheet_text_extracts_shared_inline_and_numeric_cells():
    import io
    import zipfile
    from backend.media_content import document_text
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('xl/sharedStrings.xml', '<sst xmlns="urn:test"><si><t>项目</t></si></sst>')
        archive.writestr('xl/worksheets/sheet1.xml', '<worksheet xmlns="urn:test"><row>'
            '<c t="s"><v>0</v></c><c t="inlineStr"><is><t>完成</t></is></c><c><v>42</v></c>'
            '</row></worksheet>')
    assert document_text('test.xlsx', data.getvalue()) == '项目\t完成\t42'


def media_upload(client, config, packet):
    return client.post('/api/v2/client/media', headers={'Authorization':'Bearer '+config['token']},
                       json=envelope(config, packet))


def test_encrypted_media_roundtrip_scope_retries_and_mcp(platform):
    from backend.platform_media import CHUNK_SIZE
    alice, user_id = user(platform)
    bob, _ = user(platform, 'bob')
    _, service, _ = platform
    config = device(alice)
    sid, _ = seed(alice, config)
    image = b'\xff\xd8\xff' + b'synthetic image' * 40000 + b'\xff\xd9'
    message = {'id':'message/message_0.db:9','database':'message/message_0.db','local_id':9,
               'server_id':990,'timestamp':1704067209,'type':3,'type_name':'图片','text':'[图片]'}
    batch = {'kind':'messages','source':'a'*64,'session':'friend','items':[message]}
    assert upload(alice, config, batch).status_code == 200
    packet = {'source':'a'*64,'session':'friend','database':message['database'],'local_id':9,
              'sha256':hashlib.sha256(image).hexdigest(),'size':len(image),'name':'image.jpg','mime':'image/jpeg'}
    commit = media_upload(alice, config, {**packet,'action':'commit'})
    assert commit.status_code == 409
    for part, start in enumerate(range(0, len(image), CHUNK_SIZE)):
        payload = {**packet, 'action':'chunk', 'part':part,
                   'data':base64.b64encode(image[start:start+CHUNK_SIZE]).decode('ascii')}
        assert media_upload(alice, config, payload).status_code == 200
        assert media_upload(alice, config, payload).status_code == 200
    assert media_upload(alice, config, {**packet,'action':'commit'}).json()['complete']
    items = alice.get(f'/api/v2/sessions/{sid}/messages').json()['items']
    source = next(item for item in items if item['local_id'] == 9)
    assert source['media']['size'] == len(image)
    path = '/api/v2/messages/'+source['evidence_id']+'/media'
    response = alice.get(path)
    assert response.status_code == 200 and response.content == image and response.headers['content-type'] == 'image/jpeg'
    partial = alice.get(path, headers={'Range':'bytes=5-19'})
    assert partial.status_code == 206 and partial.content == image[5:20]
    assert partial.headers['content-range'] == f'bytes 5-19/{len(image)}'
    assert bob.get(path).status_code == 404
    assert alice.post(f'/api/v2/sessions/{sid}/listener', headers=UI, json={'enabled':True}).status_code == 200
    token = key(alice, sessions=[sid])['key']
    result = mcp(alice, token, 'get_media', {'evidence_id':source['evidence_id'], 'offset':0, 'limit':100}).json()['result']
    assert not result.get('isError')
    value = result.get('structuredContent') or json.loads(result['content'][0]['text'])
    assert base64.b64decode(value['data']) == image[:100] and value['next_offset'] == 100
    with service.db.transaction() as con:
        objects = list(con.execute(select(media_objects).where(media_objects.c.user_id == user_id)).mappings())
        chunks = list(con.execute(select(media_chunks)).mappings())
        assert objects and chunks and all('synthetic image' not in c['payload'] for c in chunks)


def test_media_rejects_wrong_source_and_unverified_content(platform):
    alice, _ = user(platform)
    config = device(alice)
    seed(alice, config)
    blob = b'not an image'
    packet = {'source':'a'*64,'session':'friend','database':'message/message_0.db','local_id':1,
              'sha256':hashlib.sha256(blob).hexdigest(),'size':len(blob),'name':'x.jpg','mime':'image/jpeg',
              'action':'chunk','data':base64.b64encode(blob).decode()}
    assert media_upload(alice, config, packet).status_code == 422  # text message cannot carry media


def test_worker_analyzes_image_and_silk_with_configured_models(platform, monkeypatch):
    import io
    import pysilk
    from backend.platform_jobs import Worker
    from backend.platform_models import ModelConfig
    client, user_id = user(platform)
    _, service, _ = platform
    config = device(client)
    sid, _ = seed(client, config)
    pcm = io.BytesIO(b'\x00\x00' * 24000)
    silk = io.BytesIO(); pysilk.encode(pcm, silk, 24000, 24000)
    entries = [(9, 3, 'image/jpeg', 'image.jpg', b'\xff\xd8\xffsynthetic-image'),
               (10, 34, 'audio/silk', 'voice.silk', silk.getvalue()),
               (11, 43, 'video/mp4', 'video.mp4', b'\x00\x00\x00\x18ftypisomsynthetic-video')]
    for local_id, kind, mime, name, data in entries:
        message = {'id':f'message/message_0.db:{local_id}', 'database':'message/message_0.db',
                   'local_id':local_id, 'timestamp':1704067200+local_id, 'type':kind, 'text':'[媒体]', 'sender':'朋友'}
        assert upload(client, config, {'kind':'messages','source':'a'*64,'session':'friend','items':[message]}).status_code == 200
        packet = {'source':'a'*64,'session':'friend','database':message['database'],'local_id':local_id,
                  'sha256':hashlib.sha256(data).hexdigest(),'size':len(data),'name':name,'mime':mime}
        assert media_upload(client,config,{**packet,'action':'chunk','data':base64.b64encode(data).decode()}).status_code == 200
        assert media_upload(client,config,{**packet,'action':'commit'}).status_code == 200
    items = client.get(f'/api/v2/sessions/{sid}/messages').json()['items']
    voice_id = next(item['evidence_id'] for item in items if item['type'] == 34)
    voice = client.get(f'/api/v2/messages/{voice_id}/media/play')
    assert voice.status_code == 200 and voice.content.startswith(b'RIFF') and voice.headers['content-type'] == 'audio/wav'
    assert client.post(f'/api/v2/sessions/{sid}/listener',headers=UI,json={'enabled':True,'cycles':['day']}).status_code == 200
    monkeypatch.setenv('WECHAT_MODEL_ALLOW_PRIVATE','1')
    service.save_model(user_id,ModelConfig(base_url='http://127.0.0.1:9999/v1',model='visual-model',audio_model='audio-model',media_analysis=True))
    calls = []
    def model(cfg,prompt,purpose,before_call=None):
        if before_call: before_call()
        calls.append((cfg['model'],prompt))
        if '仅输出 JSON' in purpose:
            return '{"relationship":[],"profile":[]}', {}
        return '合成附件或聊天摘要', {}
    monkeypatch.setattr('backend.platform_jobs.call_model',model)
    assert Worker(service).run_once()
    media_calls = [(name,prompt) for name,prompt in calls if isinstance(prompt,list)]
    assert {name for name,_ in media_calls} == {'visual-model','audio-model'}
    assert {prompt[1]['type'] for _,prompt in media_calls} == {'image_url','input_audio','video_url'}
    assert all('base64,' in str(prompt[1]) for _,prompt in media_calls)


def test_client_recent_first_media_backfill_and_resume(desktop, platform, monkeypatch):
    import hashlib
    import sqlite3
    from backend.platform_app import create_app
    from helper import platform_sync
    from helper.platform_sync import PlatformUploader
    from test_incremental_sync import prepare
    from test_server import live_server
    w = prepare(desktop)
    table = 'Msg_' + hashlib.md5(b'friend').hexdigest()
    with sqlite3.connect(w.root/'message/message_0.db') as con:
        con.executemany(f'INSERT INTO [{table}] VALUES (?,?,?,?,?)',
                        [(n,2,3,1728000000+n,'<msg><img/></msg>') for n in range(10,40)])
    monkeypatch.setattr('helper.media_sync.media_for',
        lambda reader, user, database, item: (b'\xff\xd8\xffimage'+str(item['local_id']).encode(), 'image/jpeg', 'image.jpg'))
    uploader = PlatformUploader()
    monkeypatch.setattr(platform_sync, 'platform_uploader', uploader)
    web, _ = user(platform)
    _, service, _ = platform
    credentials = web.post('/api/v2/clients', headers=UI, json={'name':'media test'}).json()
    with live_server(create_app(service,start_worker=False)) as url:
        uploader.configure(w,url,credentials['app_key'],credentials['app_secret'],True,'all')
        result = uploader.sync(w)
        assert result['state'] == 'done'
        sid = web.get('/api/v2/sessions').json()['items'][0]['id']
        items = web.get(f'/api/v2/sessions/{sid}/messages',params={'limit':100}).json()['items']
        ready = {m['local_id'] for m in items if m.get('media')}
        from helper import storage
        progress = storage.read_json('sync-progress.bin', {}, secret=True)
        pending = [s.get('media_pending') for p in progress.values() for s in p['shards'].values() if s.get('media_pending')]
        assert 39 in ready and 10 not in ready, (result, ready, pending)
        # A new uploader instance resumes the independent media cursor.
        uploader = PlatformUploader(); monkeypatch.setattr(platform_sync, 'platform_uploader', uploader)
        for _ in range(15):
            assert uploader.sync(w)['state'] == 'done'
            items = web.get(f'/api/v2/sessions/{sid}/messages',params={'limit':100}).json()['items']
            ready = {m['local_id'] for m in items if m.get('media')}
            if set(range(10,40)) <= ready:
                break
        assert set(range(10,40)) <= ready
