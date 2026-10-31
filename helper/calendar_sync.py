"""Local calendar and outbound CalDAV, with confirmation and ETag conflict handling."""
from datetime import date, datetime, timedelta, timezone
import json
import time
from urllib.parse import urljoin, urlsplit, quote
import uuid
from xml.etree import ElementTree as ET
import httpx
from . import storage, memory_store as store

TZ=timezone(timedelta(hours=8))
D='{DAV:}';C='{urn:ietf:params:xml:ns:caldav}'


def normalize_event(value,allow_unscheduled=False):
    title=str(value.get('title','')).strip()
    if not title or len(title)>200:raise ValueError('日程标题不能为空，最多 200 字')
    all_day=value.get('all_day',True)
    if not isinstance(all_day,bool):raise ValueError('全天日程标记无效')
    start=str(value.get('start','')).strip();end=str(value.get('end','')).strip()
    recurrence=value.get('recurrence','none')
    if recurrence not in {'none','yearly'}:raise ValueError('仅支持单次或每年重复的日程')
    if not start:
        if not allow_unscheduled:raise ValueError('请先确认日程日期')
        end=''
    elif all_day:
        first=date.fromisoformat(start);after=date.fromisoformat(end) if end else first+timedelta(days=1)
        if after<=first:raise ValueError('结束日期必须在开始日期之后（全天结束日期不包含在日程内）')
        start=first.isoformat();end=after.isoformat()
    else:
        first=datetime.fromisoformat(start);after=datetime.fromisoformat(end) if end else first+timedelta(hours=1)
        first=first.replace(tzinfo=TZ) if first.tzinfo is None else first
        after=after.replace(tzinfo=TZ) if after.tzinfo is None else after
        if after<=first:raise ValueError('结束时间必须晚于开始时间')
        start=first.astimezone(TZ).isoformat();end=after.astimezone(TZ).isoformat()
    description=str(value.get('description',''))
    location=str(value.get('location',''))
    if len(description)>4000 or len(location)>500:raise ValueError('日程说明或地点过长')
    return {'title':title,'start':start,'end':end,'all_day':all_day,'recurrence':recurrence,'description':description,'location':location}


def events(account,users,month=None,include_ignored=False):
    with store.database() as con:
        rows=con.execute('SELECT * FROM events WHERE account=? ORDER BY start,created_at DESC',(account,)).fetchall()
    result=[]
    if month:
        first=date.fromisoformat(month+'-01')
        after=date(first.year+1,1,1) if first.month==12 else date(first.year,first.month+1,1)
    for row in rows:
        if row['user'] and row['user'] not in users or not include_ignored and row['status']=='ignored':continue
        item=store.unpack(row)
        item['all_day']=bool(item['all_day']);item['display_start']=item['start'];item['display_end']=item['end']
        if month and item['start']:
            event_date=date.fromisoformat(item['start'][:10])
            end_date=date.fromisoformat(item['end'][:10])
            if item['recurrence']=='yearly':
                # Include last year's occurrence when it extends into January.
                for year in (first.year-1,first.year):
                    if year<event_date.year:continue
                    try:shown=event_date.replace(year=year)
                    except ValueError:continue  # Feb 29 repeats only in leap years.
                    delta=shown-event_date;occurrence=dict(item)
                    if item['all_day']:
                        occurrence['display_start']=shown.isoformat();occurrence['display_end']=(end_date+delta).isoformat()
                    else:
                        occurrence['display_start']=(datetime.fromisoformat(item['start'])+delta).isoformat()
                        occurrence['display_end']=(datetime.fromisoformat(item['end'])+delta).isoformat()
                    if overlaps_month(occurrence,first,after):result.append(occurrence)
                continue
            if not overlaps_month(item,first,after):continue
        result.append(item)
    return result


def overlaps_month(item,first,after):
    start=item['display_start'];end=item['display_end']
    if item['all_day']:return start<after.isoformat() and end>first.isoformat()
    begin=datetime.combine(first,datetime.min.time(),TZ)
    finish=datetime.combine(after,datetime.min.time(),TZ)
    return datetime.fromisoformat(start)<finish and datetime.fromisoformat(end)>begin


def event_by_id(account,users,key):
    with store.database() as con:row=con.execute('SELECT * FROM events WHERE account=? AND id=?',(account,key)).fetchone()
    if row is None or row['user'] and row['user'] not in users:raise ValueError('日程不存在或来源会话已移出工作台')
    result=store.unpack(row);result['all_day']=bool(result['all_day']);return result


def save_event(account,users,value,key=None):
    old=event_by_id(account,users,key) if key else None
    user=old['user'] if old else value.get('user','')
    if user and user not in users:raise ValueError('请先将日程关联会话加入工作台')
    status=value.get('status',old['status'] if old else 'confirmed')
    if status not in {'draft','confirmed','ignored'}:raise ValueError('无效日程状态')
    normalized=normalize_event((old or {})|value,allow_unscheduled=status!='confirmed')
    now=int(time.time());key=key or uuid.uuid4().hex
    with store.database() as con:
        if old:
            con.execute('UPDATE events SET title=?,start=?,end=?,all_day=?,recurrence=?,description=?,location=?,status=?,sync_state=?,sync_error=?,updated_at=?,revision=revision+1 WHERE id=?',
                        tuple(normalized[k] for k in ('title','start','end','all_day','recurrence','description','location'))+(status,'pending','',now,key))
        else:
            con.execute('INSERT INTO events(id,account,user,title,start,end,all_day,recurrence,description,location,status,evidence,updated_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (key,account,user)+tuple(normalized[k] for k in ('title','start','end','all_day','recurrence','description','location'))+(status,'[]',now,now))
    return key


def escape_text(text):
    return str(text).replace('\\','\\\\').replace('\r\n','\n').replace('\r','\n').replace('\n','\\n').replace(';','\\;').replace(',','\\,')


def fold_line(line):
    pieces=[];current='';size=0
    for character in line:
        length=len(character.encode('utf-8'))
        if size+length>75:
            pieces.append(current);current=' ';size=1
        current+=character;size+=length
    pieces.append(current)
    return '\r\n'.join(pieces)


def event_lines(event):
    lines=['BEGIN:VEVENT','UID:'+event['id']+'@wechat-helper.local',
           'SEQUENCE:'+str(event.get('revision',0)),
           'DTSTAMP:'+datetime.fromtimestamp(event['updated_at'],timezone.utc).strftime('%Y%m%dT%H%M%SZ')]
    if event['all_day']:
        lines+=['DTSTART;VALUE=DATE:'+event['start'].replace('-',''),'DTEND;VALUE=DATE:'+event['end'].replace('-','')]
    else:
        for field,key in [('DTSTART','start'),('DTEND','end')]:
            lines.append(field+':'+datetime.fromisoformat(event[key]).astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    if event['recurrence']=='yearly':lines.append('RRULE:FREQ=YEARLY')
    lines+=['SUMMARY:'+escape_text(event['title']),'DESCRIPTION:'+escape_text(event['description']),
            'LOCATION:'+escape_text(event['location']),'STATUS:CONFIRMED','END:VEVENT']
    return lines


def icalendar(items):
    lines=['BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//WechatHelper//Conversation Memory 1.0//ZH','CALSCALE:GREGORIAN']
    for event in items:
        if event['status']=='confirmed' and event['start']:lines+=event_lines(event)
    lines.append('END:VCALENDAR')
    return '\r\n'.join(fold_line(line) for line in lines)+'\r\n'


def validate_url(url):
    parsed=urlsplit(url)
    if parsed.scheme not in {'https','http'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('请填写有效的 CalDAV HTTP(S) 地址，不在地址中放入账号密码')
    if parsed.scheme=='http' and parsed.hostname not in {'127.0.0.1','localhost','::1'}:
        raise ValueError('远程日历连接请使用 HTTPS')
    return url.rstrip('/')+'/'


def origin(url):
    p=urlsplit(url);return p.scheme,p.hostname,p.port or (443 if p.scheme=='https' else 80)


def safe_href(base,href):
    url=validate_url(urljoin(base,href))
    if origin(url)!=origin(base):raise ValueError('日历服务器返回了不同域名的地址，请使用该服务的直接 CalDAV 地址')
    return url


def settings(account,public=True):
    cfg=storage.read_json('caldav.bin',{},secret=True).get(account,{})
    result={'url':'','username':'','password':'','calendar_url':'','auto_sync':False}|cfg
    if public:result['has_password']=bool(result.pop('password'))
    return result


def save_settings(account,value):
    with storage.WRITE_LOCK:
        all_settings=storage.read_json('caldav.bin',{},secret=True)
        previous=all_settings.get(account,{})
        url=validate_url(value.get('url',''))
        username=str(value.get('username','')).strip()
        if not username:raise ValueError('请填写 CalDAV 用户名')
        password=value.get('password','')
        same_login=previous.get('username')==username and origin(previous.get('url',url))==origin(url)
        if not password:
            if not same_login:raise ValueError('更换服务或用户名后请重新填写应用专用密码')
            password=previous.get('password','')
        if not password:raise ValueError('请填写应用专用密码')
        calendar_url=value.get('calendar_url','')
        if calendar_url:calendar_url=safe_href(url,calendar_url)
        all_settings[account]={'url':url,'username':username,'password':password,'calendar_url':calendar_url,'auto_sync':bool(value.get('auto_sync',False))}
        storage.write_json('caldav.bin',all_settings,secret=True)
    return settings(account)


def client(cfg):
    return httpx.AsyncClient(auth=(cfg['username'],cfg['password']),timeout=30,trust_env=False,follow_redirects=False)


def check_response(response):
    if response.status_code in {401,403}:raise ValueError('日历账号授权失败，请检查用户名、应用专用密码和日历写入权限')
    if 300<=response.status_code<400:raise ValueError('日历服务要求重定向，请填写服务的最终 CalDAV 地址')
    if response.is_error:raise ValueError(f'日历服务返回 HTTP {response.status_code}')


async def properties(connection,url,depth='0'):
    body='<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><d:displayname/><d:resourcetype/><d:current-user-principal/><c:calendar-home-set/></d:prop></d:propfind>'
    response=await connection.request('PROPFIND',url,content=body,headers={'Depth':depth,'Content-Type':'application/xml; charset=utf-8'})
    check_response(response)
    if len(response.content)>2*1024*1024:raise ValueError('日历目录响应过大')
    try:return ET.fromstring(response.content)
    except ET.ParseError as error:raise ValueError('服务器未返回有效的 CalDAV 目录') from error


async def discover(account):
    cfg=settings(account,False)
    if not cfg['url'] or not cfg['password']:raise ValueError('请先保存日历连接设置')
    async with client(cfg) as connection:
        root=await properties(connection,cfg['url'])
        if root.find('.//'+C+'calendar') is not None:
            return [{'name':root.findtext('.//'+D+'displayname') or '当前日历','url':cfg['url']}]
        home=root.findtext('.//'+C+'calendar-home-set/'+D+'href')
        if not home:
            principal=root.findtext('.//'+D+'current-user-principal/'+D+'href')
            if principal:
                root=await properties(connection,safe_href(cfg['url'],principal))
                home=root.findtext('.//'+C+'calendar-home-set/'+D+'href')
        base=safe_href(cfg['url'],home) if home else cfg['url']
        listing=await properties(connection,base,'1')
        result=[]
        for response in listing.findall(D+'response'):
            if response.find('.//'+C+'calendar') is None:continue
            href=response.findtext(D+'href')
            if href:result.append({'name':response.findtext('.//'+D+'displayname') or '日历','url':safe_href(base,href)})
        if not result:raise ValueError('未发现日历，请填写完整的日历集合地址后重试')
        return result


async def sync(account,users,check=lambda:None,active_users=None,retry_conflicts=True):
    cfg=settings(account,False)
    if not cfg['calendar_url']:raise ValueError('请先测试连接并选择目标日历')
    calendar_url=safe_href(cfg['url'],cfg['calendar_url'])
    candidates=[e for e in events(account,users) if e['status']=='confirmed' and e['sync_state']!='synced' and (retry_conflicts or e['sync_state']!='conflict')]
    result={'synced':0,'conflicts':0,'errors':0}
    async with client(cfg) as connection:
        for event in candidates:
            check()
            if event['user'] and active_users is not None and event['user'] not in active_users():continue
            target=calendar_url+quote(event['id'],safe='')+'.ics'
            if event['remote_url'] and event['remote_url']!=target:
                state='conflict';error='目标日历已更换，原日程仍在旧日历中，请手动处理后再迁移'
            else:
                headers={'Content-Type':'text/calendar; charset=utf-8'}
                if event['remote_url']:
                    if not event['remote_etag']:
                        state='conflict';error='远端未提供 ETag，无法安全更新；请在日历服务中处理'
                    else:
                        headers['If-Match']=event['remote_etag'];state='ready';error=''
                else:headers['If-None-Match']='*';state='ready';error=''
                if state=='ready':
                    try:
                        response=await connection.put(target,content=icalendar([event]).encode('utf-8'),headers=headers)
                        if response.status_code in {409,412}:
                            state='conflict';error='远端日程已发生变化，未覆盖，请到第三方日历检查'
                        else:
                            check_response(response);state='synced';error=''
                            etag=response.headers.get('etag','')
                            if not etag:
                                head=await connection.head(target)
                                if not head.is_error:etag=head.headers.get('etag','')
                    except (ValueError,httpx.RequestError) as exception:
                        state='error';error=str(exception) if isinstance(exception,ValueError) else '日历网络连接失败，请稍后重试'
            check()
            with store.database() as con:
                # Don't mark a concurrently edited event as synced with old content.
                current=con.execute('SELECT revision,status FROM events WHERE account=? AND id=?',(account,event['id'])).fetchone()
                if not current or current['revision']!=event['revision'] or current['status']!=event['status']:
                    if state=='synced':
                        con.execute('UPDATE events SET remote_url=?,remote_etag=? WHERE id=?',(target,etag,event['id']))
                    continue
                if state=='synced':
                    con.execute('UPDATE events SET sync_state=?,remote_url=?,remote_etag=?,sync_error=? WHERE id=?',(state,target,etag,error,event['id']))
                else:con.execute('UPDATE events SET sync_state=?,sync_error=? WHERE id=?',(state,error,event['id']))
            result[{'synced':'synced','conflict':'conflicts','error':'errors'}[state]]+=1
    return result
