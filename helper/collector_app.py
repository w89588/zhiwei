"""Local-only control surface for the Windows collector."""
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .collector import collector
from .wechat import wechat

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
TOKEN = secrets.token_urlsafe(32)
STATIC = Path(__file__).parent / 'collector_static'


@app.middleware('http')
async def local_only(request: Request, call_next):
    host = request.headers.get('host', '').split(':')[0]
    if host not in ('127.0.0.1', 'localhost', 'testserver'):
        return Response(status_code=403)
    origin = request.headers.get('origin')
    if origin and origin != str(request.base_url).rstrip('/'):
        return Response(status_code=403)
    if request.url.path.startswith('/api/') and not secrets.compare_digest(request.headers.get('x-helper-token', ''), TOKEN):
        return Response(status_code=401)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@app.get('/')
def index():
    return HTMLResponse((STATIC / 'index.html').read_text('utf-8').replace('__TOKEN__', TOKEN))


app.mount('/static', StaticFiles(directory=STATIC), name='static')


class Settings(BaseModel):
    url: str = Field(max_length=2048)
    app_key: str = Field(max_length=64)
    app_secret: str = Field(default='', max_length=128)
    consent: bool = False


class Account(BaseModel):
    path: str = Field(max_length=4096)


@app.get('/api/status')
def status():
    return collector.status()


@app.post('/api/settings')
def settings(body: Settings):
    try:
        return collector.configure(body.url, body.app_key, body.app_secret, body.consent)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@app.post('/api/account')
def account(body: Account):
    try:
        return collector.select_account(body.path)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@app.post('/api/image-key')
def image_key():
    from .media import find_image_key
    if wechat.status.get('state') != 'ready':
        raise HTTPException(409, '请先连接微信账号')
    try:
        with wechat.lock:
            result = find_image_key(wechat)
        collector.changed.set()
        return result
    except (ValueError, OSError) as error:
        raise HTTPException(400, str(error)) from None
