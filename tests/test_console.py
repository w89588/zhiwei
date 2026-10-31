from test_server import gateway, upload, UPLOAD, READ, OTHER_READ, MANIFEST


def test_console_shell_is_public_but_contains_no_credentials(gateway):
    for path, mime in [('/', 'text/html'), ('/console/style.css', 'text/css'), ('/console/app.js', 'text/javascript')]:
        response = gateway.get(path)
        assert response.status_code == 200
        assert response.headers['content-type'].startswith(mime)
        assert response.headers['cache-control'] == 'no-store'
        assert "frame-ancestors 'none'" in response.headers['content-security-policy']
        assert 'r'*32 not in response.text and 'u'*32 not in response.text
    assert '服务端管理' in gateway.get('/').text
    assert gateway.get('/console/../store.py').status_code == 401
    assert gateway.get('/api/console/overview').status_code == 401
    assert gateway.get('/api/console/overview', headers=UPLOAD).status_code == 401


def test_console_overview_scoped_to_active_sessions(gateway):
    upload(gateway)
    response = gateway.get('/api/console/overview', headers=READ)
    assert response.status_code == 200
    result = response.json()
    assert result['account'] == 'first' and result['workspace_count'] == 1
    assert result['message_count'] == 6 and result['first_message'] == 100 and result['last_message'] == 105
    assert result['updated_at'] and result['checked_at']
    other = gateway.get('/api/console/overview', headers=OTHER_READ).json()
    assert other['account'] == 'second' and other['message_count'] == 0 and other['state'] == 'idle'
    assert 'source' not in result and 'revision' not in result
    gateway.post('/api/v1/client/workspace', headers=UPLOAD, json={**MANIFEST, 'sessions': []})
    result = gateway.get('/api/console/overview', headers=READ).json()
    assert result['workspace_count'] == 0 and result['message_count'] == 0 and result['first_message'] is None


def test_console_allows_own_origin_and_rejects_cross_origin(gateway):
    assert gateway.get('/api/console/overview', headers={**READ, 'Origin': 'http://testserver'}).status_code == 200
    for origin in ['https://evil.example', 'http://testserver.evil.example', 'null', 'https://testserver']:
        assert gateway.get('/api/console/overview', headers={**READ, 'Origin': origin}).status_code == 403
    # The public shell never grants a cookie or ambient access to data.
    assert 'set-cookie' not in gateway.get('/').headers
    assert gateway.get('/api/external/sessions').status_code == 401
