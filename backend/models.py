from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Session(StrictModel):
    id: str = Field(min_length=1, max_length=256)
    name: str = Field(max_length=2000)
    note: str = Field(default='', max_length=2000)
    preview: str = Field(default='', max_length=100000)
    timestamp: int = Field(default=0, ge=0)
    group: bool = False
    context: dict[str, Any] = Field(default_factory=dict)


class Workspace(StrictModel):
    source: str = Field(pattern=r'^[a-f0-9]{64}$')
    sessions: list[Session] = Field(max_length=10000)


class Message(StrictModel):
    id: str = Field(min_length=1, max_length=1024)
    database: str = Field(min_length=1, max_length=512)
    local_id: int = Field(ge=0)
    server_id: int | str | None = None
    timestamp: int = Field(ge=0)
    type: int = 1
    type_name: str = Field(default='文本', max_length=100)
    sender_id: str = Field(default='', max_length=256)
    sender: str = Field(default='', max_length=2000)
    is_me: bool = False
    text: str = Field(default='', max_length=1000000)
    raw: str = Field(default='', max_length=1000000)
    extra: dict[str, Any] = Field(default_factory=dict)


class MessageBatch(StrictModel):
    revision: str = Field(min_length=1, max_length=64)
    user: str = Field(min_length=1, max_length=256)
    items: list[Message] = Field(min_length=1, max_length=500)
