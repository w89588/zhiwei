"""Logical WeChat accounts and conversation pools over isolated device sources."""
import json

from sqlalchemy import and_, delete, func, or_, select

from .platform_db import accounts, contacts, history_sync, jobs, logical_sessions, media_objects, memories, message_pool, messages, row, sessions, rows
from .platform_security import digest


class Identity:
    def __init__(self, platform):
        self.platform = platform

    def merge_cycles(self, con, user_id, group_id, owner, listened, siblings):
        if not owner['listening']:
            return
        from .platform_jobs import CYCLES, enqueue_periods
        previous = set(owner['cycles'].split(','))
        selected = set().union(*(set(s['cycles'].split(',')) for s in listened))
        extra = [cycle for cycle in CYCLES if cycle in selected and cycle not in previous]
        if not extra:
            return
        merged = [cycle for cycle in CYCLES if cycle in previous or cycle in selected]
        con.execute(sessions.update().where(sessions.c.id == owner['id']).values(cycles=','.join(merged)))
        day = func.floor((messages.c.timestamp + 28800) / 86400) * 86400 - 28800
        stamps = con.execute(select(day).select_from(messages.join(message_pool, message_pool.c.message_id == messages.c.id))
            .where(messages.c.user_id == user_id, message_pool.c.user_id == user_id, message_pool.c.group_id == group_id).distinct()).scalars()
        enqueue_periods(con, user_id, {**owner, 'cycles':','.join(extra)}, (int(x) for x in stamps),
            history_ready=self.history_complete(con, user_id, siblings))
        owner['cycles'] = ','.join(merged)

    def account_members(self, con, user_id, account_id):
        account = row(con, accounts, accounts.c.user_id == user_id, accounts.c.id == account_id)
        if not account:
            return []
        identity = self.platform.decrypt_row('accounts', account).get('self_id', '')
        if not identity:
            return [account_id]
        return [a['id'] for a in con.execute(select(accounts).where(accounts.c.user_id == user_id)).mappings()
                if self.platform.decrypt_row('accounts', a).get('self_id', '') == identity]

    @staticmethod
    def group_key(user_id, kind, account, local_id):
        return digest(json.dumps([str(user_id), kind, account, local_id], ensure_ascii=False))

    def reidentify(self, con, user_id, account_id, identity):
        """Merge a formerly anonymous source after its exact WeChat ID becomes known."""
        for source in rows(con, sessions, sessions.c.user_id == user_id, sessions.c.account_id == account_id, limit=50000):
            local_id = self.platform.decrypt_row('sessions', source)['id']
            old_id = self.group_key(user_id, 'source', account_id, local_id)
            new_id = self.group_key(user_id, 'wechat', identity, local_id)
            old = row(con, logical_sessions, logical_sessions.c.id == old_id, logical_sessions.c.user_id == user_id)
            if not old:
                continue
            new = row(con, logical_sessions, logical_sessions.c.id == new_id, logical_sessions.c.user_id == user_id)
            if not new:
                con.execute(message_pool.update().where(message_pool.c.group_id == old_id).values(group_id=new_id))
                con.execute(media_objects.update().where(media_objects.c.user_id == user_id, media_objects.c.group_id == old_id).values(group_id=new_id))
                con.execute(logical_sessions.update().where(logical_sessions.c.id == old_id).values(id=new_id))
                continue
            old_owner = row(con, sessions, sessions.c.id == old['owner_id'], sessions.c.user_id == user_id)
            new_owner = row(con, sessions, sessions.c.id == new['owner_id'], sessions.c.user_id == user_id)
            candidates = [s for s in (new_owner, old_owner) if s]
            if not candidates:
                raise ValueError('原会话池没有有效会话，已停止合并')
            counts = dict(con.execute(select(memories.c.session_id, func.count()).where(memories.c.user_id == user_id,
                memories.c.session_id.in_([s['id'] for s in candidates])).group_by(memories.c.session_id)).all())
            owner = sorted(candidates, key=lambda s: (not s['listening'], -counts.get(s['id'],0), s['id']))[0]
            old_entries = [dict(r) for r in con.execute(select(message_pool).where(message_pool.c.group_id == old_id,
                message_pool.c.user_id == user_id)).mappings()]
            target_entries = [dict(r) for r in con.execute(select(message_pool).where(message_pool.c.group_id == new_id,
                message_pool.c.user_id == user_id)).mappings()]
            present = {e['message_key'] for e in target_entries}
            old_keys = {e['message_key'] for e in old_entries}
            unique = [e for e in old_entries if e['message_key'] not in present]
            additional = unique if owner['id'] == new['owner_id'] else [e for e in target_entries if e['message_key'] not in old_keys]
            con.execute(delete(message_pool).where(message_pool.c.group_id == old_id, message_pool.c.user_id == user_id))
            con.execute(media_objects.update().where(media_objects.c.user_id == user_id, media_objects.c.group_id == old_id).values(group_id=new_id))
            for start in range(0, len(unique), 500):
                con.execute(message_pool.insert(), [{**e, 'group_id':new_id} for e in unique[start:start+500]])
            con.execute(delete(logical_sessions).where(logical_sessions.c.id == old_id, logical_sessions.c.user_id == user_id))
            con.execute(logical_sessions.update().where(logical_sessions.c.id == new_id).values(owner_id=owner['id']))
            for other in candidates:
                if other['id'] != owner['id'] and other['listening']:
                    con.execute(sessions.update().where(sessions.c.id == other['id']).values(listening=False,
                        listen_epoch=other['listen_epoch']+1))
                    con.execute(jobs.update().where(jobs.c.user_id == user_id, jobs.c.session_id == other['id'],
                        jobs.c.state.in_(['queued','running','waiting_history','waiting_model'])).values(
                            state='cancelled', lease='', lease_until=0, error='已合并到同一微信会话'))
            _, _, siblings = self.group(con, user_id, owner, create=False)
            self.merge_cycles(con, user_id, new_id, owner, [s for s in candidates if s['listening']], siblings)
            if owner['listening'] and additional:
                from .platform_jobs import enqueue_periods
                ids = [e['message_id'] for e in additional]
                stamps = [ts for offset in range(0,len(ids),500) for ts in con.execute(select(messages.c.timestamp).where(
                    messages.c.user_id == user_id, messages.c.id.in_(ids[offset:offset+500]))).scalars()]
                enqueue_periods(con, user_id, owner, stamps, debounce=True,
                    history_ready=self.history_complete(con, user_id, siblings))

    def account_groups(self, con, user_id):
        groups = {}
        for a in con.execute(select(accounts).where(accounts.c.user_id == user_id)).mappings():
            a = dict(a)
            identity = self.platform.decrypt_row('accounts', a).get('self_id', '')
            groups.setdefault(('wechat', identity) if identity else ('source', a['id']), []).append(a)
        return list(groups.values())

    def catalog(self, con, user_id):
        """Group sessions and contacts by exact WeChat account and local ID."""
        accounts_by_id = {}
        for group in self.account_groups(con, user_id):
            key = min(a['id'] for a in group)
            for account in group:
                accounts_by_id[account['id']] = key
        grouped = {}
        for table, kind in ((sessions, 'sessions'), (contacts, 'contacts')):
            for item in rows(con, table, table.c.user_id == user_id, limit=50000):
                payload = self.platform.decrypt_row(kind, item)
                key = (accounts_by_id.get(item['account_id'], item['account_id']), payload['id'])
                group = grouped.setdefault(key, {'sessions': [], 'contacts': []})
                group[kind].append((item, payload))
        owners = {r['owner_id'] for r in con.execute(select(logical_sessions.c.owner_id).where(
            logical_sessions.c.user_id == user_id)).mappings()}
        counts = dict(con.execute(select(memories.c.session_id, func.count()).where(memories.c.user_id == user_id,
            memories.c.session_id != '').group_by(memories.c.session_id)).all())
        result = []
        for item in grouped.values():
            session_rows = item['sessions']; contact_rows = item['contacts']
            if session_rows:
                listened = [s for s in session_rows if s[0]['listening']]
                chosen = next((s for s in session_rows if s[0]['id'] in owners), None)
                if not chosen:
                    chosen = sorted(listened, key=lambda s: (-counts.get(s[0]['id'], 0), s[0]['id']))[0] if listened else min(session_rows, key=lambda s:s[0]['id'])
                owner, session_payload = chosen
                contact = next((c for c in contact_rows if c[0]['id'] == owner['id']), None)
                if not contact and contact_rows:
                    contact = min(contact_rows, key=lambda c:c[0]['id'])
                payload = contact[1] if contact else session_payload
                identity, account_id = owner['id'], owner['account_id']
                cycles = owner['cycles'].split(',')
            else:
                chosen = min(contact_rows, key=lambda c:c[0]['id'])
                owner, payload = chosen
                identity, account_id = owner['id'], owner['account_id']
                cycles = ['day','week','month','halfyear','year']
            result.append({'id':identity, 'account_id':account_id, 'payload':payload,
                'listening':any(s[0]['listening'] for s in session_rows), 'cycles':cycles,
                'sessions':session_rows, 'contacts':contact_rows,
                'account_ids':{r['account_id'] for r, _ in session_rows+contact_rows}})
        return result

    def group(self, con, user_id, session, create=False):
        """Resolve a source session; initialize its pool only on first use."""
        user_id = str(user_id)
        if create:
            self.platform.check_user(con, user_id, True)
        local_id = self.platform.decrypt_row('sessions', session)['id']
        members = self.account_members(con, user_id, session['account_id']) or [session['account_id']]
        account = row(con, accounts, accounts.c.id == session['account_id'], accounts.c.user_id == user_id)
        identity = self.platform.decrypt_row('accounts', account).get('self_id', '') if account else ''
        group_id = self.group_key(user_id, 'wechat' if identity else 'source', identity or session['account_id'], local_id)
        ids = [self.platform.session_id(a, local_id) for a in members]
        siblings = [dict(x) for x in con.execute(select(sessions).where(sessions.c.user_id == user_id, sessions.c.id.in_(ids))).mappings()]
        group = row(con, logical_sessions, logical_sessions.c.id == group_id, logical_sessions.c.user_id == user_id, lock=create)
        valid_owner = bool(group and any(s['id'] == group['owner_id'] for s in siblings))
        if valid_owner or not create:
            listened = sorted(x['id'] for x in siblings if x['listening'])
            owner = group['owner_id'] if valid_owner else (listened[0] if listened else min(x['id'] for x in siblings))
            return group_id, owner, siblings
        # Recheck under the user's serialization lock before inserting.
        group = row(con, logical_sessions, logical_sessions.c.id == group_id, logical_sessions.c.user_id == user_id, lock=True)
        if group and any(s['id'] == group['owner_id'] for s in siblings):
            return group_id, group['owner_id'], siblings
        if group and row(con, sessions, sessions.c.id == group['owner_id'], sessions.c.user_id == user_id):
            raise ValueError('会话池身份与原数据源不一致，已停止自动合并')
        counts = dict(con.execute(select(memories.c.session_id, func.count()).where(memories.c.user_id == user_id,
            memories.c.session_id.in_([s['id'] for s in siblings])).group_by(memories.c.session_id)).all())
        listened = [s for s in siblings if s['listening']]
        owner = sorted(listened, key=lambda s: (-counts.get(s['id'], 0), s['id']))[0]['id'] if listened else min(s['id'] for s in siblings)
        if group:
            # A server restore may remove accounts/messages while leaving newer
            # pool metadata behind. Rebuild only after verifying the old owner is gone.
            con.execute(delete(message_pool).where(message_pool.c.group_id == group_id, message_pool.c.user_id == user_id))
            con.execute(logical_sessions.update().where(logical_sessions.c.id == group_id).values(owner_id=owner))
        else:
            con.execute(logical_sessions.insert().values(id=group_id, user_id=user_id, owner_id=owner))
        # Existing source rows remain untouched; a pool entry selects one copy of
        # each globally identified message while keeping every device cursor.
        seen, entries, additional = set(), [], set()
        for sibling in sorted(siblings, key=lambda s: (s['id'] != owner, s['id'])):
            after = None
            while True:
                query = select(messages).where(messages.c.user_id == user_id, messages.c.session_id == sibling['id'])
                if after:
                    query = query.where(or_(messages.c.timestamp > after[0],
                        and_(messages.c.timestamp == after[0], messages.c.id > after[1])))
                page = list(con.execute(query.order_by(messages.c.timestamp, messages.c.id).limit(500)).mappings())
                if not page:
                    break
                for item in page:
                    payload = self.platform.decrypt_row('messages', item)
                    key = self.message_key(payload, item['id'])
                    if key in seen:
                        continue
                    seen.add(key)
                    entries.append({'group_id':group_id, 'message_key':key, 'user_id':user_id, 'message_id':item['id']})
                    if item['session_id'] != owner:
                        additional.add(((item['timestamp'] + 28800) // 86400) * 86400 - 28800)
                if entries:
                    con.execute(message_pool.insert(), entries); entries = []
                after = (page[-1]['timestamp'], page[-1]['id'])
        if entries:
            con.execute(message_pool.insert(), entries)
        for sibling in listened:
            if sibling['id'] != owner:
                con.execute(sessions.update().where(sessions.c.id == sibling['id']).values(
                    listening=False, listen_epoch=sibling['listen_epoch']+1))
                con.execute(jobs.update().where(jobs.c.user_id == user_id, jobs.c.session_id == sibling['id'],
                    jobs.c.state.in_(['queued','running','waiting_history','waiting_model'])).values(
                        state='cancelled', lease='', lease_until=0, error='已合并到同一微信会话'))
                sibling['listening'] = False
        chosen = next(s for s in siblings if s['id'] == owner)
        self.merge_cycles(con, user_id, group_id, chosen, listened, siblings)
        if chosen['listening'] and additional:
            from .platform_jobs import enqueue_periods
            enqueue_periods(con, user_id, chosen, additional, debounce=True,
                history_ready=self.history_complete(con, user_id, siblings))
        return group_id, owner, siblings

    @staticmethod
    def message_key(payload, physical_id):
        server_id = str(payload.get('server_id') or '')
        return digest('server:'+server_id) if server_id not in {'', '0'} else digest('source:'+physical_id)

    @staticmethod
    def history_complete(con, user_id, siblings):
        statuses = dict(con.execute(select(history_sync.c.session_id, history_sync.c.complete).where(
            history_sync.c.user_id == user_id, history_sync.c.session_id.in_([s['id'] for s in siblings]))).all())
        return bool(siblings) and all(statuses.get(s['id'], False) for s in siblings)

    def add_message(self, con, user_id, group_id, physical_id, payload):
        key = self.message_key(payload, physical_id)
        existing = row(con, message_pool, message_pool.c.group_id == group_id, message_pool.c.message_key == key)
        if existing:
            if existing['message_id'] != physical_id and not row(con, messages, messages.c.id == existing['message_id'], messages.c.user_id == user_id):
                con.execute(message_pool.update().where(message_pool.c.group_id == group_id,
                    message_pool.c.message_key == key).values(message_id=physical_id))
                return True
            return existing['message_id'] == physical_id
        con.execute(message_pool.insert().values(group_id=group_id, message_key=key, user_id=user_id, message_id=physical_id))
        return True
