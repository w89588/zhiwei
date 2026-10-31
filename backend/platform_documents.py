"""Immutable encrypted Markdown blobs, published through durable SQL version pointers."""
import threading
import time
import uuid
from contextlib import contextmanager

from .migrations import Migration, MigrationError, check_engine_version, validate_history


MONGO_MIGRATIONS = (
    Migration(1, 'memory_owner_index', lambda store: store.collection.create_index([('user_id', 1), ('memory_id', 1)]), retry_safe=True),
    Migration(2, 'memory_format_version', lambda store: store.collection.update_many(
        {'format_version': {'$exists': False}}, {'$set': {'format_version': 1}}), retry_safe=True),
)


class MongoDocuments:
    def __init__(self, uri, *, initialize=True, lock_timeout=60):
        from pymongo import MongoClient
        self.client = MongoClient(uri, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000)
        self.schema_version, self.lock_timeout = 0, lock_timeout
        try:
            self.server_version = check_engine_version('mongodb', self.client.server_info()['version'])
            self.database = self.client.get_default_database()
            self.collection = self.database.get_collection('memory_versions')
            self.history = self.database.get_collection('_wechat_schema')
            if initialize:
                self.migrate()
        except Exception:
            self.client.close()
            raise

    @contextmanager
    def migration_lock(self):
        from pymongo import ReturnDocument
        from pymongo.errors import DuplicateKeyError
        locks = self.database.get_collection('_wechat_migration_lock')
        owner, deadline = uuid.uuid4().hex, time.monotonic() + self.lock_timeout
        while True:
            try:
                lock = locks.find_one_and_update({'_id':'platform', '$or':[
                    {'expires_at':{'$lte':time.time()}}, {'owner':owner}]},
                    {'$set':{'owner':owner, 'expires_at':time.time()+60}},
                    upsert=True, return_document=ReturnDocument.AFTER)
                if lock and lock['owner'] == owner:
                    break
            except DuplicateKeyError:
                pass
            if time.monotonic() >= deadline:
                raise MigrationError('等待 MongoDB 迁移锁超时，请检查其他服务实例')
            time.sleep(.2)
        stop, lost = threading.Event(), threading.Event()
        def renew():
            while not stop.wait(10):
                try:
                    result = locks.update_one({'_id':'platform', 'owner':owner}, {'$set':{'expires_at':time.time()+60}})
                    if result.matched_count != 1:
                        lost.set(); return
                except Exception:
                    lost.set(); return
        heartbeat = threading.Thread(target=renew, daemon=True)
        heartbeat.start()
        try:
            yield lost, owner, locks
        finally:
            stop.set(); heartbeat.join(5)
            locks.delete_one({'_id':'platform', 'owner':owner})

    def migrate(self):
        with self.migration_lock() as (lost, owner, locks):
            def ensure_owner():
                if lost.is_set() or not locks.find_one({'_id':'platform', 'owner':owner, 'expires_at':{'$gt':time.time()}}):
                    raise MigrationError('MongoDB 迁移锁已失效')
            ensure_owner()
            records = list(self.history.find({}))
            applied = [r['_id'] for r in records if r.get('state') == 'done']
            validate_history(applied, MONGO_MIGRATIONS)
            if any(not isinstance(r['_id'], int) or r['_id'] > len(MONGO_MIGRATIONS) for r in records):
                raise MigrationError('MongoDB 结构版本高于当前程序，请使用匹配的镜像')
            for migration in MONGO_MIGRATIONS:
                if migration.version in applied:
                    continue
                ensure_owner()
                previous = self.history.find_one({'_id':migration.version})
                if previous and not migration.retry_safe:
                    raise MigrationError(f'MongoDB 迁移 {migration.version} 上次未完成，需要检查后修复')
                self.history.update_one({'_id':migration.version}, {'$set':{'name':migration.name, 'owner':owner,
                    'state':'running', 'started_at':int(time.time())}}, upsert=True)
                try:
                    migration.apply(self)
                    ensure_owner()
                    self.history.update_one({'_id':migration.version, 'owner':owner}, {'$set':{'state':'done', 'applied_at':int(time.time())}})
                except Exception:
                    self.history.update_one({'_id':migration.version, 'owner':owner}, {'$set':{'state':'failed'}})
                    raise MigrationError(f'MongoDB 迁移 {migration.version}（{migration.name}）失败；未启动服务') from None
            self.schema_version = len(MONGO_MIGRATIONS)
        return self.schema_version

    def put(self, blob_id, user_id, memory_id, ciphertext):
        self.collection.update_one({'_id': blob_id}, {'$setOnInsert': {
            'user_id': user_id, 'memory_id': memory_id, 'ciphertext': ciphertext, 'format_version': 1}}, upsert=True)

    def get(self, blob_id, user_id):
        document = self.collection.find_one({'_id': blob_id, 'user_id': user_id})
        if not document:
            raise ValueError('记忆正文暂不可用，请检查文档存储或恢复备份')
        return document['ciphertext']

    def ping(self):
        self.client.admin.command('ping')

    def close(self):
        self.client.close()


class TestDocuments:
    """Explicit test double, never selected by deployment configuration."""
    def __init__(self):
        self.data = {}
        self.lock = threading.Lock()

    def put(self, blob_id, user_id, memory_id, ciphertext):
        with self.lock:
            self.data.setdefault((user_id, blob_id), ciphertext)

    def get(self, blob_id, user_id):
        return self.data[(user_id, blob_id)]

    def ping(self):
        pass

    def close(self):
        pass
