"""Read models for the listening workspace; encrypted bodies are loaded on demand."""
import json
from collections import defaultdict

from sqlalchemy import case, func, select

from .platform_db import contacts, sessions, memories, jobs, messages, row, rows


class Workspace:
    def __init__(self, platform):
        self.platform = platform

    def contacts(self, user_id, listening=True, contact_type='', account_id='', q='', offset=0, limit=50, identity=''):
        if contact_type not in {'', 'person', 'group'}:
            raise ValueError('通讯录类型无效')
        with self.platform.db.transaction() as con:
            groups = self.platform.identity.catalog(con, user_id)
            account_ids = set(self.platform.identity.account_members(con, user_id, account_id)) if account_id else None
        result = []
        for group in groups:
            payload = group['payload']
            if listening is not None and group['listening'] != listening:
                continue
            if account_ids is not None and not account_ids.intersection(group['account_ids']):
                continue
            if identity and identity not in {r['id'] for r, _ in group['sessions']+group['contacts']}:
                continue
            if contact_type and bool(payload.get('group')) != (contact_type == 'group'):
                continue
            if q and not any(q.casefold() in json.dumps(p, ensure_ascii=False).casefold() for _,p in group['sessions']+group['contacts']):
                continue
            result.append({**payload, 'source_id': payload['id'], 'id': group['id'],
                           'account_id': group['account_id'], 'listening': group['listening'], 'cycles': group['cycles']})
        return {'items': result[offset:offset+limit], 'total': len(result), 'has_more': offset+limit < len(result)}

    def add_listener(self, principal, contact_id, cycles):
        from .platform_service import NotFound, now
        with self.platform.db.transaction() as con:
            self.platform.check_user(con, principal['user_id'], True)
            session = row(con, sessions, sessions.c.id == contact_id, sessions.c.user_id == principal['user_id'])
            if not session:
                contact = row(con, contacts, contacts.c.id == contact_id, contacts.c.user_id == principal['user_id'])
                if not contact:
                    raise NotFound('通讯录对象不存在')
                payload = self.platform.decrypt_row('contacts', contact)
                con.execute(sessions.insert().values(id=contact_id, user_id=principal['user_id'],
                    account_id=contact['account_id'], updated_at=now(),
                    payload=self.platform.vault.encrypt(principal['user_id'], 'sessions', contact_id,
                        {k: payload[k] for k in ('id','name','group')})))
        return self.platform.listen(principal, contact_id, True, cycles)

    def memory_tree(self, principal):
        # SQL filters first. Only the metadata of authorized listening sessions is read.
        with self.platform.db.transaction() as con:
            catalog = [g for g in self.platform.identity.catalog(con, principal['user_id']) if g['listening'] and g['sessions']]
            allowed, alias_owner = {}, {}
            for group in catalog:
                session = next(r for r,_ in group['sessions'] if r['id'] == group['id'])
                _, owner, siblings = self.platform.identity.group(con, principal['user_id'], session, create=True)
                allowed[owner] = group
                alias_owner.update({s['id']: owner for s in siblings})
            docs = rows(con, memories, memories.c.user_id == principal['user_id'],
                        (memories.c.session_id == '') | memories.c.session_id.in_(alias_owner),
                        limit=50000, order=memories.c.period_start.desc())
        groups = {'personal': {'id': 'personal', 'name': '个人', 'session_id': '', 'types': {}}}
        for identity, group in allowed.items():
            payload = group['payload']
            groups[identity] = {'id': identity, 'name': payload.get('remark') or payload.get('name') or payload['id'],
                                'account_id': group['account_id'], 'session_id': identity, 'types': {}}
        for doc in docs:
            group = groups[alias_owner.get(doc['session_id'], 'personal')]
            group['types'].setdefault(doc['kind'], []).append({k: doc[k] for k in
                ('id','kind','version','period_start','period_end','manual','updated_at')})
        return {'items': list(groups.values()), 'total': len(docs), 'truncated': len(docs) == 50000}

    def analysis(self, user_id, offset=0, limit=30):
        from .platform_service import now
        with self.platform.db.transaction() as con:
            for group in self.platform.identity.catalog(con, user_id):
                if group['listening'] and group['sessions']:
                    session = next(r for r,_ in group['sessions'] if r['id'] == group['id'])
                    self.platform.identity.group(con, user_id, session, create=True)
            conditions = [sessions.c.user_id == user_id, sessions.c.listening == True]
            total = con.execute(select(func.count()).select_from(sessions).where(*conditions)).scalar_one()
            page = rows(con, sessions, *conditions, limit=limit, offset=offset, order=sessions.c.updated_at.desc())
            labels = {c['id']: c for c in rows(con, contacts, contacts.c.user_id == user_id,
                contacts.c.id.in_([s['id'] for s in page]), limit=limit)}
            counts = con.execute(select(jobs.c.session_id, jobs.c.state, func.count(), func.max(jobs.c.updated_at))
                .where(jobs.c.user_id == user_id, jobs.c.session_id.in_([s['id'] for s in page]))
                .group_by(jobs.c.session_id, jobs.c.state)).all()
            summary = {state: count for state, count in con.execute(select(jobs.c.state, func.count())
                .join(sessions, sessions.c.id == jobs.c.session_id)
                .where(jobs.c.user_id == user_id, sessions.c.user_id == user_id, sessions.c.listening == True)
                .group_by(jobs.c.state))}
            failures = rows(con, jobs, jobs.c.user_id == user_id, jobs.c.state == 'failed',
                jobs.c.session_id.in_(select(sessions.c.id).where(*conditions)), limit=10, order=jobs.c.updated_at.desc())
            scheduled, budget_waiting = con.execute(select(
                func.coalesce(func.sum(case((jobs.c.period_end > now(), 1), else_=0)), 0),
                func.coalesce(func.sum(case((jobs.c.error == '已达到今日模型调用上限，将于明日自动继续', 1), else_=0)), 0))
                .where(jobs.c.user_id == user_id, jobs.c.state == 'queued',
                    jobs.c.session_id.in_(select(sessions.c.id).where(*conditions)))).one()
        grouped = defaultdict(dict)
        for sid, state, count, updated in counts:
            grouped[sid][state] = count
        items = []
        for session in page:
            payload = self.platform.decrypt_row('sessions', session)
            contact = self.platform.decrypt_row('contacts', labels[session['id']]) if session['id'] in labels else {}
            items.append({'id': session['id'], 'name': contact.get('remark') or payload.get('name', ''), 'group': payload.get('group', False),
                          'cycles': session['cycles'].split(','), 'counts': grouped[session['id']]})
        return {'items': items, 'total': total, 'has_more': offset+limit < total,
                'summary': summary, 'scheduled': scheduled, 'budget_waiting': budget_waiting,
                'failures': [{k: j[k] for k in ('id','session_id','cycle','error','updated_at')} for j in failures]}
