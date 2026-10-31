from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from .models import Message, Session


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Login(Strict):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class UserCreate(Strict):
    username: str = Field(pattern=r'^[a-zA-Z0-9_.@-]{3,80}$')
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=12, max_length=256)
    role: Literal['user', 'admin'] = 'user'


class UserUpdate(Strict):
    enabled: bool | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    password: str | None = Field(default=None, min_length=12, max_length=256)


class PasswordChange(Strict):
    current_password: str = Field(max_length=256)
    password: str = Field(min_length=12, max_length=256)


class Name(Strict):
    name: str = Field(min_length=1, max_length=120)


class Enabled(Strict):
    enabled: bool | None = None


class Registration(Strict):
    app_key: str = Field(min_length=10, max_length=64)
    app_secret: str = Field(min_length=32, max_length=128)
    device_id: str = Field(min_length=16, max_length=256)
    version: str = Field(default='2.0.9', max_length=80)


class Envelope(Strict):
    batch_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    nonce: str = Field(max_length=24)
    ciphertext: str = Field(max_length=7_000_000)
    tag: str = Field(max_length=32)


class MediaPacket(Strict):
    action: Literal['chunk', 'commit']
    sent_at: int = Field(ge=0)
    source: str = Field(pattern=r'^[a-f0-9]{64}$')
    session: str = Field(min_length=1, max_length=256)
    database: str = Field(min_length=1, max_length=512)
    local_id: int = Field(ge=0)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    size: int = Field(ge=1, le=104857600)
    name: str = Field(min_length=1, max_length=255)
    mime: str = Field(min_length=1, max_length=100)
    part: int = Field(default=0, ge=0, le=199)
    data: str = Field(default='', max_length=700000)


class Contact(Strict):
    id: str = Field(min_length=1, max_length=256)
    name: str = Field(max_length=2000)
    nickname: str = Field(default='', max_length=2000)
    remark: str = Field(default='', max_length=2000)
    group: bool = False


class SyncPayload(Strict):
    kind: Literal['manifest', 'messages', 'history']
    sent_at: int = Field(ge=0)
    source: str = Field(pattern=r'^[a-f0-9]{64}$')
    account_name: str = Field(default='', max_length=2000)
    self_id: str = Field(default='', max_length=256)
    sessions: list[Session] = Field(default_factory=list, max_length=500)
    contacts: list[Contact] = Field(default_factory=list, max_length=500)
    session: str = Field(default='', max_length=256)
    items: list[Message] = Field(default_factory=list, max_length=500)
    history_complete: bool = False


class Listener(Strict):
    enabled: bool
    cycles: list[Literal['day', 'week', 'month', 'halfyear', 'year']] = Field(default_factory=lambda: ['day', 'week', 'month', 'halfyear', 'year'], min_length=1, max_length=5)


class ModelConfig(Strict):
    base_url: str = Field(min_length=1, max_length=2000)
    model: str = Field(min_length=1, max_length=200)
    api_key: str = Field(default='', max_length=4096)
    temperature: float = Field(default=0.3, ge=0, le=2)
    max_tokens: int = Field(default=2000, ge=256, le=131072)
    analysis_max_tokens: int = Field(default=2048, ge=256, le=131072)
    thinking_mode: Literal['default', 'enabled', 'disabled'] = 'default'
    thinking_budget: int | None = Field(default=None, ge=0, le=262144)
    daily_call_limit: int = Field(default=100, ge=1, le=5000)
    media_analysis: bool = False
    audio_model: str = Field(default='', max_length=200)
    enabled: bool = True


class KeyCreate(Name):
    permission: Literal['read', 'full']
    sessions: list[str] = Field(default_factory=list, max_length=500)
    profile: bool = False
    expires_days: int = Field(default=90, ge=1, le=365)

    @field_validator('permission', mode='before')
    @classmethod
    def legacy_write_permission(cls, value):
        # Existing API clients may still submit the old write-only value.
        return 'full' if value == 'write' else value


class MemoryEdit(Strict):
    version: int = Field(ge=1)
    markdown: str = Field(min_length=1, max_length=200000)


class Suggestion(Strict):
    session_id: str = Field(min_length=1, max_length=64)
    intent: str = Field(default='自然地继续对话', max_length=2000)
