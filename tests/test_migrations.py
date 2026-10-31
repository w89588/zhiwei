from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import inspect, select, text

from backend.migrations import CompatibilityError, Migration, MigrationError, check_engine_version
from backend.platform_db import Database, schema


@pytest.mark.parametrize('engine,version', [('mysql','8.0.40'), ('mysql','8.4.3'), ('mongodb','6.0.22'), ('mongodb','7.0.15'), ('mongodb','8.0.1')])
def test_supported_database_engines(engine, version):
    assert check_engine_version(engine, version) == version


@pytest.mark.parametrize('engine,version', [('mysql','5.7.44'), ('mysql','10.11.8-MariaDB'), ('mysql','10.0.0'), ('mongodb','5.0.31'), ('mongodb','9.0.0'), ('mongodb','unknown')])
def test_unsupported_database_engines_fail_before_migration(engine, version):
    with pytest.raises(CompatibilityError):
        check_engine_version(engine, version)


def test_ordered_schema_and_data_upgrade_is_applied_once(tmp_path, monkeypatch):
    from backend import platform_db
    url = 'sqlite:///' + str(tmp_path/'migration.sqlite3')
    database = Database(url)
    database.engine.dispose()
    def upgrade(con):
        con.execute(text('CREATE TABLE v2_migration_probe (value INTEGER NOT NULL)'))
        con.execute(text('INSERT INTO v2_migration_probe VALUES (7)'))
    monkeypatch.setattr(platform_db, 'SQL_MIGRATIONS', (*platform_db.SQL_MIGRATIONS, Migration(8, 'test_structure_and_data', upgrade)))
    upgraded = Database(url)
    assert upgraded.schema_version == 8
    with upgraded.transaction() as con:
        assert con.execute(text('SELECT value FROM v2_migration_probe')).scalar_one() == 7
    upgraded.engine.dispose()
    repeated = Database(url)
    with repeated.transaction() as con:
        assert con.execute(text('SELECT COUNT(*) FROM v2_migration_probe')).scalar_one() == 1
    repeated.engine.dispose()


def test_future_schema_fails_closed_and_failed_migration_is_not_published(tmp_path, monkeypatch):
    from backend import platform_db
    url = 'sqlite:///' + str(tmp_path/'failure.sqlite3')
    database = Database(url)
    def failed(con):
        con.execute(text('CREATE TABLE v2_failed_probe (value INTEGER)'))
        raise ValueError('secret-connection-string')
    original = platform_db.SQL_MIGRATIONS
    monkeypatch.setattr(platform_db, 'SQL_MIGRATIONS', (*original, Migration(8, 'failed_step', failed)))
    with pytest.raises(MigrationError, match='迁移 8') as error:
        Database(url)
    assert 'secret-connection-string' not in str(error.value)
    with database.transaction() as con:
        assert con.execute(select(schema.c.version).where(schema.c.version == 8)).first() is None
        assert 'v2_failed_probe' not in inspect(con).get_table_names()
        con.execute(schema.insert().values(version=99, applied_at=0))
    monkeypatch.setattr(platform_db, 'SQL_MIGRATIONS', original)
    with pytest.raises(MigrationError, match='高于当前程序'):
        Database(url)
    database.engine.dispose()


def test_two_startups_serialize_initialization(tmp_path):
    url = 'sqlite:///' + str(tmp_path/'concurrent.sqlite3')
    def start():
        database = Database(url)
        try:
            return database.schema_version
        finally:
            database.engine.dispose()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: start(), range(2))) == [7, 7]


def test_account_source_migration_preserves_existing_ids(tmp_path, monkeypatch):
    from backend import platform_db
    from backend.platform_db import accounts, client_source
    url = 'sqlite:///' + str(tmp_path/'legacy.sqlite3')
    migrations = platform_db.SQL_MIGRATIONS
    monkeypatch.setattr(platform_db, 'SQL_MIGRATIONS', migrations[:2])
    old = Database(url)
    with old.transaction() as con:
        con.execute(accounts.insert().values(id='a'*32, user_id='u'*32, source='b'*64,
            client_id='c'*32, payload='preserved ciphertext', updated_at=1))
    old.engine.dispose()
    monkeypatch.setattr(platform_db, 'SQL_MIGRATIONS', migrations)
    upgraded = Database(url)
    with upgraded.transaction() as con:
        account = con.execute(select(accounts)).mappings().one()
        assert account['id'] == 'a'*32 and account['payload'] == 'preserved ciphertext'
        assert account['source'] == client_source('c'*32, 'b'*64)
    upgraded.engine.dispose()


def test_history_migration_pauses_queued_and_running_jobs(tmp_path, monkeypatch):
    from backend import platform_db
    from backend.platform_db import jobs
    url='sqlite:///'+str(tmp_path/'history.sqlite3')
    migrations=platform_db.SQL_MIGRATIONS
    monkeypatch.setattr(platform_db,'SQL_MIGRATIONS',migrations[:4])
    old=Database(url)
    with old.transaction() as con:
        for i,state in enumerate(('queued','running','failed')):
            con.execute(jobs.insert().values(id=str(i)*64,user_id='a'*32,session_id='b'*64,cycle='day',
                period_start=i,period_end=i+1,state=state,revision=1,attempts=0,lease='lease',lease_until=9,
                next_run=0,error='',updated_at=0))
    old.engine.dispose()
    monkeypatch.setattr(platform_db,'SQL_MIGRATIONS',migrations)
    upgraded=Database(url)
    with upgraded.transaction() as con:
        states={item['id'][0]:item for item in con.execute(select(jobs)).mappings()}
        assert states['0']['state']==states['1']['state']=='waiting_history'
        assert not states['1']['lease'] and states['2']['state']=='failed'
    upgraded.engine.dispose()


def test_env_initialization_keeps_existing_secrets(tmp_path):
    from tools.init_server_env import initialize
    from dotenv import dotenv_values
    from backend.platform_security import decode
    path = tmp_path/'.env'
    initialize(path)
    original = path.read_bytes()
    values = dotenv_values(path, interpolate=False)
    assert 'CHANGE_' not in original.decode()
    assert len(decode(values['WECHAT_MASTER_KEY'])) == 32
    assert values['MYSQL_PASSWORD'] in values['WECHAT_DATABASE_URL']
    assert values['MONGO_INITDB_ROOT_PASSWORD'] in values['WECHAT_MONGO_URL']
    assert values['REDIS_PASSWORD'] in values['WECHAT_REDIS_URL']
    with pytest.raises(FileExistsError):
        initialize(path)
    assert path.read_bytes() == original


def test_incompatible_mongo_prevents_sql_migration(monkeypatch):
    from types import SimpleNamespace
    from backend import platform_app
    from backend.platform_security import encode
    events = []
    for name, value in {'WECHAT_DATABASE_URL':'mysql+pymysql://user:pass@mysql/test',
                        'WECHAT_MONGO_URL':'mongodb://mongodb/test', 'WECHAT_MASTER_KEY':encode(b'a'*32),
                        'WECHAT_ADMIN_PASSWORD':'Initial-password-123'}.items():
        monkeypatch.setenv(name, value)
    def database(*args, **kwargs):
        assert kwargs['initialize'] is False
        return SimpleNamespace(engine=SimpleNamespace(dispose=lambda:events.append('closed')),
                               migrate=lambda:events.append('migrated'))
    def incompatible(*args, **kwargs):
        assert kwargs['initialize'] is False
        raise CompatibilityError('MongoDB version unsupported')
    monkeypatch.setattr(platform_app, 'Database', database)
    monkeypatch.setattr(platform_app, 'MongoDocuments', incompatible)
    with pytest.raises(CompatibilityError):
        platform_app.from_environment()
    assert events == ['closed']
