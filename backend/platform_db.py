"""Versioned platform schema. MySQL in WSLC; SQLite is only a test adapter."""
from contextlib import contextmanager
import hashlib

from sqlalchemy import (BigInteger, Boolean, Column, Index, Integer, MetaData, String,
                        Table, Text, UniqueConstraint, create_engine, select, text)
from sqlalchemy.dialects.mysql import LONGTEXT
from .migrations import CompatibilityError, Migration, MigrationError, check_engine_version, run_sql_migrations

metadata = MetaData()
text_type = Text().with_variant(LONGTEXT(), 'mysql')


def table(name, *columns, **kw):
    return Table('v2_' + name, metadata, *columns, mysql_charset='utf8mb4', **kw)


users = table('users', Column('id', String(32), primary_key=True),
    Column('username', String(80), unique=True, nullable=False), Column('name', String(120), nullable=False),
    Column('password', String(256), nullable=False), Column('role', String(16), nullable=False),
    Column('enabled', Boolean, nullable=False), Column('must_change', Boolean, nullable=False),
    Column('epoch', Integer, nullable=False, default=1), Column('created_at', BigInteger, nullable=False))
tokens = table('tokens', Column('hash', String(64), primary_key=True), Column('user_id', String(32), nullable=False, index=True),
    Column('kind', String(16), nullable=False), Column('subject', String(64), nullable=False),
    Column('epoch', Integer, nullable=False), Column('expires', BigInteger, nullable=False))
clients = table('clients', Column('id', String(32), primary_key=True), Column('user_id', String(32), nullable=False, index=True),
    Column('name', String(120), nullable=False), Column('app_key', String(64), unique=True, nullable=False),
    Column('secret_hash', String(64), nullable=False), Column('transport_key', text_type, nullable=False),
    Column('device_hash', String(64), nullable=False, default=''), Column('enabled', Boolean, nullable=False),
    Column('version', String(80), nullable=False, default=''), Column('last_seen', BigInteger, nullable=False, default=0),
    Column('created_at', BigInteger, nullable=False))
accounts = table('accounts', Column('id', String(32), primary_key=True), Column('user_id', String(32), nullable=False, index=True),
    Column('source', String(64), nullable=False), Column('client_id', String(32), nullable=False),
    Column('payload', text_type, nullable=False), Column('updated_at', BigInteger, nullable=False),
    UniqueConstraint('user_id', 'source'))
contacts = table('contacts', Column('id', String(64), primary_key=True), Column('user_id', String(32), nullable=False, index=True),
    Column('account_id', String(32), nullable=False, index=True), Column('payload', text_type, nullable=False),
    Column('updated_at', BigInteger, nullable=False))
sessions = table('sessions', Column('id', String(64), primary_key=True), Column('user_id', String(32), nullable=False, index=True),
    Column('account_id', String(32), nullable=False, index=True), Column('payload', text_type, nullable=False),
    Column('listening', Boolean, nullable=False, default=False), Column('listen_epoch', Integer, nullable=False, default=0),
    Column('cycles', String(100), nullable=False, default='day,week,month,halfyear,year'),
    Column('updated_at', BigInteger, nullable=False))
messages = table('messages', Column('id', String(64), primary_key=True), Column('user_id', String(32), nullable=False, index=True),
    Column('session_id', String(64), nullable=False), Column('client_id', String(32), nullable=False),
    Column('database_hash', String(64), nullable=False), Column('local_id', BigInteger, nullable=False),
    Column('timestamp', BigInteger, nullable=False), Column('fingerprint', String(64), nullable=False),
    Column('payload', text_type, nullable=False), Column('updated_at', BigInteger, nullable=False))
Index('v2_message_page', messages.c.user_id, messages.c.session_id, messages.c.timestamp, messages.c.id)
Index('v2_message_cursor', messages.c.client_id, messages.c.session_id, messages.c.database_hash, messages.c.local_id)
media_objects = table('media_objects', Column('id', String(64), primary_key=True),
    Column('user_id', String(32), nullable=False, index=True), Column('group_id', String(64), nullable=False),
    Column('message_key', String(64), nullable=False), Column('state', String(16), nullable=False),
    Column('payload', text_type, nullable=False), Column('created_at', BigInteger, nullable=False),
    Column('updated_at', BigInteger, nullable=False))
Index('v2_media_message', media_objects.c.user_id, media_objects.c.group_id, media_objects.c.message_key, media_objects.c.state)
Index('v2_media_cleanup', media_objects.c.user_id, media_objects.c.state, media_objects.c.updated_at)
media_chunks = table('media_chunks', Column('id', String(64), primary_key=True),
    Column('object_id', String(64), nullable=False, index=True), Column('part', Integer, nullable=False),
    Column('payload', text_type, nullable=False), UniqueConstraint('object_id', 'part'))
receipts = table('receipts', Column('id', String(64), primary_key=True), Column('client_id', String(32), nullable=False),
    Column('digest', String(64), nullable=False), Column('result', text_type, nullable=False), Column('created_at', BigInteger, nullable=False))
models = table('models', Column('user_id', String(32), primary_key=True), Column('payload', text_type, nullable=False),
    Column('revision', Integer, nullable=False), Column('updated_at', BigInteger, nullable=False))
jobs = table('jobs', Column('id', String(64), primary_key=True), Column('user_id', String(32), nullable=False, index=True),
    Column('session_id', String(64), nullable=False), Column('cycle', String(16), nullable=False),
    Column('period_start', BigInteger, nullable=False), Column('period_end', BigInteger, nullable=False),
    Column('state', String(32), nullable=False), Column('revision', Integer, nullable=False, default=1),
    Column('attempts', Integer, nullable=False, default=0), Column('lease', String(32), nullable=False, default=''),
    Column('lease_until', BigInteger, nullable=False, default=0), Column('next_run', BigInteger, nullable=False, default=0),
    Column('error', String(300), nullable=False, default=''), Column('updated_at', BigInteger, nullable=False))
Index('v2_job_queue', jobs.c.state, jobs.c.next_run)
history_sync = table('history_sync', Column('session_id', String(64), primary_key=True),
    Column('user_id', String(32), nullable=False, index=True), Column('complete', Boolean, nullable=False),
    Column('updated_at', BigInteger, nullable=False))
logical_sessions = table('logical_sessions', Column('id', String(64), primary_key=True),
    Column('user_id', String(32), nullable=False, index=True), Column('owner_id', String(64), nullable=False, unique=True))
message_pool = table('message_pool', Column('group_id', String(64), primary_key=True),
    Column('message_key', String(64), primary_key=True), Column('user_id', String(32), nullable=False, index=True),
    Column('message_id', String(64), nullable=False, unique=True))
memories = table('memories', Column('id', String(64), primary_key=True), Column('user_id', String(32), nullable=False, index=True),
    Column('session_id', String(64), nullable=False), Column('kind', String(20), nullable=False),
    Column('period_start', BigInteger, nullable=False, default=0), Column('period_end', BigInteger, nullable=False, default=0),
    Column('version', Integer, nullable=False), Column('blob_id', String(64), nullable=False),
    Column('manual', Boolean, nullable=False), Column('updated_at', BigInteger, nullable=False))
versions = table('versions', Column('blob_id', String(64), primary_key=True), Column('memory_id', String(64), nullable=False, index=True),
    Column('user_id', String(32), nullable=False), Column('version', Integer, nullable=False),
    Column('manual', Boolean, nullable=False), Column('created_at', BigInteger, nullable=False),
    UniqueConstraint('memory_id', 'version'))
mcp_keys = table('mcp_keys', Column('id', String(32), primary_key=True), Column('user_id', String(32), nullable=False, index=True),
    Column('name', String(120), nullable=False), Column('hash', String(64), unique=True, nullable=False),
    Column('prefix', String(16), nullable=False), Column('permission', String(16), nullable=False),
    Column('scopes', text_type, nullable=False), Column('enabled', Boolean, nullable=False),
    Column('expires', BigInteger, nullable=False), Column('last_used', BigInteger, nullable=False, default=0),
    Column('created_at', BigInteger, nullable=False))
audit = table('audit', Column('id', String(32), primary_key=True), Column('user_id', String(32), nullable=False, index=True),
    Column('action', String(80), nullable=False), Column('target', String(128), nullable=False),
    Column('created_at', BigInteger, nullable=False))
usage = table('usage', Column('id', String(32), primary_key=True), Column('user_id', String(32), nullable=False, index=True),
    Column('job_id', String(64), nullable=False), Column('input_tokens', Integer, nullable=False),
    Column('output_tokens', Integer, nullable=False), Column('created_at', BigInteger, nullable=False))
model_calls = table('model_calls', Column('id', String(32), primary_key=True),
    Column('user_id', String(32), nullable=False, index=True), Column('job_id', String(64), nullable=False, index=True),
    Column('stage_key', String(64), nullable=False, index=True), Column('model', String(200), nullable=False),
    Column('state', String(24), nullable=False), Column('error_code', String(40), nullable=False, default=''),
    Column('http_status', Integer, nullable=False, default=0), Column('request_id', String(128), nullable=False, default=''),
    Column('started_at', BigInteger, nullable=False), Column('duration_ms', BigInteger, nullable=False, default=0),
    Column('input_tokens', BigInteger, nullable=True), Column('output_tokens', BigInteger, nullable=True),
    Column('reasoning_tokens', BigInteger, nullable=True))
model_stages = table('model_stages', Column('id', String(64), primary_key=True),
    Column('user_id', String(32), nullable=False, index=True), Column('job_id', String(64), nullable=False, index=True),
    Column('payload', text_type, nullable=False), Column('created_at', BigInteger, nullable=False))
schema = table('schema', Column('version', Integer, primary_key=True), Column('applied_at', BigInteger, nullable=False))
migration_runs = table('migration_runs', Column('version', Integer, primary_key=True),
    Column('name', String(120), nullable=False), Column('state', String(16), nullable=False),
    Column('started_at', BigInteger, nullable=False), Column('finished_at', BigInteger, nullable=False))


def upgrade_mcp_permissions(con):
    con.execute(mcp_keys.update().where(mcp_keys.c.permission == 'write').values(permission='full'))


def client_source(client_id, source):
    return hashlib.sha256(f'{client_id}:{source}'.encode()).hexdigest()


def scope_existing_accounts(con):
    # Preserve account/session IDs and encrypted data while assigning each legacy
    # source to its last recorded client. Previously overwritten data cannot be recovered.
    for account in con.execute(select(accounts.c.id, accounts.c.client_id, accounts.c.source)).all():
        con.execute(accounts.update().where(accounts.c.id == account.id).values(
            source=client_source(account.client_id, account.source)))


def add_history_sync(con):
    history_sync.create(con, checkfirst=True)
    con.execute(jobs.update().where(jobs.c.state.in_(['queued', 'running', 'waiting_model'])).values(
        state='waiting_history', lease='', lease_until=0, error='等待客户端确认历史消息同步完成'))


SQL_MIGRATIONS = (
    Migration(1, 'initial_platform_tables', lambda con: metadata.create_all(con), retry_safe=True),
    Migration(2, 'mcp_full_permissions', upgrade_mcp_permissions, retry_safe=True),
    Migration(3, 'scope_account_sources_to_clients', scope_existing_accounts),
    Migration(4, 'model_attempts_and_checkpoints',
        lambda con: (model_calls.create(con, checkfirst=True), model_stages.create(con, checkfirst=True)), retry_safe=True),
    Migration(5, 'session_history_sync', add_history_sync, retry_safe=True),
    Migration(6, 'logical_session_message_pool',
        lambda con: (logical_sessions.create(con, checkfirst=True), message_pool.create(con, checkfirst=True)), retry_safe=True),
    Migration(7, 'encrypted_media_chunks',
        lambda con: (media_objects.create(con, checkfirst=True), media_chunks.create(con, checkfirst=True)), retry_safe=True),
)


class Database:
    def __init__(self, url, *, initialize=True, lock_timeout=60):
        options = {'pool_pre_ping': True, 'hide_parameters': True}
        if url.startswith('sqlite'):
            options['connect_args'] = {'check_same_thread': False, 'timeout': 30}
        self.engine = create_engine(url, **options)
        self.lock_timeout, self.schema_version = lock_timeout, 0
        try:
            with self.engine.connect() as con:
                if con.dialect.name == 'mysql':
                    self.server_version = check_engine_version('mysql', con.execute(text('SELECT VERSION()')).scalar_one())
                elif con.dialect.name == 'sqlite':
                    self.server_version = con.execute(text('SELECT sqlite_version()')).scalar_one()
                else:
                    raise CompatibilityError('服务端仅支持 MySQL；SQLite 仅供自动化测试使用')
            if initialize:
                self.migrate()
        except Exception:
            self.engine.dispose()
            raise

    @contextmanager
    def migration_lock(self):
        with self.engine.connect() as con:
            mysql = con.dialect.name == 'mysql'
            lock_name = 'wechat-schema:' + hashlib.sha256((self.engine.url.database or '').encode()).hexdigest()[:40]
            if mysql:
                acquired = con.execute(text('SELECT GET_LOCK(:name, :timeout)'), {'name':lock_name, 'timeout':self.lock_timeout}).scalar_one()
                con.commit()
                if acquired != 1:
                    raise MigrationError('等待数据库迁移锁超时，请检查其他服务实例')
            else:
                con.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                yield con
                con.commit()
            except Exception:
                con.rollback()
                raise
            finally:
                if mysql:
                    try:
                        con.execute(text('SELECT RELEASE_LOCK(:name)'), {'name':lock_name})
                        con.commit()
                    except Exception:
                        con.invalidate()

    def migrate(self):
        with self.migration_lock() as con:
            self.schema_version = run_sql_migrations(con, schema, migration_runs, SQL_MIGRATIONS)
        return self.schema_version

    @contextmanager
    def transaction(self):
        with self.engine.begin() as con:
            yield con


def row(con, table_, *conditions, lock=False):
    query = select(table_).where(*conditions)
    if lock:
        query = query.with_for_update()
    result = con.execute(query).mappings().first()
    return dict(result) if result else None


def rows(con, table_, *conditions, limit=500, offset=0, order=None):
    query = select(table_).where(*conditions)
    if order is not None:
        query = query.order_by(order)
    return [dict(r) for r in con.execute(query.limit(limit).offset(offset)).mappings()]
