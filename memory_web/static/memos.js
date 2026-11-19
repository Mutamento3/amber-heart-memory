// The secretary proposes changes; this page builds the text the user saves.
export const SECTIONS = {period: '月经周期', health: '健康关注', longterm: '长期事项', daily: '日常杂记'};
export const lines = (text) => String(text || '').split('\n').filter(line => line.trim());
export const count = (text) => Array.from(String(text || '').replace(/\s/g, '')).length;
const total = (sections) => Object.values(sections).reduce((n, text) => n + count(text), 0);
const same = (a, b) => Object.keys(SECTIONS).every(key => a[key] === b[key]);
const starred = (text) => text.trimStart().startsWith('★');
const withoutStar = (text) => text.trimStart().replace(/^★\s*/, '');
const entryText = (value) => value.replace(/[\r\n]+/g, ' ').trim();
function startDate(text) {
  const head = text.trimStart().replace(/^★\s*/, '');
  const match = head.match(/^(\d{4})年(\d{1,2})月(\d{1,2})日/) || head.match(/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?!\d)/);
  if (!match) return null;
  const [year, month, day] = match.slice(1).map(Number), date = new Date(0);
  date.setUTCFullYear(year, month - 1, day);
  return year > 0 && date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day ? year * 10000 + month * 100 + day : null;
}
function orderLongterm(items) {
  const dated = items.map((item, index) => ({item, index, date: startDate(item.text)}))
    .filter(row => row.date !== null).sort((a, b) => b.date - a.date || a.index - b.index);
  let next = 0;
  // Unknown dates keep their slots; equal dates keep their existing order.
  return items.map(item => startDate(item.text) === null ? item : dated[next++].item);
}
export function tidyPreview(job, selected) {
  const sections = {...job.base_sections}, rows = {};
  const chosen = (job.suggestions || []).filter(s => selected.has(s.id));
  for (const section of Object.keys(SECTIONS)) {
    const entries = Object.entries(job.entries || {}).filter(([, e]) => e.section === section).sort((a, b) => a[1].index - b[1].index);
    const replacements = new Map(), removed = new Set();
    for (const change of chosen.filter(s => s.section === section)) {
      const targets = [...change.targets].sort((a, b) => job.entries[a].index - job.entries[b].index);
      replacements.set(targets[0], change);
      targets.slice(1).forEach(id => removed.add(id));
    }
    let items = [];
    for (const [key, entry] of entries) {
      if (removed.has(key)) continue;
      const change = replacements.get(key);
      if (change?.type === '删除') continue;
      const targets = change ? change.targets : [key];
      items.push({text: change ? change.new_text : entry.text, changed: !!change,
        questions: (job.need_confirm || []).filter(q => targets.includes(q.target)).map(q => q.question)});
    }
    if (section === 'longterm' && chosen.some(s => s.section === section)) items = orderLongterm(items);
    rows[section] = items.flatMap(item => lines(item.text).map(text => ({...item, text})));
    // Keep untouched sections byte-for-byte, including manually entered spacing.
    if (chosen.some(s => s.section === section)) sections[section] = rows[section].map(row => row.text).join('\n');
  }
  return {sections, rows};
}

export async function coreMemoryPage(main, {api, read, url, esc, state, epoch, fitTextarea, autoSave, wireAutoSave, saveStatusHTML}) {
  const config = await read(url('/configuration'));
  if (state.epoch !== epoch || !main.isConnected) return;
  const $ = (selector, parent = main) => parent.querySelector(selector);
  // Reads like a page: the text grows with its content instead of scrolling inside a box,
  // and the count/save line stays in view while a long text is scrolled.
  main.innerHTML = `<section class="memo-core"><form id="core-form" class="card"><div class="core-head"><span class="meta" id="core-count"></span><span class="core-status">${saveStatusHTML()}<span class="edit-hint">点击空白处可编辑</span></span></div><label class="sr-only" for="core-memory">核心记忆正文</label><textarea id="core-memory" name="core_memory" rows="1" placeholder="那些始终想让 TA 记得的事…">${esc(config.core_memory)}</textarea></form></section>`;
  const core = $('#core-form'), input = $('#core-memory');
  function coreCount() {
    const size = Array.from(input.value).length;
    $('#core-count').textContent = `${size} / 5000 字`;
    $('#core-count').classList.toggle('error', size > 5000);
  }
  const fit = () => fitTextarea(input);
  input.oninput = () => { coreCount(); fit(); };
  input.addEventListener('focus', () => requestAnimationFrame(fit));
  input.addEventListener('blur', () => requestAnimationFrame(fit));
  coreCount();
  fit();
  const coreSave = autoSave($('.autosave-status', core), () => input.value, async (value) => {
    if (Array.from(value).length > 5000) throw new Error('核心记忆须为 5000 字以内的文字');
    const latest = await api(url('/core-memory'), {
      method: 'PATCH',
      body: {expected_revision: config.revision, core_memory: value},
    });
    Object.assign(config, latest);
  });
  wireAutoSave(core, coreSave);
  core.onsubmit = event => event.preventDefault();

}

export async function memosPage(main, {api, read, keepView, url, esc, state, epoch, message, busy, fitTextarea, autoSave, wireAutoSave, saveStatusHTML}) {
  const $ = (s, parent = main) => parent.querySelector(s);
  const $$ = (s, parent = main) => [...parent.querySelectorAll(s)];
  const live = () => state.epoch === epoch && main.isConnected;
  const [initial, pending, secretary] = await Promise.all([
    read(url('/recent-notes')), read(url('/bookmarks')), read(url('/recent-notes/tidy')),
  ]);
  if (!live()) return;
  let saved = initial, sections = {...saved.sections}, job = secretary.job, selected = new Set(), undo = null, markers = {}, adopted = null;
  let activeSection = 'health', editor = null, openMenu = null, stale = false, bookmarks = pending.items;
  // Sections switched off in settings are left out of the read, so they stay hidden here.
  const shownSections = () => Object.entries(SECTIONS).filter(([key]) => Object.hasOwn(saved.sections, key));
  const showError = (error, target = $('#memos-error')) => { if (live()) target.textContent = error.message; };
  const clearError = () => { $('#memos-error').textContent = ''; };
  const delta = (n) => n > 0 ? `+${n}` : `${n}`;
  function adoptJob(latest) {
    if (latest.id !== job?.id || (latest.status === 'done' && job?.status !== 'done')) selected = new Set((latest.suggestions || []).map(s => s.id));
    job = latest;
  }
  adoptJob(job);
  selected = new Set((job.suggestions || []).map(s => s.id));
  main.innerHTML = `<div class="memos-page">
    <div class="collection-tabs" role="tablist" aria-label="记忆书签分栏">
      <button type="button" id="organized-tab" role="tab" aria-controls="organized-bookmarks" aria-selected="true">已整理</button>
      <button type="button" id="pending-tab" role="tab" aria-controls="pending-panel" aria-selected="false" tabindex="-1">待整理</button>
    </div>
    <section class="memo-recent" id="organized-bookmarks" role="tabpanel" aria-label="已整理的书签" tabindex="0">
      <header class="memo-bar">
        <span class="meta"><span id="notes-count"></span> · 每轮聊天都会带给 TA</span>
        <button type="button" id="open-tidy" aria-controls="tidy-panel" aria-expanded="false">秘书整理</button>
      </header>
      <p class="muted memo-legend" id="starred-note-help" hidden>★ 标记的条目，秘书只能压缩，不能删除</p>
      <p class="memo-limit" id="notes-limit" hidden></p>
      <div id="adoption-notice" hidden></div>
      <p class="error" id="memos-error" role="alert"></p>
      <div id="notes-status">${saveStatusHTML()}<button type="button" class="quiet" id="reload-notes" hidden>重新读取书签</button></div>
      <section id="tidy-panel" class="card memo-tidy" aria-labelledby="tidy-title" hidden></section>
      <div id="notes-sections"></div>
    </section>
    <section class="memo-pending" id="pending-panel" role="tabpanel" aria-labelledby="pending-tab" tabindex="0" hidden>
      <p class="meta memo-bar" id="pending-help"></p>
      <p class="error" id="pending-error" role="alert"></p>
      <div id="pending-bookmarks"></div>
    </section>
  </div>`;
  const page = $('.memos-page');
  const navigation = $('.topbar nav', main.closest('.shell'));
  const panel = $('#tidy-panel'), tidyButton = $('#open-tidy'), reloadButton = $('#reload-notes');
  function setTidyOpen(open) {
    panel.hidden = !open;
    tidyButton.setAttribute('aria-expanded', String(open));
  }
  function requireReload(required) {
    stale = required;
    reloadButton.hidden = !required;
  }

  function noteDraft() {
    if (!editor) return sections;
    const {key, index, textarea, original} = editor;
    const text = entryText(textarea.value);
    if (!text || text === original.trim()) return sections;
    const items = lines(sections[key]);
    if (index === null) items.unshift(text);
    else items[index] = text;
    return {...sections, [key]: items.join('\n')};
  }
  const notesStatus = $('#notes-status');
  const notesSave = autoSave($('.autosave-status', notesStatus), noteDraft, async (draft) => {
    const tidyId = adopted;
    const body = {expected_version: saved.version, sections: draft};
    if (tidyId) body.tidy_job_id = tidyId;
    try {
      const result = await api(url('/recent-notes'), {method: 'PUT', body});
      saved = {...saved, version: result.version, sections: draft};
      if (tidyId && job.id === tidyId) job = {...job, status: 'applied'};
      if (adopted === tidyId) adopted = null;
      requireReload(false);
    } catch (error) {
      if (error.status === 409) requireReload(true);
      throw error;
    } finally {
      if (live()) {
        if (tidyId) renderAdoption();
        if (!panel.hidden) renderTidy();
      }
    }
  }, {fadeSaved: true});
  const tabs = $$('.collection-tabs [role="tab"]');
  function selectTab(active) {
    if (active.getAttribute('aria-selected') === 'true') return;
    if (active.id === 'pending-tab') {
      closeMenu();
      finishEdit();
    }
    tabs.forEach(tab => {
      const selected = tab === active;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
      $(`#${tab.getAttribute('aria-controls')}`).hidden = !selected;
    });
    if (editor && active.id === 'organized-tab') fitTextarea(editor.textarea);
  }
  tabs.forEach((tab, index) => {
    const other = tabs[1 - index];
    const keys = {ArrowLeft: other, ArrowRight: other, Home: tabs[0], End: tabs[1]};
    tab.onclick = () => selectTab(tab);
    tab.onkeydown = event => {
      if (!Object.hasOwn(keys, event.key)) return;
      event.preventDefault();
      const target = keys[event.key];
      selectTab(target);
      target.focus();
    };
  });
  function renderNotesCount() {
    const current = noteDraft();
    const size = total(current);
    $('#notes-count').textContent = `${size} 字`;
    $('#notes-limit').hidden = size <= saved.soft_limit;
    $('#notes-limit').textContent = `已超过 ${saved.soft_limit} 字的字数线，可以请秘书整理，目标约 ${saved.target} 字。`;
    $('#starred-note-help').hidden = !Object.values(current).some(text => lines(text).some(starred));
    $$('.memo-section').forEach(block => { $('.memo-section-count', block).textContent = `${count(current[block.dataset.section])} 字`; });
  }
  function renderAdoption() {
    $('#adoption-notice').hidden = !undo;
    $('#adoption-notice').innerHTML = undo ? `<span>${adopted ? '采纳的建议待保存。' : '已采纳到记忆书签。'}</span><button type="button" id="undo-tidy" class="quiet">撤销采纳</button>` : '';
    if (undo) $('#undo-tidy').onclick = () => {
      if (state.busy) return;
      if (!same(noteDraft(), undo.preview) && !confirm('撤销采纳也会还原采纳后的手动修改。继续？')) return;
      sections = {...undo.sections}; undo = null; adopted = null; markers = {}; editor = null;
      notesSave.changed(true);
      renderNotes(); renderTidy();
    };
  }
  function renderNotes() {
    closeMenu();
    panel.before(notesStatus);
    renderNotesCount();
    renderAdoption();
    // Each section is its own card, numbered like the self-hosted snapshot page.
    $('#notes-sections').innerHTML = shownSections().map(([key, label], number) => `
      <section class="card memo-section" data-section="${key}" aria-labelledby="notes-${key}">
        <header class="memo-section-head"><span class="memo-no" aria-hidden="true">${String(number + 1).padStart(2, '0')}</span><h3 id="notes-${key}">${label}</h3><span class="meta memo-section-count"></span><button type="button" class="quiet add-note">＋ 添加</button></header>
        <div class="memo-rows">${lines(sections[key]).map((_, index) => `<article class="memo-row" data-index="${index}"></article>`).join('')}</div>
      </section>`).join('');
    $$('.memo-section').forEach(block => {
      const key = block.dataset.section;
      const items = lines(sections[key]);
      $('.add-note', block).onclick = () => editRow(key, $('.memo-rows', block));
      $$('.memo-row', block).forEach((row, index) => renderNote(row, key, items[index]));
    });
    renderNotesCount();
  }
  function numberRows(list) {
    $$('.memo-row[data-index]', list).forEach((row, index) => { row.dataset.index = index; });
  }
  function closeMenu() {
    if (!openMenu) return;
    $('.memo-note-menu', openMenu).hidden = true;
    $('.memo-more', openMenu).setAttribute('aria-expanded', 'false');
    openMenu = null;
  }
  function renderNote(row, key, text) {
    const index = Number(row.dataset.index);
    const important = starred(text);
    const mark = markers[key]?.[index];
    const questions = mark?.text === text ? (mark.questions || []) : [];
    const hasStatus = row.contains(notesStatus);
    row.classList.toggle('memo-changed', Boolean(mark?.text === text && mark.changed));
    row.innerHTML = `<p class="memo-text">${important ? '<span class="memo-star" aria-label="已标星">★</span>' + esc(withoutStar(text)) : esc(text)}</p>
      <div class="memo-note-actions">
        <button type="button" class="quiet memo-more" aria-label="条目操作" aria-expanded="false">⋯</button>
        <div class="memo-note-menu" hidden>
          <button type="button" class="quiet star-note">${important ? '取消标星' : '标星'}</button>
          <button type="button" class="quiet edit-note">修改</button>
          <button type="button" class="danger delete-note">删除</button>
        </div>
      </div>
      ${questions.map(q => `<p class="tidy-question">秘书拿不准：${esc(q)}</p>`).join('')}`;
    if (hasStatus) row.append(notesStatus);
    const actions = $('.memo-note-actions', row), more = $('.memo-more', row);
    more.onclick = () => {
      const opening = openMenu !== actions;
      closeMenu();
      if (opening) {
        const menu = $('.memo-note-menu', actions);
        menu.hidden = false;
        const bounds = actions.getBoundingClientRect(), nav = navigation.getBoundingClientRect();
        const visibleBottom = nav.height && getComputedStyle(navigation).position === 'fixed' ? nav.top : innerHeight;
        const spaceBelow = visibleBottom - bounds.bottom;
        menu.classList.toggle('opens-up', menu.offsetHeight > spaceBelow && bounds.top > spaceBelow);
        more.setAttribute('aria-expanded', 'true');
        openMenu = actions;
      }
    };
    actions.onkeydown = event => {
      if (event.key !== 'Escape' || openMenu !== actions) return;
      event.preventDefault();
      closeMenu();
      more.focus();
    };
    actions.onfocusout = event => {
      if (openMenu === actions && event.relatedTarget && !actions.contains(event.relatedTarget)) closeMenu();
    };
    $('.edit-note', row).onclick = () => {
      closeMenu();
      editRow(key, row.parentElement, row);
    };
    $('.delete-note', row).onclick = () => changeNote(row, key, true);
    $('.star-note', row).onclick = () => changeNote(row, key, false);
  }
  function changeNote(row, key, deleting) {
    if (state.busy || editor) return;
    closeMenu();
    if (deleting && !confirm('删除这条书签？')) return;
    const list = row.parentElement, index = Number(row.dataset.index);
    const items = lines(sections[key]);
    if (deleting) items.splice(index, 1);
    else items[index] = starred(items[index]) ? withoutStar(items[index]) : '★' + items[index];
    sections = {...sections, [key]: items.join('\n')};
    if (deleting) {
      row.before(notesStatus);
      row.remove();
      numberRows(list);
    } else {
      row.append(notesStatus);
      renderNote(row, key, items[index]);
      $('.memo-more', row).focus({preventScroll: true});
    }
    notesSave.changed(true);
    renderNotesCount();
    renderTidy();
  }
  function editRow(key, list, row) {
    if (state.busy || editor) return;
    const index = row ? Number(row.dataset.index) : null;
    if (!row) {
      row = document.createElement('article');
      row.className = 'memo-row';
      list.prepend(row);
    }
    const hasStatus = row.contains(notesStatus);
    const form = document.createElement('form');
    form.className = 'memo-line-editor';
    const original = index === null ? '' : lines(sections[key])[index];
    form.innerHTML = `<textarea rows="1" aria-label="编辑${SECTIONS[key]}书签">${esc(original)}</textarea>`;
    row.replaceChildren(form);
    if (hasStatus) form.append(notesStatus);
    const textarea = $('textarea', form);
    editor = {key, index, textarea, original, row};
    fitTextarea(textarea);
    textarea.oninput = () => {
      const text = entryText(textarea.value);
      if (text && text !== original.trim() && !form.contains(notesStatus)) form.append(notesStatus);
      fitTextarea(textarea);
      renderNotesCount();
    };
    wireAutoSave(form, notesSave);
    form.onsubmit = event => event.preventDefault();
    form.onkeydown = event => {
      if (event.key !== 'Escape' || event.isComposing) return;
      event.preventDefault();
      finishEdit();
      if (!editor) ($('.memo-more', row) || $('.add-note', list.parentElement)).focus();
    };
    textarea.focus();
  }
  function finishEdit() {
    if (!editor) return;
    const {row, key, index, textarea} = editor;
    sections = noteDraft();
    editor = null;
    if (index === null && !entryText(textarea.value)) {
      if (row.contains(notesStatus)) row.before(notesStatus);
      row.remove();
    } else {
      if (index === null) {
        row.dataset.index = 0;
        numberRows(row.parentElement);
      }
      renderNote(row, key, lines(sections[key])[index ?? 0]);
    }
    notesSave.flush();
    renderNotesCount();
  }
  const outsideClicks = new AbortController();
  document.addEventListener('click', event => {
    if (!live()) return;
    if (openMenu && !openMenu.contains(event.target)) closeMenu();
    if (editor && !editor.row.contains(event.target)) finishEdit();
  }, {capture: true, signal: outsideClicks.signal});
  reloadButton.onclick = async () => {
    if (state.busy) return;
    busy(page, true);
    try {
      if (!await notesSave.flush() && !confirm('重新读取会丢弃尚未保存的书签修改。继续？')) return;
      const latest = await api(url('/recent-notes'));
      saved = latest; sections = {...latest.sections}; editor = null; undo = null; adopted = null; markers = {};
      requireReload(false);
      notesSave.reset();
      adoptJob(latest.tidy_job); clearError();
      renderNotes();
    } catch (error) { showError(error); }
    finally { busy(page, false); renderTidy(); }
  };
  function renderBookmarks() {
    $('#pending-tab').textContent = bookmarks.length ? `待整理 · ${bookmarks.length}` : '待整理';
    $('#pending-help').hidden = !bookmarks.length;
    $('#pending-help').textContent = `共 ${bookmarks.length} 条 · 今晚整理后并入已整理，之前可以撤回`;
    // Grouped like the organized tab: one numbered card per section that has bookmarks.
    $('#pending-bookmarks').innerHTML = shownSections().map(([key, label], number) => {
      const items = bookmarks.filter(item => item.section === key);
      if (!items.length) return '';
      return `<section class="card pending-section" data-section="${key}" aria-labelledby="pending-${key}">
        <header class="memo-section-head"><span class="memo-no" aria-hidden="true">${String(number + 1).padStart(2, '0')}</span><h3 id="pending-${key}">${label}</h3><span class="meta">${items.length} 条</span></header>
        ${items.map(item => `<article class="pending-bookmark" data-id="${esc(item.full_id)}">
          <p>${esc(item.content)}</p><button type="button" class="quiet withdraw-bookmark">撤回</button>
        </article>`).join('')}
      </section>`;
    }).join('') || '<p class="muted">今天还没有新书签</p>';
    $$('.withdraw-bookmark').forEach(button => button.onclick = async () => {
      if (state.busy || !confirm('撤回这条书签？它将从待整理列表移除。')) return;
      const identifier = button.closest('[data-id]').dataset.id;
      const errorLine = $('#pending-error');
      errorLine.textContent = '';
      busy(page, true);
      try {
        await api(url(`/bookmarks/${encodeURIComponent(identifier)}`), {method: 'DELETE', body: {}});
        bookmarks = bookmarks.filter(item => item.full_id !== identifier); message('书签已撤回');
      } catch (error) {
        showError(error, errorLine);
        if (error.status === 409) {
          try { bookmarks = (await api(url('/bookmarks'))).items; } catch (reloadError) { showError(reloadError, errorLine); }
        }
      } finally { busy(page, false); renderBookmarks(); }
    });
  }
  const isStale = () => stale || (job.status === 'done' && job.base_version !== saved.version);
  function renderTidy() {
    const done = job.status === 'done', running = job.status === 'running';
    const preview = done ? tidyPreview(job, selected) : null;
    const controlsDisabled = isStale() || !!adopted || !done;
    const titles = {idle: '让秘书帮忙合并和精简', running: '秘书正在整理…', done: '整理建议', failed: '本次整理未完成', cancelled: '本次整理已取消', applied: '本次整理已采纳并保存'};
    panel.innerHTML = `<div class="panelhead"><h2 id="tidy-title">秘书整理</h2><button type="button" class="quiet" id="close-tidy">收起</button></div><p class="muted">${titles[job.status] || titles.idle}${running ? ' 可以离开本页，稍后再来看。' : ''}</p>${job.error ? `<p class="error">${esc(job.error)}</p>` : ''}${!secretary.configured ? '<p class="muted">请先在服务器配置秘书整理的模型岗位。</p>' : ''}<div class="memo-toolbar"><button type="button" id="start-tidy" ${running || !secretary.configured || !!adopted ? 'disabled' : ''}>${job.id ? '重新整理' : '开始整理'}</button>${running ? '<button type="button" class="quiet" id="cancel-tidy">取消整理</button>' : ''}</div>${isStale() && !stale ? '<p class="error">记忆书签在整理之后有了变化，请重新整理。</p>' : ''}${done ? `<p class="tidy-total">${total(job.base_sections)} → <strong>${total(preview.sections)}</strong> 字 <span class="meta">（${delta(total(preview.sections) - total(job.base_sections))}）</span></p><div class="tidy-tabs" role="tablist" aria-label="整理分区">${shownSections().map(([key, label]) => `<button type="button" role="tab" data-tidy-section="${key}" aria-selected="${activeSection === key}" aria-controls="tidy-suggestions" id="tidy-tab-${key}">${label}<small>${count(job.base_sections[key])} → ${count(preview.sections[key])} 字 · ${job.suggestions.filter(s => s.section === key).length} 处</small></button>`).join('')}</div><div id="tidy-suggestions" role="tabpanel" aria-labelledby="tidy-tab-${activeSection}"></div><div class="tidy-selection"><span>已选 ${selected.size} / ${job.suggestions.length} 处</span><button type="button" class="quiet" id="tidy-all" ${controlsDisabled ? 'disabled' : ''}>全选</button><button type="button" class="quiet" id="tidy-none" ${controlsDisabled ? 'disabled' : ''}>全不选</button><button type="button" class="primary" id="adopt-tidy" ${controlsDisabled || !selected.size ? 'disabled' : ''}>采纳所选建议</button></div>` : ''}${job.note ? `<p class="muted tidy-note">${esc(job.note)}</p>` : ''}${job.dropped?.length ? `<p class="muted">有 ${job.dropped.length} 项不符合整理规则，已过滤。</p>` : ''}`;
    $('#close-tidy').onclick = () => { setTidyOpen(false); tidyButton.focus(); };
    $('#start-tidy').onclick = () => secretaryAction('start');
    if (running) $('#cancel-tidy').onclick = () => secretaryAction('cancel');
    if (!done) return;
    $$('[data-tidy-section]').forEach(button => button.onclick = () => { activeSection = button.dataset.tidySection; renderTidy(); $(`#tidy-tab-${activeSection}`).focus(); });
    const questions = targets => (job.need_confirm || []).filter(q => targets.includes(q.target)).map(q => `<p class="tidy-question"><strong>${esc(q.target)} · 秘书拿不准</strong> ${esc(q.question)}</p>`).join('');
    const suggestions = job.suggestions.filter(s => s.section === activeSection);
    const used = new Set(job.suggestions.flatMap(s => s.targets));
    const unchanged = Object.entries(job.entries).filter(([id, entry]) => entry.section === activeSection && !used.has(id));
    $('#tidy-suggestions').innerHTML = suggestions.map(change => `<article class="tidy-change"><label class="check"><input type="checkbox" data-change="${esc(change.id)}" ${selected.has(change.id) ? 'checked' : ''} ${controlsDisabled ? 'disabled' : ''}><span>${esc(change.type)} · ${esc(change.targets.join('、'))} <small>（${delta(count(change.new_text) - change.targets.reduce((n, id) => n + count(job.entries[id].text), 0))} 字）</small></span></label><div class="tidy-compare"><div><span class="meta">原文</span>${change.targets.map(id => `<p>${esc(job.entries[id].text)}</p>`).join('')}</div><div><span class="meta">${change.type === '删除' ? '建议删除' : '改为'}</span><p>${change.new_text ? esc(change.new_text) : '删除这条记录'}</p></div></div>${change.reason ? `<p class="muted">${esc(change.reason)}</p>` : ''}${questions(change.targets)}</article>`).join('') || '<p class="muted memo-empty">这个分区没有改动建议。</p>';
    $('#tidy-suggestions').insertAdjacentHTML('beforeend', `<details class="tidy-unchanged"><summary>其余 ${unchanged.length} 条未改动</summary>${unchanged.map(([id, entry]) => `<article><span class="meta">${esc(id)}</span><p>${esc(entry.text)}</p>${questions([id])}</article>`).join('')}</details>`);
    $$('[data-change]').forEach(box => box.onchange = () => { if (box.checked) selected.add(box.dataset.change); else selected.delete(box.dataset.change); const id = box.dataset.change; renderTidy(); $(`[data-change="${id}"]`).focus(); });
    $('#tidy-all').onclick = () => { selected = new Set(job.suggestions.map(s => s.id)); renderTidy(); };
    $('#tidy-none').onclick = () => { selected.clear(); renderTidy(); };
    $('#adopt-tidy').onclick = async () => {
      if (state.busy || isStale() || adopted || !selected.size) return;
      clearError(); busy(page, true);
      try {
        if (!await notesSave.flush()) return;
        const latest = await api(url('/recent-notes'));
        if (latest.version !== job.base_version) {
          requireReload(true);
          throw new Error('记忆书签在整理之后有了变化，请重新读取并重新整理。');
        }
        if (latest.tidy_job.id !== job.id || latest.tidy_job.status !== 'done') {
          requireReload(true);
          throw new Error('整理结果已改变，请重新读取书签后再整理。');
        }
        const result = tidyPreview(job, selected);
        undo = {sections: {...saved.sections}, preview: {...result.sections}};
        sections = result.sections; editor = null; markers = result.rows; adopted = job.id;
        setTidyOpen(false);
        renderNotes();
        notesSave.flush(true);
      } catch (error) { showError(error); }
      finally {
        busy(page, false); renderTidy();
        if (stale) reloadButton.focus();
        else if (undo) $('#adoption-notice').scrollIntoView({behavior: 'smooth', block: 'center'});
      }
    };
  }
  async function secretaryAction(action) {
    if (state.busy) return;
    clearError();
    busy(page, true);
    try {
      if (action === 'start') {
        if (!await notesSave.flush()) return;
        const latest = await api(url('/recent-notes'));
        if (latest.version !== saved.version) {
          requireReload(true);
          throw new Error('记忆书签有了变化，请先重新读取书签，再开始整理。');
        }
      }
      const result = await api(url(`/recent-notes/tidy/${action}`), {method: 'POST', body: {}});
      adoptJob(result.job || result);
      if (action === 'start') requireReload(false);
    } catch (error) { showError(error); }
    finally {
      busy(page, false); renderTidy();
      if (stale) reloadButton.focus();
    }
  }
  tidyButton.onclick = () => {
    setTidyOpen(true);
    renderTidy();
    panel.scrollIntoView({behavior: 'smooth', block: 'start'});
  };
  renderNotes(); renderBookmarks(); renderTidy();
  // Poll only while this page is visible. Navigation invalidates this closure.
  async function poll() {
    if (!live()) { outsideClicks.abort(); return; }
    if (document.visibilityState !== 'hidden' && job.status === 'running' && !state.busy) {
      try {
        const latest = await api(url('/recent-notes/tidy'));
        if (live()) {
          keepView();
          adoptJob(latest.job);
          renderTidy();
        }
      } catch (error) { showError(error); }
    }
    if (live()) setTimeout(poll, 3000);
    else outsideClicks.abort();
  }
  setTimeout(poll, 3000);
}
