"""Seed/check isolated migration databases. Requires the explicit QA database name."""
import argparse
import json
import os
import time

from pymongo import MongoClient
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url

from backend.platform_db import Database, mcp_keys, migration_runs, row, schema
from backend.platform_security import digest


def main(mode):
    url, mongo_url = os.environ['WECHAT_DATABASE_URL'], os.environ['WECHAT_MONGO_URL']
    if make_url(url).database != 'wechat_migration_qa':
        raise ValueError('仅允许操作 wechat_migration_qa 测试数据库')
    mongo = MongoClient(mongo_url)
    documents = mongo.get_default_database()
    if documents.name != 'wechat_migration_qa':
        raise ValueError('仅允许操作 wechat_migration_qa 测试文档库')
    db = Database(url, initialize=mode == 'seed')
    try:
        if mode == 'seed':
            with db.transaction() as con:
                con.execute(mcp_keys.insert().values(id='migration-test', user_id='qa-user', name='QA legacy key',
                    hash=digest('qa-key-no-user-data'), prefix='mcp_write_qa', permission='write',
                    scopes=json.dumps({'sessions':['qa-session'], 'profile':False}), enabled=False,
                    expires=int(time.time())+86400, last_used=0, created_at=0))
                con.execute(delete(schema).where(schema.c.version == 2))
                con.execute(delete(migration_runs))
            documents.memory_versions.insert_one({'_id':'qa-blob', 'user_id':'qa-user',
                'memory_id':'qa-memory', 'ciphertext':'qa-ciphertext-preserved'})
            print('旧版 SQL V1 与无版本 MongoDB 测试数据已准备')
        elif mode == 'check':
            with db.transaction() as con:
                versions = sorted(con.execute(select(schema.c.version)).scalars())
                key = row(con, mcp_keys, mcp_keys.c.id == 'migration-test')
            assert versions == [1, 2] and key['permission'] == 'full'
            assert key['enabled'] is False and json.loads(key['scopes'])['sessions'] == ['qa-session']
            blob = documents.memory_versions.find_one({'_id':'qa-blob'})
            assert blob['format_version'] == 1 and blob['ciphertext'] == 'qa-ciphertext-preserved'
            history = list(documents['_wechat_schema'].find({}))
            assert sorted(item['_id'] for item in history if item['state'] == 'done') == [1, 2]
            print(json.dumps({'sql_schema':versions[-1], 'mongo_schema':2, 'legacy_data_preserved':True,
                'scope_and_disabled_state_preserved':True}))
        elif mode == 'future-sql':
            with db.transaction() as con:
                con.execute(schema.insert().values(version=99, applied_at=0))
        elif mode == 'future-mongo':
            with db.transaction() as con:
                con.execute(delete(schema).where(schema.c.version == 99))
            documents['_wechat_schema'].insert_one({'_id':99, 'state':'done'})
        elif mode == 'reset-qa':
            with db.transaction() as con:
                con.execute(delete(schema).where(schema.c.version > 1))
                con.execute(delete(migration_runs))
                con.execute(mcp_keys.update().where(mcp_keys.c.id == 'migration-test').values(permission='write'))
            documents['_wechat_schema'].delete_many({})
            documents.memory_versions.update_many({}, {'$unset':{'format_version':''}})
    finally:
        db.engine.dispose(); mongo.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['seed', 'check', 'future-sql', 'future-mongo', 'reset-qa'])
    main(parser.parse_args().mode)
