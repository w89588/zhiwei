/* Listening workspace. Page-specific state stays out of the shared shell. */
const Workspace = (() => {
  let dirty = false;
  function appendSharedLink(container, message) {
    if (message.type !== 49 || !message.extra) return;
    const {des, url} = message.extra;
    if (des && des !== message.text) container.append(el('div', des));
    if (typeof url !== 'string' || !url) return;
    let parsed;
    try {
      parsed = new URL(url);
      if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) throw new Error();
    } catch { container.append(el('div', url)); return; }
    const link = el('a', url); link.href = parsed.href; link.target = '_blank';
    link.rel = 'noopener noreferrer'; link.referrerPolicy = 'no-referrer'; container.append(link);
  }
  const timers = new Set();
  function later(fn, delay = 5000) {
    const timer = setTimeout(() => { timers.delete(timer); fn(); }, delay);
    timers.add(timer);
  }
  function leave() {
    if (dirty && !confirm('有尚未保存的记忆修改，确定离开？')) return false;
    dirty = false;
    for (const timer of timers) clearTimeout(timer);
    timers.clear();
    return true;
  }
  window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
  function typeFilter(value = '') {
    const select = el('select'); select.setAttribute('aria-label', '通讯录类型');
    for (const [key, label] of [['', '全部类型'], ['person', '联系人'], ['group', '群聊']]) {
      const option = el('option', label); option.value = key; select.append(option);
    }
    select.value = value;
    return select;
  }
  async function addListener() {
    const box = modal('添加监听'), form = el('form', undefined, 'toolbar');
    const search = el('input'), type = typeFilter(), results = el('div'), controls = el('div', undefined, 'pagination');
    search.type = 'search'; search.placeholder = '搜索所有通讯录的昵称、备注或微信号'; search.setAttribute('aria-label', search.placeholder);
    const submit = el('button', '搜索'); submit.type = 'submit';
    form.append(search, type, submit); box.append(el('p', '选择联系人或群聊，添加到你的监听工作区。', 'muted'), form, results, controls);
    let sequence = 0, offset = 0, debounce;
    const load = async () => {
      const ticket = ++sequence;
      results.replaceChildren(el('p', '正在搜索…', 'muted'));
      try {
        const data = await api('workspace/contacts?' + new URLSearchParams({scope: 'all', q: search.value, contact_type: type.value, offset, limit: 20}));
        if (ticket !== sequence || !$('modal').open) return;
        results.replaceChildren(); controls.replaceChildren();
        if (!data.items.length) results.append(empty('没有匹配的通讯录', '等待客户端同步，或尝试其他关键词。', null, 'contacts'));
        for (const item of data.items) {
          const row = el('div', undefined, 'health-row');
          const add = button(item.listening ? '已监听' : '添加监听', async () => {
            await api(`workspace/contacts/${item.id}/listener`, {enabled: true});
            item.listening = true; add.textContent = '已监听';
            toast('已添加监听'); await route(); await load();
          }, item.listening ? '' : 'primary');
          add.disabled = item.listening;
          row.append(cell(item.remark || item.name, `${item.group ? '群聊' : '联系人'} · ${item.source_id}`), add); results.append(row);
        }
        const prev = button('上一页', async () => { offset = Math.max(0, offset - 20); await load(); }); prev.disabled = offset === 0;
        const next = button('下一页', async () => { offset += 20; await load(); }); next.disabled = !data.has_more;
        controls.append(el('span', `共 ${data.total} 项`), prev, next);
      } catch (error) { if (ticket === sequence) results.replaceChildren(el('p', error.message, 'notice error')); }
    };
    form.onsubmit = event => { event.preventDefault(); clearTimeout(debounce); offset = 0; load(); };
    search.oninput = () => { clearTimeout(debounce); debounce = setTimeout(() => { offset = 0; load(); }, 300); };
    type.onchange = () => { offset = 0; load(); };
    await load(); search.focus();
  }
  async function contactsPage(container, params) {
    const query = new URLSearchParams(params); query.set('limit', '50');
    const [data, accounts] = await Promise.all([api('workspace/contacts?' + query), accountFilter(params)]);
    const type = typeFilter(params.get('contact_type') || '');
    type.onchange = () => { const query = new URLSearchParams(params); query.set('contact_type', type.value); query.delete('offset'); navigate('contacts', Object.fromEntries(query)); };
    container.append(heading('通讯录', '你的监听工作区 · 持续整理联系人与群聊。', [button('添加监听', addListener, 'primary')]),
      searchbar(params, '搜索监听的昵称、备注或微信号', [accounts, type]));
    if (!data.items.length) container.append(empty('还没有匹配的监听对象', '点击添加监听，从所有通讯录中选择联系人或群聊。', button('添加监听', addListener, 'primary'), 'contacts'));
    else container.append(table(['监听对象', '昵称 / 备注', '通讯录类型', '整理周期', '操作'], data.items, item => [
      cell(item.remark || item.name, item.source_id), cell(item.nickname || '—', item.remark || ''), badge(item.group ? '群聊' : '联系人'),
      item.cycles.map(c => cycleNames[c]).join(' · '), actions(button('消息', () => navigate('sessions', {id: item.id})),
        button('监听设置', () => listenerDialog(item)), button('移出', () => confirmAction('移出监听', '停止自动分析，保留已同步的消息与记忆。', () => api(`sessions/${item.id}/listener`, {enabled: false}))))
    ]), pager(data, params));
  }
  async function sessionsPage(container, params) {
    container.append(heading('会话与消息', '选择监听对象，查看最近消息或向上加载历史。', [button('添加监听', addListener, 'primary')]));
    const split = el('div', undefined, 'workspace-split'), left = el('aside', undefined, 'workspace-list'), right = el('section', undefined, 'conversation');
    const search = el('input'), list = el('div', undefined, 'object-list'), more = el('div');
    search.type = 'search'; search.placeholder = '搜索监听对象'; search.setAttribute('aria-label', search.placeholder);
    left.setAttribute('aria-label', '监听通讯录'); right.setAttribute('aria-label', '消息历史');
    left.append(search, list, more); split.append(left, right); container.append(split);
    let offset = 0, sequence = 0, selected, debounce;
    const select = async item => {
      const ticket = ++sequence; selected = item;
      list.querySelectorAll('button[data-id]').forEach(n => n.classList.toggle('active', n.dataset.id === item.id));
      right.replaceChildren(el('p', '正在加载消息…', 'loading'));
      const head = el('div', undefined, 'conversation-heading'), transcript = el('div', undefined, 'transcript'), feedback = el('p', '', 'muted');
      transcript.tabIndex = 0; transcript.setAttribute('aria-label', '聊天消息');
      const messages = el('div', undefined, 'messages'); let older = '', newer = '', hasOlder = false, loading = false;
      const seen = new Map();
      function renderMessage(message) {
        const node = el('article', undefined, 'message' + (message.is_me ? ' me' : ''));
        const body = el('div', message.type === 47 ? '' : (message.text || `[${message.type_name || '非文本消息'}]`), 'message-text');
        appendSharedLink(body, message);
        const extra = message.extra || {}, details = [];
        const mediaCandidate = [3, 34, 43, 47].includes(message.type) ||
          (message.type === 49 && !!extra['appattach/totallen']);
        if (message.type === 47) {
          const image = el('img'); image.src = `/api/v2/messages/${encodeURIComponent(message.evidence_id)}/media`;
          image.alt = '聊天表情'; image.loading = 'lazy'; image.className = 'message-emoji';
          image.onerror = () => image.replaceWith(el('span', '表情暂不可用，请在微信中查看', 'message-meta'));
          body.append(image);
        } else if (mediaCandidate || message.media) {
          if (message.media) {
            const url = `/api/v2/messages/${encodeURIComponent(message.evidence_id)}/media`;
            if (message.type === 3) {
              const link = el('a'); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
              const image = el('img'); image.src = url; image.alt = '聊天图片'; image.loading = 'lazy'; image.className = 'message-image';
              image.onerror = () => { link.replaceWith(el('p', '图片加载失败，请稍后重试', 'message-meta')); };
              link.append(image); body.append(link);
              if (message.media.name.startsWith('image-thumbnail.')) body.append(el('div', '当前为微信缓存的缩略图', 'message-meta'));
            } else if (message.type === 43 && message.media.mime === 'video/mp4') {
              const video = el('video'); video.src = url; video.controls = true; video.preload = 'metadata'; video.className = 'message-video';
              body.append(video);
            } else {
              if (message.type === 34) {
                const audio = el('audio'); audio.src = url + '/play'; audio.controls = true; audio.preload = 'none';
                audio.className = 'message-audio'; body.append(audio);
              }
              const link = el('a', `下载${message.type === 34 ? '语音' : '文件'}：${message.media.name}`);
              link.href = url; link.download = message.media.name; body.append(link);
            }
          } else {
            body.append(el('div', '媒体尚未同步；请保持原微信客户端在线，必要时在微信中打开该文件。', 'message-meta'));
          }
        }
        if (message.type === 3 && extra.img_width && extra.img_height) details.push(`${extra.img_width} × ${extra.img_height}`);
        if (message.type === 43 && extra.videomsg_playlength) details.push(`时长 ${extra.videomsg_playlength} 秒`);
        if (message.type === 34 && extra.voicemsg_voicelength) details.push(`时长 ${Math.round(extra.voicemsg_voicelength / 100) / 10} 秒`);
        if (message.type === 49 && extra['appattach/fileext']) details.push(`格式 ${extra['appattach/fileext']}`);
        if (details.length) body.append(el('div', details.join(' · '), 'message-meta'));
        node.append(el('div', `${message.sender || '未知发送者'} · ${date(message.timestamp)}`, 'message-meta'), body);
        if (message.type !== 1 && message.raw) {
          const raw = el('details', undefined, 'message-raw'); raw.append(el('summary', '查看原始记录'), el('pre', message.raw)); node.append(raw);
        }
        node.dataset.evidence = message.evidence_id;
        node.dataset.media = JSON.stringify(message.media || null);
        node.dataset.mediaPending = String(mediaCandidate && !message.media);
        return node;
      }
      function append(items, prepend = false) {
        const fragment = document.createDocumentFragment();
        for (const message of items) {
          if (seen.has(message.evidence_id)) {
            const old = seen.get(message.evidence_id);
            if (old.dataset.media !== JSON.stringify(message.media || null)) {
              const updated = renderMessage(message); old.replaceWith(updated); seen.set(message.evidence_id, updated);
            }
            continue;
          }
          const node = renderMessage(message); seen.set(message.evidence_id, node); fragment.append(node);
        }
        if (prepend) messages.prepend(fragment); else messages.append(fragment);
      }
      const loadOlder = button('加载更早消息', async () => {
        const height = transcript.scrollHeight;
        const data = await api(`sessions/${item.id}/messages?` + new URLSearchParams({before: older, limit: 50, count: false}));
        if (ticket !== sequence) return;
        append(data.items, true); older = data.older_cursor || older; hasOlder = data.has_more;
        loadOlder.hidden = !hasOlder; transcript.scrollTop += transcript.scrollHeight - height;
      });
      try {
        const data = await api(`sessions/${item.id}/messages?limit=50&count=false`);
        if (ticket !== sequence) return;
        head.append(cell(item.remark || item.name, item.group ? '群聊 · 已监听' : '联系人 · 已监听'), button('监听设置', () => listenerDialog(item)));
        older = data.older_cursor; newer = data.newer_cursor; hasOlder = data.has_more;
        loadOlder.hidden = !hasOlder; append(data.items);
        transcript.append(loadOlder, messages);
        if (!data.items.length) feedback.textContent = '暂无已同步消息，客户端会在后台补齐历史。';
        right.replaceChildren(head, transcript, feedback); transcript.scrollTop = transcript.scrollHeight;
        let polls = 0, pendingCursor = 0;
        async function poll() {
          if (ticket !== sequence || !right.isConnected || document.hidden) { if (ticket === sequence && right.isConnected) later(poll); return; }
          if (loading) return; loading = true;
          try {
            const nearBottom = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 80;
            const initiallyEmpty = !newer;
            const data = await api(`sessions/${item.id}/messages?` + new URLSearchParams({after: newer, limit: 100, count: false}));
            if (ticket !== sequence) return;
            append(data.items); newer = data.newer_cursor || newer;
            if (++polls % 3 === 0) {
              const recent = await api(`sessions/${item.id}/messages?limit=50&count=false`);
              if (ticket !== sequence) return;
              append(recent.items);
              const pending = Array.from(seen.values()).filter(node => node.dataset.mediaPending === 'true');
              if (pending.length) {
                const selected = Array.from({length: Math.min(5, pending.length)}, (_, i) => pending[(pendingCursor + i) % pending.length]);
                pendingCursor = (pendingCursor + selected.length) % pending.length;
                const updated = await Promise.all(selected.map(node => api(`messages/${encodeURIComponent(node.dataset.evidence)}`).catch(() => null)));
                if (ticket !== sequence) return;
                append(updated.filter(Boolean));
              }
            }
            if (!older) older = data.older_cursor;
            if (initiallyEmpty && data.has_more) { hasOlder = true; loadOlder.hidden = false; }
            feedback.textContent = data.items.length ? '已更新新消息' : '已追平当前消息';
            if (nearBottom) transcript.scrollTop = transcript.scrollHeight;
            later(poll, data.has_more ? 300 : 5000);
          } catch (error) { feedback.textContent = error.message + '，稍后重试'; later(poll); }
          finally { loading = false; }
        }
        later(poll);
      } catch (error) { if (ticket === sequence) right.replaceChildren(el('p', error.message, 'notice error')); }
    };
    const load = async reset => {
      if (reset) { offset = 0; list.replaceChildren(); }
      const data = await api('workspace/contacts?' + new URLSearchParams({q: search.value, account_id: params.get('account_id') || '', offset, limit: 50}));
      for (const item of data.items) {
        const node = button('', () => select(item), 'object-item'); node.dataset.id = item.id;
        node.append(el('span', (item.remark || item.name).slice(0, 1), 'object-avatar'), cell(item.remark || item.name, item.group ? '群聊' : '联系人'));
        if (selected?.id === item.id) node.classList.add('active'); list.append(node);
      }
      more.replaceChildren();
      if (data.has_more) more.append(button('加载更多监听对象', async () => { offset += 50; await load(false); }));
      if (!selected) {
        let initial = data.items.find(i => i.id === params.get('id'));
        if (!initial && params.get('id')) initial = (await api('workspace/contacts?identity=' + encodeURIComponent(params.get('id')))).items[0];
        initial ||= data.items[0];
        if (initial) await select(initial);
        else right.replaceChildren(empty('选择你的第一位监听对象', '添加监听后，在这里查看历史对话。', button('添加监听', addListener, 'primary'), 'sessions'));
      }
    };
    search.oninput = () => { clearTimeout(debounce); debounce = setTimeout(() => load(true).catch(e => toast(e.message)), 300); };
    await load(true);
  }
  function markdownPreview(text) {
    const node = el('div', undefined, 'markdown rendered-markdown');
    node.innerHTML = DOMPurify.sanitize(marked.parse(text || ''), {USE_PROFILES: {html: true}, FORBID_TAGS: ['img', 'style', 'iframe', 'form', 'input']});
    node.querySelectorAll('a').forEach(a => { a.target = '_blank'; a.rel = 'noopener noreferrer'; });
    return node;
  }
  async function memoriesPage(container, params) {
    const data = await api('workspace/memories');
    container.append(heading('记忆中心', '按对象与记忆类型整理文件 · 默认预览，按需编辑。'));
    const split = el('div', undefined, 'workspace-split memory-workspace'), tree = el('aside', undefined, 'directory-tree'), pane = el('section', undefined, 'memory-pane');
    tree.setAttribute('aria-label', '记忆目录'); pane.setAttribute('aria-label', 'Markdown 阅读和编辑区');
    split.append(tree, pane); container.append(split);
    let ticket = 0, active, first;
    async function open(doc, group) {
      if (dirty && !confirm('有尚未保存的修改，确定打开其他文件？')) return;
      dirty = false; active = doc.id; const current = ++ticket;
      tree.querySelectorAll('button').forEach(n => n.classList.toggle('active', n.dataset.id === doc.id));
      pane.replaceChildren(el('p', '正在读取 Markdown…', 'loading'));
      try {
        const memory = await api('memories/' + doc.id);
        if (current !== ticket) return;
        let editing = false;
        const title = el('div', undefined, 'memory-heading'), body = el('div', undefined, 'memory-body'), controls = el('div', undefined, 'actions');
        body.tabIndex = 0; body.setAttribute('aria-label', 'Markdown 文档');
        const text = el('textarea', undefined, 'editor markdown-source'); text.value = memory.markdown;
        text.setAttribute('aria-label', 'Markdown 编辑器'); text.spellcheck = false; text.oninput = () => { dirty = text.value !== memory.markdown; };
        const status = el('p', `预览模式 · 版本 ${memory.latest_version}`, 'muted');
        const save = button('保存修改', async () => {
          try {
            const result = await api('memories/' + doc.id, {version: memory.latest_version, markdown: text.value}, 'PUT');
            memory.markdown = text.value; memory.latest_version = result.version; dirty = false; editing = false;
            doc.version = result.version;
            memory.history.unshift({version: result.version, manual: true, created_at: Math.floor(Date.now()/1000)});
            const file = [...tree.querySelectorAll('button[data-id]')].find(n => n.dataset.id === doc.id);
            if (file) file.textContent = `${doc.period_start ? shortDate(doc.period_start) : '当前'} · v${doc.version}.md`;
            render(); toast('记忆已保存');
          } catch (error) { status.textContent = error.message + '。修改内容已保留，可复制后重新打开最新版本。'; throw error; }
        }, 'primary');
        const edit = button('编辑 Markdown', () => { editing = true; render(); text.focus(); });
        function history() {
          const box = modal('记忆版本历史');
          box.append(table(['版本','来源','时间','操作'], memory.history, version => [
            `v${version.version}`, version.manual ? '人工编辑' : '自动整理', date(version.created_at),
            button('预览', async () => {
              const archived = await api(`memories/${doc.id}?version=${version.version}`);
              box.replaceChildren(el('h2', `历史版本 v${version.version}`), markdownPreview(archived.markdown),
                button('返回版本列表', history));
            })]));
        }
        const cancel = button('取消编辑', () => { if (!dirty || confirm('放弃未保存的修改？')) { text.value = memory.markdown; dirty = false; editing = false; render(); } });
        const toggle = button('预览', () => {
          const visible = !text.hidden; text.hidden = visible; preview.hidden = !visible; toggle.textContent = visible ? '继续编辑' : '预览';
        });
        const toolbar = el('div', undefined, 'markdown-toolbar'); let preview;
        for (const [label, start, end] of [['标题', '## ', ''], ['粗体', '**', '**'], ['斜体', '*', '*'], ['列表', '- ', ''], ['引用', '> ', ''], ['代码', '`', '`'], ['链接', '[', '](https://)']]) {
          toolbar.append(button(label, () => {
            const from = text.selectionStart, to = text.selectionEnd, selected = text.value.slice(from, to) || '文字';
            text.setRangeText(start + selected + end, from, to, 'select'); text.dispatchEvent(new Event('input')); text.focus();
          }));
        }
        function render() {
          controls.replaceChildren(...(editing ? [toggle, cancel, save] : [edit, button('版本历史', history), link('下载 Markdown', `/api/v2/memories/${doc.id}/download`)]));
          preview = markdownPreview(editing ? text.value : memory.markdown);
          toggle.textContent = '预览'; text.hidden = false; preview.hidden = editing;
          body.replaceChildren(...(editing ? [toolbar, text, preview] : [preview]));
          status.textContent = `${editing ? '编辑模式' : '预览模式'} · 版本 ${memory.latest_version}`;
        }
        // Re-render the current source each time preview is requested.
        toggle.onclick = () => {
          if (!text.hidden) { preview.replaceWith(preview = markdownPreview(text.value)); text.hidden = true; toggle.textContent = '继续编辑'; }
          else { preview.hidden = true; text.hidden = false; toggle.textContent = '预览'; text.focus(); }
        };
        title.append(cell(`${group.name} / ${cycleNames[doc.kind] || doc.kind}`, 'Markdown · ' + (doc.period_start ? shortDate(doc.period_start) : '持续更新')), controls);
        pane.replaceChildren(title, body, status); render();
      } catch (error) { if (current === ticket) pane.replaceChildren(el('p', error.message, 'notice error')); }
    }
    let requested;
    for (const group of data.items) {
      const requestedKind = Object.entries(group.types).find(([, docs]) => docs.some(d => d.id === params.get('id')))?.[0];
      const folder = el('details'); folder.open = group.id === 'personal' || !!requestedKind; folder.append(el('summary', group.name));
      if (group.account_id) folder.querySelector('summary').title = '微信账号 ' + group.account_id;
      for (const [kind, docs] of Object.entries(group.types)) {
        const category = el('details'); category.open = group.id === 'personal' || kind === requestedKind; category.append(el('summary', `${cycleNames[kind] || kind} (${docs.length})`));
        let loaded = 0;
        function loadFiles() {
          category.querySelector('.load-files')?.remove();
          for (const doc of docs.slice(loaded, loaded+100)) {
            const node = button(`${doc.period_start ? shortDate(doc.period_start) : '当前'} · v${doc.version}.md`, () => open(doc, group), 'file-item');
            node.dataset.id = doc.id; node.classList.toggle('active', doc.id === active); category.append(node);
          }
          loaded += 100;
          if (loaded < docs.length) category.append(button('加载更多文件', loadFiles, 'load-files'));
        }
        category.ontoggle = () => { if (category.open && !loaded) loadFiles(); };
        if (category.open) loadFiles();
        for (const doc of docs) {
          first ||= [doc, group];
          if (doc.id === params.get('id')) requested = [doc, group];
        }
        folder.append(category);
      }
      if (!Object.keys(group.types).length) folder.append(el('p', '尚无记忆文件', 'muted')); tree.append(folder);
    }
    if (data.truncated) tree.append(el('p', '目录达到显示上限，请按周期归档后查看。', 'notice'));
    if (params.get('id') && !requested) pane.append(empty('此文件不在当前监听目录', '已保存的历史记忆仍保留；恢复该会话的监听后可在目录中查看。', button('管理通讯录', () => navigate('contacts'))));
    else if (requested || first) await open(...(requested || first));
    else pane.append(empty('你的记忆目录还没有文件', '监听会话并配置模型，完整周期结束后会自动整理。', button('查看自动分析', () => navigate('jobs'))));
  }
  async function jobsPage(container, params) {
    const [data, model, taskPage] = await Promise.all([api('workspace/analysis?' + new URLSearchParams({offset: params.get('offset') || 0, limit: 30})), api('model'), api('manage/jobs?limit=100')]);
    container.append(heading('自动分析', '按监听对象查看整理进度，历史任务在后台持续处理。', [button('模型配置', () => navigate('model'))]));
    if (!model.configured || !model.enabled) container.append(el('p', '等待模型配置。聊天仍会同步，配置并启用模型后继续整理。', 'notice'));
    const metrics = el('div', undefined, 'metrics'), values = [], progress = new Map();
    for (const [label, value] of [['正在整理', data.summary.running || 0], ['等待整理', Math.max(0, (data.summary.queued || 0) - data.scheduled) + (data.summary.waiting_history || 0) + (data.summary.waiting_model || 0)], ['已完成', data.summary.done || 0], ['需要处理', data.summary.failed || 0]]) {
      const tile = el('section', undefined, 'metric'), number = el('div', String(value), 'metric-value');
      values.push(number); tile.append(el('div', label, 'metric-label'), number); metrics.append(tile);
    }
    container.append(metrics, el('p', '历史同步完成后，按周期结束时间从早到近整理；未结束的周期不会提前调用模型。新消息变更会合并处理，人物记忆仅在有可靠的新事实时增量更新。', 'notice'));
    if (data.scheduled) container.append(el('p', `${data.scheduled} 个周期尚未结束，到期后自动整理。`, 'muted'));
    if (data.budget_waiting) container.append(el('p', `${data.budget_waiting} 个任务已达到今日模型调用上限，将于明日自动继续。`, 'notice'));
    if (!data.items.length) container.append(empty('还没有监听对象', '从通讯录添加监听，开始积累聊天记忆。', button('添加监听', addListener, 'primary'), 'jobs'));
    for (const item of data.items) {
      const panel = card(item.name, button('监听设置', () => listenerDialog({...item, listening: true})));
      const chips = el('div', undefined, 'chip-list');
      progress.set(item.id, chips);
      for (const [state, count] of Object.entries(item.counts)) chips.append(badge(`${statusNames[state] || state} ${count}`, state === 'failed' ? 'red' : state === 'running' ? 'green' : ''));
      panel.append(el('p', `${item.group ? '群聊' : '联系人'} · ${item.cycles.map(c => cycleNames[c]).join(' / ')}`, 'muted'), chips);
      if (!Object.keys(item.counts).length) panel.append(el('p', '等待已同步消息进入完整统计周期。', 'muted'));
      container.append(panel);
    }
    const refreshState = el('p', '运行进度每 10 秒更新', 'muted'); container.append(refreshState);
    async function refreshProgress() {
      if (!metrics.isConnected) return;
      if (document.hidden) { later(refreshProgress, 10000); return; }
      try {
        const latest = await api('workspace/analysis?' + new URLSearchParams({offset: params.get('offset') || 0, limit: 30}));
        if (!metrics.isConnected) return;
        const summary = latest.summary;
        [summary.running || 0, Math.max(0, (summary.queued || 0) - latest.scheduled) + (summary.waiting_history || 0) + (summary.waiting_model || 0), summary.done || 0, summary.failed || 0]
          .forEach((value, index) => { values[index].textContent = String(value); });
        for (const item of latest.items) {
          progress.get(item.id)?.replaceChildren(...Object.entries(item.counts).map(([state, count]) =>
            badge(`${statusNames[state] || state} ${count}`, state === 'failed' ? 'red' : state === 'running' ? 'green' : '')));
        }
        refreshState.textContent = '运行进度已更新 · ' + new Date().toLocaleTimeString('zh-CN');
      } catch (error) { refreshState.textContent = '进度暂时未更新：' + error.message; }
      later(refreshProgress, 10000);
    }
    later(refreshProgress, 10000);
    container.append(pager(data, params, 30));
    if (data.failures.length) {
      const panel = card('需要处理的任务');
      for (const failure of data.failures) {
        const row = el('div', undefined, 'health-row');
        row.append(cell(`${cycleNames[failure.cycle]} · ${data.items.find(i => i.id === failure.session_id)?.name || '监听会话'}`, failure.error),
          button('重试（可能再次计费）', async () => { await api(`jobs/${failure.id}/retry`, {}); await route(); })); panel.append(row);
      }
      container.append(panel);
    }
    const warnings = taskPage.items.filter(item => item.state === 'done' && item.error?.startsWith('已跳过 ')).slice(0, 10);
    if (warnings.length) {
      const panel = card('近期未纳入的记忆建议');
      panel.append(el('p', '这些建议缺少明确证据或本人归属，聊天总结已保存，建议未写入人物记忆。', 'muted'));
      for (const item of warnings) panel.append(cell(`${cycleNames[item.cycle]} · ${date(item.period_start)}`, item.error));
      container.append(panel);
    }
  }
  return {leave, contacts: contactsPage, sessions: sessionsPage, memories: memoriesPage, jobs: jobsPage};
})();
