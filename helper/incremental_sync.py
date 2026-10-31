"""Durable upload checkpoints, recent-first bootstrap and bounded historical catch-up."""
import hashlib
import json
import time

from . import storage


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class IncrementalSync:
    def __init__(self, uploader, w, config, common, client, guard, full=False, started=None):
        self.uploader, self.w, self.config = uploader, w, config
        self.common, self.client, self.guard, self.full = common, client, guard, full
        self.identity = fingerprint([common['source'], config['url'], config['app_key']])
        records = storage.read_json('sync-progress.bin', {}, secret=True)
        self.progress = records.get(self.identity, {'manifest': {}, 'shards': {}, 'turn': 0})
        self.audit = full or time.time() - self.progress.get('audit_at', 0) >= 300
        self.audit_epoch = self.progress.setdefault('audit_epoch', time.time_ns()) if self.audit and not full else None
        self.uploaded = 0
        self.budget = float('inf') if full else 1000
        self.started = time.monotonic() if started is None else started

    def save(self):
        with storage.WRITE_LOCK:
            records = storage.read_json('sync-progress.bin', {}, secret=True)
            records[self.identity] = self.progress
            storage.write_json('sync-progress.bin', records, secret=True)

    def post(self, payload):
        from .platform_sync import envelope
        self.guard()
        return self.uploader.response(self.client.post('api/v2/client/sync', json=envelope(self.config, {**self.common, **payload})))

    def page_limit(self):
        return 100 if self.full else max(1, min(100, int(self.budget)))

    def manifest(self, catalog, contacts, deadline=None):
        changed = {}
        seen = self.progress.setdefault('audit_manifest', {}) if self.audit_epoch is not None else {}
        for kind, objects in [('sessions', catalog), ('contacts', contacts)]:
            changed[kind] = [obj for obj in objects if self.full or
                (self.audit_epoch is not None and seen.get(kind+':'+obj['id']) != self.audit_epoch) or
                self.progress['manifest'].get(kind+':'+obj['id']) != fingerprint(obj)]
        for index in range(0, max(len(changed['sessions']), len(changed['contacts']), 1 if self.audit else 0), 200):
            if deadline is not None and index and time.monotonic() >= deadline:
                self.save()
                return False
            payload = {kind: objects[index:index+200] for kind, objects in changed.items()}
            self.post({'kind': 'manifest', **payload})
            for kind, objects in payload.items():
                for obj in objects:
                    self.progress['manifest'][kind+':'+obj['id']] = fingerprint(obj)
                    if self.audit_epoch is not None:
                        seen[kind+':'+obj['id']] = self.audit_epoch
            self.save()
        return True

    def send(self, user, database, items, state, remote_cursor):
        recent = state.setdefault('recent', {})
        changed = [m for m in items if self.full or m['local_id'] > remote_cursor or recent.get(str(m['local_id'])) != fingerprint(m)]
        batches, batch, size = [], [], 0
        for message in changed:
            length = len(json.dumps(message, ensure_ascii=False).encode())
            if batch and size+length > 3*1024*1024:
                batches.append(batch); batch, size = [], 0
            batch.append(message); size += length
        if batch:
            batches.append(batch)
        for batch in batches:
            result = self.post({'kind': 'messages', 'session': user, 'items': batch})
            self.uploader.changed_messages += result.get('changed', 0)
            self.uploaded += len(batch)
            for message in batch:
                recent[str(message['local_id'])] = fingerprint(message)
            state['cursor'] = max(state.get('cursor', -1), max(m['local_id'] for m in batch))
            self.save()  # Acknowledge only after the server commits. Lost replies retry safely.
        for message in items:
            recent[str(message['local_id'])] = fingerprint(message)
        state['recent'] = {k: recent[k] for k in sorted(recent, key=int)[-200:]}

    def history_ready(self, user, databases, require_current=False):
        table = 'Msg_'+hashlib.md5(user.encode()).hexdigest()
        for database in databases:
            if table not in self.w.tables.get(database, set()):
                continue
            state = self.progress['shards'].get(fingerprint([user, database]))
            if not state or state.get('history_until', 0) > state.get('history_after', -1)+1:
                return False
            if require_current and state.get('stamp') != fingerprint(self.w.cache.get(database, (None,))[0]):
                return False
        return True

    def report_history(self, user, complete):
        sent = self.progress.setdefault('history_announced', {})
        if sent.get(user) is complete and not (complete and self.audit):
            return
        self.post({'kind': 'history', 'session': user, 'history_complete': complete})
        sent[user] = complete
        self.save()

    def messages(self, catalog, databases, priorities=None, *, prune=True, skip_priority=False, deadline=None):
        relevant = {s['id']: [db for db in databases if 'Msg_'+hashlib.md5(s['id'].encode()).hexdigest()
                    in self.w.tables.get(db, set())] for s in catalog}
        if prune:
            active = {fingerprint([s['id'], db]) for s in catalog for db in relevant[s['id']]}
            self.progress['shards'] = {key: value for key, value in self.progress['shards'].items() if key in active}
        if not catalog:
            return False
        priorities = priorities or set()
        high = [] if skip_priority else [s for s in catalog if s['id'] in priorities]
        normal = [s for s in catalog if s['id'] not in priorities]
        both = bool(high and normal and not self.full)
        exhausted = False
        for group, turn_key, budget, duration in ((high, 'priority_turn', 600 if both else 1000, 6),
                                                   (normal, 'turn', 1000, 9)):
            if not group:
                continue
            turn = self.progress.get(turn_key, 0) % len(group)
            ordered = group[turn:] + group[:turn]
            self.progress[turn_key] = (turn + 1) % len(group)
            self.budget = float('inf') if self.full else budget
            group_deadline = deadline if deadline is not None else (self.started + duration if high and not self.full else None)
            exhausted = self.message_group(ordered, relevant, group_deadline) or exhausted
        return exhausted or any(s.get('stamp') is None or s.get('history_until', 0) > s.get('history_after', -1)+1
                                for s in self.progress['shards'].values())

    def message_group(self, ordered, relevant, deadline=None):
        work = 0
        def expired():
            return bool(deadline is not None and work and time.monotonic() >= deadline)
        for session in ordered:
            if expired():
                self.save()
                return True
            user = session['id']
            self.guard()
            if not self.uploader.allowed(self.w, user):
                raise ValueError('会话上传范围已改变')
            pairs = [(database, fingerprint(self.w.cache.get(database, (None,))[0]),
                      self.progress['shards'].setdefault(fingerprint([user, database]), {})) for database in relevant[user]]
            pending = [(db, stamp, state) for db, stamp, state in pairs
                       if self.full or (self.audit_epoch is not None and state.get('audit_epoch') != self.audit_epoch)
                       or state.get('stamp') != stamp or state.get('history_until', 0) > state.get('history_after', -1)+1]
            if not pending:
                continue
            cursors = {} if self.full else self.uploader.response(self.client.get('api/v2/client/cursors',
                params={'source': self.common['source'], 'session': user}))['cursors']
            for database, stamp, state in pending:
                if self.budget <= 0 or expired():
                    self.save()
                    return True
                remote = cursors.get(hashlib.sha256(database.encode()).hexdigest(), -1)
                if remote < state.get('cursor', -1):
                    state.clear()  # Server restored/cleared: reconcile instead of trusting a local checkpoint.
                if not state and not self.full and remote < 0:
                    tail = self.w.upload_batch(user, database, -1, self.page_limit(), platform=True, newest=True)
                    work += 1
                    if tail:
                        state.update(history_after=-1, history_until=tail[0]['local_id'])
                        self.save()  # Preserve the missing older range before advancing the remote high-water mark.
                        self.send(user, database, tail, state, remote)
                        remote = state.get('cursor', remote)
                        self.budget -= len(tail)
                after = -1 if self.full else max(-1, remote-100)
                complete = False
                # Cap work per shard so a large history cannot monopolize a sync round.
                pages = 0
                while self.budget > 0 and (self.full or pages < 2):
                    if expired():
                        self.save()
                        return True
                    self.guard()
                    limit = self.page_limit()
                    items = self.w.upload_batch(user, database, after, limit, platform=True)
                    work += 1
                    if not items:
                        complete = True
                        break
                    self.send(user, database, items, state, remote)
                    after = items[-1]['local_id']; pages += 1
                    self.budget -= len(items)
                    if len(items) < limit:
                        complete = True
                        break
                if complete:
                    state['stamp'] = stamp
                    if self.audit_epoch is not None:
                        state['audit_epoch'] = self.audit_epoch
                # Backfill the earlier range with its own durable cursor, independent of recent messages.
                if self.budget > 0 and state.get('history_until', 0) > state.get('history_after', -1)+1:
                    if expired():
                        self.save()
                        return True
                    older = self.w.upload_batch(user, database, state.get('history_after', -1), self.page_limit(), platform=True)
                    work += 1
                    older = [m for m in older if m['local_id'] < state['history_until']]
                    if older:
                        self.send(user, database, older, state, remote)
                        state['history_after'] = older[-1]['local_id']
                        self.budget -= len(older)
                    else:
                        state['history_after'] = state['history_until']
                self.uploader.states[self.common['source']].update(uploaded=self.uploaded,
                    message=f"增量同步 {session['name']} · 本轮 {self.uploaded} 条，历史在后台补齐")
        return False

    def audit_complete(self, catalog, databases):
        if self.audit_epoch is None:
            return True
        for session in catalog:
            table = 'Msg_'+hashlib.md5(session['id'].encode()).hexdigest()
            for db in databases:
                if table in self.w.tables.get(db, set()):
                    state = self.progress['shards'].get(fingerprint([session['id'], db]), {})
                    if state.get('audit_epoch') != self.audit_epoch:
                        return False
        return True

    def finish(self, audit_complete=True):
        if self.audit and audit_complete:
            self.progress['audit_at'] = time.time()
            self.progress.pop('audit_epoch', None)
            self.progress.pop('audit_manifest', None)
        self.save()
