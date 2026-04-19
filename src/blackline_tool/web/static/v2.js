(function reviewBoot() {
  const RUN_ID = window.__BL_RUN_ID__;
  const BATCH_HISTORY_KEY = "blackline_batch_history_v1";
  const FACET_LABELS = {
    content: "Content",
    formatting: "Formatting",
    style: "Style",
    alignment: "Alignment",
    layout: "Layout",
    indentation: "Indentation",
    spacing: "Spacing",
    pagination: "Pagination",
    numbering: "Numbering",
    capitalization: "Capitalization",
    punctuation: "Punctuation",
    whitespace: "Whitespace",
    header: "Header",
    footer: "Footer",
    table: "Table",
    textbox: "Text Box",
    footnote: "Footnote",
    endnote: "Endnote",
  };
  const FACET_ORDER = ["content", "formatting", "style", "alignment", "layout", "indentation", "spacing", "pagination", "numbering", "capitalization", "punctuation", "whitespace", "header", "footer", "table", "textbox", "footnote", "endnote"];
  const D = document;
  const s = {
    meta: null,
    filters: { kind: 'changed', facets: new Set(), decision: 'any', formatOnly: false, q: '' },
    selection: null,
    decisions: {},
    busy: false,
    undoStack: [],
    view: 'inline',
    events: {},
  };

  const BL = {
    get meta(){ return s.meta; },
    get selection(){ return s.selection; },
    set selection(v){ s.selection = v; },
    get filters(){ return s.filters; },
    get decisions(){ return s.decisions; },
    set decisions(v){ s.decisions = v; },
    on(k, f){ (s.events[k] || (s.events[k]=[])).push(f); },
    emit(k, p){ (s.events[k] || []).forEach(f => f(k, p)); }
  };

  async function loadMeta() {
    const r = await fetch('/api/runs/' + encodeURIComponent(RUN_ID));
    if (!r.ok) throw new Error(r.status);
    s.meta = await r.json();
    return s.meta;
  }

  function decisionFor(sec) {
    return s.decisions[String(sec.index)] || 'pending';
  }

  function sectionMatchesFilters(sec) {
    const f = s.filters;
    // Kind
    if (f.kind === 'changed' && (!sec.kind || sec.kind === 'equal')) return false;
    if (f.kind !== 'all' && f.kind !== 'changed' && sec.kind !== f.kind) return false;
    // Facets
    if (f.facets.size > 0) {
      const secFacets = new Set([...(sec.change_facets || []), ...(sec.format_change_facets || [])]);
      for (const req of f.facets) { if (!secFacets.has(req)) return false; }
    }
    // Decision
    if (f.decision !== 'any') {
      const d = decisionFor(sec);
      if (f.decision !== d) return false;
    }
    // Formatting-only
    if (f.formatOnly) {
      const hasContent = (sec.change_facets || []).some(x => x !== 'formatting' && !['style', 'alignment', 'layout', 'indentation', 'spacing', 'pagination'].includes(x));
      if (hasContent) return false;
    }
    // Search
    const q = (f.q || '').trim().toLowerCase();
    if (q) {
      const hay = [sec.label, sec.kind, sec.original_text, sec.revised_text].join(' ').toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  }

  function visibleSections() {
    if (!BL.meta) return [];
    return (BL.meta.sections || []).filter(sectionMatchesFilters);
  }

  function buildCounts() {
    if (!BL.meta) return null;
    const secs = BL.meta.sections || [];
    const c = {
      all: secs.length, changed: 0, insert: 0, delete: 0, replace: 0, move: 0,
      facets: {}, decision: { any: 0, pending: 0, accept: 0, reject: 0, decided: 0 },
      fmtOnly: 0, fmtOnlyTotal: 0,
    };
    for (const s of secs) {
      const changed = s.kind && s.kind !== 'equal';
      if (changed) c.changed += 1;
      if (s.kind && c[s.kind] !== undefined) c[s.kind] += 1;
      const allFacets = new Set([...(s.change_facets || []), ...(s.format_change_facets || [])]);
      for (const f of allFacets) { c.facets[f] = (c.facets[f] || 0) + 1; }
      const d = decisionFor(s);
      c.decision.any += 1;
      c.decision[d] = (c.decision[d] || 0) + 1;
      if (d !== 'pending') c.decision.decided += 1;
      // Formatting-only
      if (changed) {
        const f = s.change_facets || [];
        const fmtOnly = f.length && f.every(x => ['formatting', 'style', 'alignment', 'layout', 'indentation', 'spacing', 'pagination'].includes(x));
        if (fmtOnly) c.fmtOnly += 1;
        c.fmtOnlyTotal += 1;
      }
    }
    return c;
  }

  function setText(id, txt) { const el = document.getElementById(id); if (el) el.textContent = txt; }

  function makeChip(label, opts) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chip' + (opts.dot ? ' swatch' : '');
    if (opts.dot) btn.style.setProperty('--dot', opts.dot);
    btn.setAttribute('aria-pressed', opts.active ? 'true' : 'false');
    btn.dataset.value = opts.value;
    btn.innerHTML = '<span>' + escapeHtml(label) + '</span>' + (opts.count != null ? '<span class="n">' + opts.count + '</span>' : '');
    if (opts.onToggle) btn.addEventListener('click', () => opts.onToggle(btn));
    return btn;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function renderTypeChips(counts) {
    const row = document.getElementById('filter-row');
    if (!row) return;
    row.innerHTML = '';
    const items = [
      { label: 'Changes', value: 'changed', dot: 'var(--mod)', count: counts.changed },
      { label: 'Moves', value: 'move', dot: 'var(--ink-3)', count: counts.move },
      { label: 'Replaced', value: 'replace', dot: 'var(--accent)', count: counts.replace },
      { label: 'Inserts', value: 'insert', dot: 'var(--ins)', count: counts.insert },
      { label: 'Deletes', value: 'delete', dot: 'var(--del)', count: counts.delete },
      { label: 'All', value: 'all', dot: null, count: counts.all },
    ];
    for (const it of items) {
      row.appendChild(makeChip(it.label, {
        value: it.value, dot: it.dot, count: it.count,
        active: BL.filters.kind === it.value,
        onToggle: () => { BL.filters.kind = it.value; BL.emit('filters', 'kind'); renderRail(); },
      }));
    }
  }

  function renderFacetChips(counts) {
    const row = document.getElementById('facet-row');
    if (!row) return;
    row.innerHTML = '';
    row.appendChild(makeChip('Any facet', {
      value: '__any__', count: counts.changed,
      active: BL.filters.facets.size === 0,
      onToggle: () => { BL.filters.facets.clear(); BL.emit('filters', 'facets'); renderRail(); },
    }));
    for (const key of FACET_ORDER) {
      const n = counts.facets[key] || 0;
      if (!n) continue;
      row.appendChild(makeChip(FACET_LABELS[key] || key, {
        value: key, count: n,
        active: BL.filters.facets.has(key),
        onToggle: () => {
          if (BL.filters.facets.has(key)) BL.filters.facets.delete(key);
          else BL.filters.facets.add(key);
          BL.emit('filters', 'facets');
          renderRail();
        },
      }));
    }
  }

  function renderDecisionChips(counts) {
    const row = document.getElementById('decision-row');
    if (!row) return;
    row.innerHTML = '';
    const items = [
      { label: 'Any', value: 'any', dot: null, count: counts.decision.any },
      { label: 'Pending', value: 'pending', dot: null, count: counts.decision.pending },
      { label: 'Accepted', value: 'accept', dot: 'var(--ins)', count: counts.decision.accept },
      { label: 'Rejected', value: 'reject', dot: 'var(--del)', count: counts.decision.reject },
    ];
    for (const it of items) {
      row.appendChild(makeChip(it.label, {
        value: it.value, dot: it.dot, count: it.count,
        active: BL.filters.decision === it.value,
        onToggle: () => { BL.filters.decision = it.value; BL.emit('filters', 'decision'); renderRail(); },
      }));
    }
  }

  function renderSectionList() {
    const list = document.getElementById('detail-list');
    if (!list) return;
    const vis = visibleSections();
    if (!vis.length) {
      list.innerHTML = '<div class="group-hint">No sections in current scope.</div>';
      return;
    }
    list.innerHTML = '';
    for (const sec of vis) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'section-item' + (BL.selection === sec.index ? ' active' : '');
      btn.dataset.index = String(sec.index);
      const bar = barFor(sec);
      btn.innerHTML =
        '<span class="idx">' + String(sec.index).padStart(2, '0') + '</span>' +
        '<span>' + escapeHtml(sec.label || 'Section ' + sec.index) + '</span>' +
        '<span class="bar">' + bar + '</span>';
      btn.addEventListener('click', () => {
        BL.selection = sec.index;
        BL.emit('select', sec.index);
        renderSectionList();
      });
      list.appendChild(btn);
    }
  }

  function barFor(sec) {
    const kinds = [];
    if (sec.kind === 'insert') kinds.push('i');
    else if (sec.kind === 'delete') kinds.push('d');
    else if (sec.kind === 'replace' || sec.kind === 'move') kinds.push('m');
    const facetCount = (sec.change_facets || []).length + (sec.format_change_facets || []).length;
    while (kinds.length < 4) { kinds.push(facetCount >= kinds.length ? 'm' : ''); }
    return kinds.slice(0, 4).map(k => '<span' + (k ? ' class="' + k + '"' : '') + '></span>').join('');
  }

  function refreshMetricsAndStatus() {
    if (!BL.meta) return;
    const counts = buildCounts();
    if (!counts) return;
    setText('m-t', String(counts.all));
    setText('m-vis', visibleSections().length + '/' + counts.all);
    setText('m-pend', String(counts.decision.pending));
    const sFiles = document.getElementById('s-files');
    if (sFiles) sFiles.textContent = (BL.meta.original_name || '—') + ' ↔ ' + (BL.meta.revised_name || '—');
    setText('s-changes', counts.changed + ' changes');
    setText('s-decided', String(counts.decision.decided));
    setText('s-pending', String(counts.decision.pending));
    const pct = counts.changed ? Math.round((counts.decision.decided / counts.changed) * 100) : 0;
    setText('s-progress-pct', pct + '%');
    const fill = document.getElementById('s-progress');
    if (fill) fill.style.width = pct + '%';
  }

  function renderRail() {
    const counts = buildCounts();
    if (!counts) return;
    renderTypeChips(counts);
    renderFacetChips(counts);
    renderDecisionChips(counts);
    renderSectionList();
    refreshMetricsAndStatus();

    const visCount = visibleSections().length;
    setText('scope-count', visCount + ' / ' + counts.changed);
    setText('type-count', counts.changed + ' changed');
    const activeFacets = BL.filters.facets.size || (counts.changed ? Object.keys(counts.facets).length : 0);
    setText('facet-count', activeFacets + ' filter' + (activeFacets === 1 ? '' : 's'));
    setText('decisions-count', counts.decision.decided + ' / ' + counts.changed);
    setText('format-only-count', counts.fmtOnly + '/' + counts.fmtOnlyTotal);

    const fmtBtn = document.getElementById('format-only-toggle');
    if (fmtBtn) fmtBtn.setAttribute('aria-pressed', BL.filters.formatOnly ? 'true' : 'false');

    const pct = counts.changed ? (visCount / counts.changed) * 100 : 0;
    const pf = document.getElementById('scope-progress-fill');
    if (pf) pf.style.width = Math.max(0, Math.min(100, pct)) + '%';

    const note = document.getElementById('next-undecided-note');
    if (note) {
      if (counts.decision.pending) note.textContent = counts.decision.pending + ' pending · ' + counts.decision.decided + ' decided';
      else if (counts.changed) note.textContent = 'All changes decided.';
      else note.textContent = 'No changes in this run.';
    }
  }

  function wireExportMenu() {
    const wrap = document.getElementById('exportWrap');
    const btn = document.getElementById('exportBtn');
    if (!btn || !wrap) return;
    btn.addEventListener('click', (e) => { e.stopPropagation(); wrap.classList.toggle('open'); btn.setAttribute('aria-expanded', wrap.classList.contains('open')); });
    document.addEventListener('click', () => { wrap.classList.remove('open'); btn.setAttribute('aria-expanded', 'false'); });
    wrap.querySelectorAll('[data-export]').forEach(b => {
      b.addEventListener('click', () => {
        const type = b.dataset.export;
        if (type === 'final-docx') window.open('/api/runs/' + encodeURIComponent(RUN_ID) + '/export-clean', '_blank');
        else {
          const m = BL.meta; if (!m || !m.downloads) return;
          const url = m.downloads[type]; if (url) window.open(url, '_blank');
        }
      });
    });
  }

  function wireSegmented() {
    document.querySelectorAll('.segmented button').forEach(b => {
      b.addEventListener('click', () => {
        const v = b.dataset.view; s.view = v;
        document.querySelectorAll('.segmented button').forEach(x => x.setAttribute('aria-pressed', x === b ? 'true' : 'false'));
        renderDoc();
      });
    });
  }

  function wireNav() {
    const on = (id, fn) => { const b = document.getElementById(id); if (b) b.addEventListener('click', fn); };
    on('btn-prev-section', () => BL.emit('nav', 'prev'));
    on('btn-next-section', () => BL.emit('nav', 'next'));
    const jump = document.getElementById('jump-index');
    if (jump) jump.addEventListener('keydown', (e) => { if (e.key === 'Enter') { BL.emit('jump', parseInt(jump.value, 10)); jump.value = ''; jump.blur(); } });
  }

  function wireKeys() {
    document.addEventListener('keydown', (e) => {
      if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
      const k = e.key.toLowerCase();
      if (k === 'z') { s.zen = !s.zen; document.getElementById('app').classList.toggle('zen', s.zen); }
    });
  }

  function wireGroups() {
    document.querySelectorAll('.rail .group .group-h').forEach(h => {
      h.addEventListener('click', () => {
        const group = h.closest('.group'); if (!group) return;
        group.classList.toggle('collapsed');
        h.setAttribute('aria-expanded', group.classList.contains('collapsed') ? 'false' : 'true');
      });
    });
  }

  function wireSearch() {
    const input = document.getElementById('search'); if (!input) return;
    input.addEventListener('input', () => { BL.filters.q = input.value || ''; BL.emit('filters', 'search'); renderRail(); });
    document.addEventListener('keydown', (e) => {
      if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
      if (e.key === '/') { e.preventDefault(); input.focus(); }
    });
  }

  function wireRailButtons() {
    const on = (id, fn) => { const b = document.getElementById(id); if (b) b.addEventListener('click', fn); };
    on('format-only-toggle', () => { BL.filters.formatOnly = !BL.filters.formatOnly; BL.emit('filters', 'formatOnly'); renderRail(); });
    on('next-pending-btn', () => BL.emit('nav', 'next-pending'));
    on('next-format-btn', () => BL.emit('nav', 'next-format'));
    on('next-changed-btn', () => BL.emit('nav', 'next-changed'));
    on('next-undecided-btn', () => BL.emit('nav', 'next-undecided'));
    on('bulk-accept', () => applyBulk('accept'));
    on('bulk-reject', () => applyBulk('reject'));
    on('bulk-clear', () => applyBulk('pending'));
  }

  function applyBulk(decision) {
    const indexes = visibleSections().filter(s => s.kind !== 'equal').map(s => s.index);
    if (!indexes.length) return;
    fetch('/api/runs/' + encodeURIComponent(RUN_ID) + '/decisions/batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, section_indexes: indexes }),
    }).then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(payload => { if (payload && payload.decisions) s.decisions = payload.decisions; renderRail(); })
      .catch(() => { });
  }

  function renderTokens(tokens) {
    if (!Array.isArray(tokens) || !tokens.length) return '';
    return tokens.map(t => {
      const txt = escapeHtml(t.text || '').replace(/\\n/g, '<br>');
      if (t.kind === 'insert') return '<span class="ins">' + txt + '</span>';
      if (t.kind === 'delete') return '<span class="del">' + txt + '</span>';
      return txt;
    }).join('');
  }

  function renderDoc() {
    const doc = document.getElementById('doc'); if (!doc || !BL.meta) return;
    const secs = BL.meta.sections || [];
    const title = BL.meta.revised_name || BL.meta.original_name || 'Document';
    const subtitle = (BL.meta.original_name && BL.meta.revised_name) ? (BL.meta.original_name + ' → ' + BL.meta.revised_name) : (BL.meta.profile_summary || '');
    let html = '<div class="doc-header">';
    html += '<h1>' + escapeHtml(title.replace(/\.docx?$/i, '').replace(/_/g, ' ')) + '</h1>';
    html += '<div class="subtitle">' + escapeHtml(subtitle) + '</div>';
    html += '</div>';

    for (const sec of secs) {
      const changed = sec.kind && sec.kind !== 'equal';
      const tokens = sec.combined_tokens;
      let bodyText;
      if (tokens && tokens.length) bodyText = renderTokens(tokens);
      else if (sec.kind === 'delete') bodyText = escapeHtml(sec.original_text || '');
      else bodyText = escapeHtml(sec.revised_text || sec.original_text || '');
      
      const decision = decisionFor(sec);
      const rowClasses = [
        'doc-row',
        'p-interactive',
        changed ? 'is-changed' : '',
        'kind-' + (sec.kind || 'equal'),
        'decision-' + decision,
        BL.selection === sec.index ? 'active' : ''
      ].filter(Boolean).join(' ');

      html += `
        <div class="${rowClasses}" id="sec-${sec.index}" data-idx="${sec.index}">
          <div class="p-gutter">
            <span class="pnum">¶${sec.index}</span>
            <div class="m-bar"></div>
          </div>
          <div class="p-content">
            <div class="p-text">${bodyText || '&nbsp;'}</div>
          </div>
        </div>`;
    }
    doc.innerHTML = html;
    doc.querySelectorAll('.p-interactive').forEach(el => {
      el.addEventListener('click', () => {
        const idx = parseInt(el.dataset.idx, 10); const sec = (BL.meta.sections || []).find(s => s.index === idx);
        if (sec && sec.kind && sec.kind !== 'equal') { 
          BL.selection = idx; 
          BL.emit('select', idx); 
          renderSectionList(); 
          openInspector(idx); 
        } else {
          closeInspector();
        }
      });
    });
  }

  function openInspector(idx) {
    const insp = document.getElementById('inspector'); if (!insp) return;
    const sec = (BL.meta.sections || []).find(s => s.index === idx); if (!sec) return;
    renderInspector(sec); insp.classList.add('open');
  }

  function closeInspector() { const insp = document.getElementById('inspector'); if (insp) insp.classList.remove('open'); }

  function renderInspector(sec) {
    const kind = sec.kind || 'equal'; const decision = decisionFor(sec);
    const pill = document.getElementById('insp-pill'); if (pill) { pill.className = 'status-pill kind-' + kind; pill.textContent = (sec.kind_label || kind) + ' · ' + decision; }
    setText('insp-title', sec.label || ('Paragraph ' + sec.index));
    setText('insp-subtitle', '· ' + (sec.container || 'Body') + ' · sec ' + sec.index);
    const tags = document.getElementById('insp-tags');
    if (tags) {
      tags.innerHTML = ''; const all = new Set([...(sec.change_facets || []), ...(sec.format_change_facets || [])]);
      if (!all.size) tags.innerHTML = '<span class="tag">No facets</span>';
      else for (const t of all) { const span = document.createElement('span'); span.className = 'tag' + ((sec.format_change_facets || []).includes(t) ? ' active' : ''); span.textContent = FACET_LABELS[t] || t; tags.appendChild(span); }
    }
    const deltas = document.getElementById('insp-deltas'); const fmt = sec.format_change_facets || [];
    if (deltas) deltas.innerHTML = fmt.length ? fmt.map(f => '<code>' + escapeHtml(f) + '</code>: changed').join('<br>') : '<span class="muted">No formatting changes.</span>';
    const meta = document.getElementById('insp-meta');
    if (meta) {
      const rows = [['location', sec.location_kind || '—'], ['container', sec.container || '—'], ['kind', sec.kind || '—'], ['original', sec.original_label || '—'], ['revised', sec.revised_label || '—']];
      meta.innerHTML = rows.map(([k, v]) => '<div class="meta-row"><span class="k">' + escapeHtml(k) + '</span><span class="v">' + escapeHtml(String(v)) + '</span></div>').join('');
    }
    setText('insp-original', sec.original_text || '—'); setText('insp-revised', sec.revised_text || '—');
    document.querySelectorAll('.insp-foot [data-action]').forEach(b => b.setAttribute('aria-pressed', b.getAttribute('data-action') === decision ? 'true' : 'false'));
  }

  function wireInspector() {
    const on = (id, fn) => { const b = document.getElementById(id); if (b) b.addEventListener('click', fn); };
    on('inspClose', () => closeInspector());
    document.querySelectorAll('.insp-foot [data-action]').forEach(b => {
      b.addEventListener('click', () => { if (BL.selection != null) sendDecision(BL.selection, b.getAttribute('data-action')); });
    });
  }

  function sendDecision(index, decision) {
    fetch('/api/runs/' + encodeURIComponent(RUN_ID) + '/decisions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ section_index: index, decision }),
    }).then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(payload => { if (payload && payload.decisions) s.decisions = payload.decisions; renderRail(); renderInspector((BL.meta.sections || []).find(s => s.index === index)); })
      .catch(() => { });
  }

  BL.on('nav', (k, p) => {
    const vis = visibleSections().filter(s => s.kind !== 'equal'); if (!vis.length) return;
    const cur = BL.selection == null ? -1 : vis.findIndex(s => s.index === BL.selection);
    let next = null;
    if (p === 'prev') next = vis[Math.max(0, cur - 1)];
    else if (p === 'next') next = vis[Math.min(vis.length - 1, cur + 1)];
    else if (p === 'next-pending' || p === 'next-undecided') next = vis.find((s, i) => i > cur && decisionFor(s) === 'pending') || vis.find(s => decisionFor(s) === 'pending');
    if (next) { BL.selection = next.index; renderRail(); renderDoc(); openInspector(next.index); document.getElementById('sec-' + next.index)?.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
  });

  wireExportMenu(); wireSegmented(); wireNav(); wireKeys(); wireGroups(); wireSearch(); wireRailButtons(); wireInspector();
  loadMeta().then(m => { s.decisions = m.decisions || {}; renderDoc(); renderRail(); });
})();
