/*
 * Mosaic web UI: a dependency-free single-page app.
 *
 * Security model: every string that comes from the server (upstream databases,
 * Claude's answers, error messages) is untrusted. The UI never parses server
 * strings as HTML: elements are built with createElement and text is set with
 * textContent / text nodes. The markdown renderer below also builds DOM nodes
 * directly (so any HTML in the source is shown as literal, escaped text), and
 * links are only created for http(s) URLs, with rel="noopener noreferrer".
 */
'use strict';

(function () {
  // ===================================================================
  // DOM helpers
  // ===================================================================
  const $ = (sel, root) => (root || document).querySelector(sel);

  /** Allow http(s) absolute URLs and same-origin absolute paths; reject everything else. */
  function safeUrl(value, allowRelative) {
    if (typeof value !== 'string') return null;
    const v = value.trim();
    if (!v) return null;
    if (allowRelative && v.startsWith('/') && !v.startsWith('//') && !v.startsWith('/\\')) return v;
    try {
      const u = new URL(v);
      if (u.protocol === 'http:' || u.protocol === 'https:') return u.href;
    } catch (_) { /* not a URL */ }
    return null;
  }

  function append(el, kids) {
    for (const kid of kids.flat(Infinity)) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }

  /** h('a', {class, text, href, onclick, ...attrs}, ...children) */
  function h(tag, props) {
    const el = document.createElement(tag);
    if (props) {
      for (const key of Object.keys(props)) {
        const v = props[key];
        if (v === null || v === undefined || v === false) continue;
        if (key === 'class') el.className = v;
        else if (key === 'text') el.textContent = String(v);
        else if (key === 'dataset') Object.assign(el.dataset, v);
        else if (key.startsWith('on') && typeof v === 'function') el.addEventListener(key.slice(2), v);
        else if (key === 'href') {
          const url = safeUrl(String(v), true);
          if (url) el.setAttribute('href', url);
        } else if (key === 'value') el.value = String(v);
        else if (['checked', 'disabled', 'selected', 'hidden', 'open', 'required', 'multiple'].includes(key)) el[key] = Boolean(v);
        else el.setAttribute(key, v === true ? '' : String(v));
      }
    }
    return append(el, Array.prototype.slice.call(arguments, 2));
  }

  /** External link (http/https only); falls back to plain text. */
  function extLink(url, label, cls) {
    const safe = safeUrl(url, false);
    const text = label === undefined || label === null || label === '' ? String(url || '') : label;
    if (!safe) return h('span', { class: cls || null }, text);
    return h('a', { href: safe, target: '_blank', rel: 'noopener noreferrer', class: cls || null }, text);
  }

  const SVG_NS = 'http://www.w3.org/2000/svg';
  const ICONS = {
    trash: ['M3 6h18', 'M8 6V4h8v2', 'M6 6l1 14h10l1-14', 'M10 11v6', 'M14 11v6'],
    table: ['M3 5h18v14H3z', 'M3 10h18', 'M3 15h18', 'M9 5v14', 'M15 5v14'],
  };
  function icon(name, cls) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    if (cls) svg.setAttribute('class', cls);
    for (const d of ICONS[name] || []) {
      const p = document.createElementNS(SVG_NS, 'path');
      p.setAttribute('d', d);
      svg.appendChild(p);
    }
    return svg;
  }

  function clear(el) { el.replaceChildren(); return el; }

  function toast(message, isError) {
    const t = h('div', { class: 'toast' + (isError ? ' err' : ''), text: message });
    $('#toasts').appendChild(t);
    setTimeout(() => t.remove(), isError ? 7000 : 3500);
  }

  // ===================================================================
  // Formatting
  // ===================================================================
  const nf = new Intl.NumberFormat();
  function fmtNum(n) { return typeof n === 'number' && isFinite(n) ? nf.format(n) : '-'; }
  function fmtMs(ms) {
    if (typeof ms !== 'number' || !isFinite(ms)) return '-';
    return ms >= 1000 ? (ms / 1000).toFixed(ms >= 10000 ? 0 : 1) + ' s' : Math.round(ms) + ' ms';
  }
  function toDate(ts) {
    if (ts === null || ts === undefined || ts === '') return null;
    if (typeof ts === 'number') return new Date(ts < 1e12 ? ts * 1000 : ts);
    const n = Number(ts);
    if (!isNaN(n) && String(ts).trim() !== '') return new Date(n < 1e12 ? n * 1000 : n);
    const d = new Date(String(ts));
    return isNaN(d.getTime()) ? null : d;
  }
  function fmtTime(ts) {
    const d = toDate(ts);
    return d ? d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : '-';
  }
  function fmtShort(ts) {
    const d = toDate(ts);
    if (!d) return '-';
    return h('span', { class: 'nowrap', title: d.toLocaleString(), text: d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' }) });
  }
  function relTime(ts) {
    const d = toDate(ts);
    if (!d) return '';
    const s = (Date.now() - d.getTime()) / 1000;
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + ' min ago';
    if (s < 86400) return Math.floor(s / 3600) + ' h ago';
    if (s < 86400 * 7) return Math.floor(s / 86400) + ' d ago';
    return d.toLocaleDateString();
  }
  function clip(text, n) {
    const s = String(text === null || text === undefined ? '' : text).replace(/\s+/g, ' ').trim();
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }
  function json(value) {
    try { return JSON.stringify(value, null, 2); } catch (_) { return String(value); }
  }

  // ===================================================================
  // API client
  // ===================================================================
  class ApiError extends Error {
    constructor(status, message, data) { super(message); this.status = status; this.data = data || null; }
  }

  async function api(path, opts) {
    opts = opts || {};
    const init = { method: opts.method || 'GET', headers: { Accept: 'application/json' }, signal: opts.signal };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch (err) {
      if (err && err.name === 'AbortError') throw err;
      throw new ApiError(0, 'Cannot reach the Mosaic server (' + (err && err.message ? err.message : 'network error') + ')');
    }
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (_) { data = null; }
    if (!res.ok) {
      const msg = data && typeof data.error === 'string' ? data.error : 'HTTP ' + res.status;
      throw new ApiError(res.status, msg, data);
    }
    return data;
  }

  function errorBox(err) {
    const status = err && err.status ? ' (HTTP ' + err.status + ')' : '';
    return h('div', { class: 'error-box', role: 'alert' }, (err && err.message ? err.message : String(err)) + status);
  }

  // Shared, lazily loaded data
  const cache = { sources: null, sourcesPromise: null, health: null };
  const KNOWN_CATS = ['literature', 'clinical', 'drugs', 'chemistry', 'proteins', 'genomics', 'biology', 'datasets'];
  const CAT_SHORT = {
    literature: 'Literature', clinical: 'Clinical & safety', drugs: 'Drugs', chemistry: 'Chemistry',
    proteins: 'Proteins & structures', genomics: 'Genomics', biology: 'Pathways & biology', datasets: 'Data repositories',
  };
  function shortCat(key, label) { return CAT_SHORT[key] || String(label || key).split(',')[0].trim(); }

  function loadSources(force) {
    if (force || !cache.sourcesPromise) {
      cache.sourcesPromise = api('/api/sources').then((data) => { cache.sources = data; return data; })
        .catch((err) => { cache.sourcesPromise = null; throw err; });
    }
    return cache.sourcesPromise;
  }
  function categoryOf(sourceName) {
    const list = cache.sources ? cache.sources.sources : [];
    const s = list.find((x) => x.name === sourceName);
    return s ? s.category : null;
  }
  function catClass(cat) { return KNOWN_CATS.includes(cat) ? 'cat-' + cat : ''; }
  function sourceBadge(name, category) {
    const cat = category || categoryOf(name);
    return h('span', { class: 'badge src-badge ' + catClass(cat), title: cat ? 'category: ' + cat : null }, name || 'mosaic');
  }

  // ===================================================================
  // Safe markdown renderer (DOM-building; never uses innerHTML)
  // ===================================================================
  const LIST_RE = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
  const FENCE_RE = /^\s{0,3}(`{3,}|~{3,})\s*([\w+#.-]*)/;
  const HEADING_RE = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
  const HR_RE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
  const QUOTE_RE = /^\s{0,3}>/;
  const TABLE_SEP_RE = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

  function isBlank(line) { return /^\s*$/.test(line); }
  function indentOf(line) { const m = line.match(/^\s*/); return m ? m[0].replace(/\t/g, '    ').length : 0; }
  function isTableStart(lines, i) {
    return lines[i].includes('|') && i + 1 < lines.length && lines[i + 1].includes('|') &&
      lines[i + 1].includes('-') && TABLE_SEP_RE.test(lines[i + 1]);
  }
  function startsBlock(lines, i) {
    const line = lines[i];
    return FENCE_RE.test(line) || HEADING_RE.test(line) || HR_RE.test(line) || QUOTE_RE.test(line) ||
      LIST_RE.test(line) || isTableStart(lines, i);
  }

  function splitRow(line) {
    let s = line.trim();
    if (s.startsWith('|')) s = s.slice(1);
    if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1);
    const cells = [];
    let cur = '';
    let inCode = false;
    for (let i = 0; i < s.length; i++) {
      const c = s[i];
      if (c === '\\' && s[i + 1] === '|') { cur += '|'; i++; continue; }
      if (c === '`') inCode = !inCode;
      if (c === '|' && !inCode) { cells.push(cur.trim()); cur = ''; continue; }
      cur += c;
    }
    cells.push(cur.trim());
    return cells;
  }

  function renderTable(lines, start) {
    const header = splitRow(lines[start]);
    const aligns = splitRow(lines[start + 1]).map((c) => {
      const left = c.startsWith(':');
      const right = c.endsWith(':');
      return left && right ? 'center' : right ? 'right' : left ? 'left' : '';
    });
    const thead = h('thead', null, h('tr', null, header.map((cell, j) =>
      h('th', { class: aligns[j] ? 'al-' + aligns[j] : null, scope: 'col' }, inline(cell)))));
    const tbody = h('tbody');
    let i = start + 2;
    while (i < lines.length && !isBlank(lines[i]) && lines[i].includes('|')) {
      const cells = splitRow(lines[i]);
      const tr = h('tr');
      for (let j = 0; j < header.length; j++) {
        tr.appendChild(h('td', { class: aligns[j] ? 'al-' + aligns[j] : null }, inline(cells[j] || '')));
      }
      tbody.appendChild(tr);
      i++;
    }
    return [h('div', { class: 'table-wrap' }, h('table', null, thead, tbody)), i];
  }

  function parseList(lines, start) {
    const first = lines[start].match(LIST_RE);
    const base = indentOf(first[1]);
    const ordered = /\d/.test(first[2]);
    const list = h(ordered ? 'ol' : 'ul');
    if (ordered) {
      const n = parseInt(first[2], 10);
      if (n !== 1) list.setAttribute('start', String(n));
    }
    let i = start;
    let item = null;
    let prevBlank = false;
    const items = [];
    while (i < lines.length) {
      const line = lines[i];
      const m = line.match(LIST_RE);
      if (m && indentOf(m[1]) < base + 2) {
        if (indentOf(m[1]) < base) break;
        if (/\d/.test(m[2]) !== ordered) break;
        item = { lines: [m[3]], offset: indentOf(m[1]) + m[2].length + 1 };
        items.push(item);
        prevBlank = false;
        i++;
        continue;
      }
      if (isBlank(line)) {
        let j = i + 1;
        while (j < lines.length && isBlank(lines[j])) j++;
        if (j >= lines.length) break;
        const nm = lines[j].match(LIST_RE);
        const continues = indentOf(lines[j]) >= base + 2 || (nm && indentOf(nm[1]) >= base && /\d/.test(nm[2]) === ordered);
        if (!continues) break;
        item.lines.push('');
        prevBlank = true;
        i++;
        continue;
      }
      if (indentOf(line) >= base + 2) {
        const strip = Math.min(indentOf(line), item.offset);
        item.lines.push(line.replace(/\t/g, '    ').slice(strip));
        prevBlank = false;
        i++;
        continue;
      }
      if (prevBlank || startsBlock(lines, i)) break;
      item.lines.push(line.trim()); // lazy continuation
      i++;
    }
    for (const it of items) {
      const li = h('li');
      const frag = renderBlocks(it.lines);
      const firstChild = frag.firstChild;
      if (firstChild && firstChild.nodeName === 'P') {
        // Tight list item: unwrap the first paragraph.
        while (firstChild.firstChild) li.appendChild(firstChild.firstChild);
        frag.removeChild(firstChild);
      }
      li.appendChild(frag);
      list.appendChild(li);
    }
    return [list, i];
  }

  function renderBlocks(lines) {
    const frag = document.createDocumentFragment();
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (isBlank(line)) { i++; continue; }
      let m = line.match(FENCE_RE);
      if (m) {
        const fence = m[1];
        const buf = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence)) buf.push(lines[i++]);
        i++;
        const code = h('code', { text: buf.join('\n') });
        if (m[2]) code.className = 'lang-' + m[2].replace(/[^\w-]/g, '');
        frag.appendChild(h('pre', null, code));
        continue;
      }
      m = line.match(HEADING_RE);
      if (m) {
        frag.appendChild(h('h' + m[1].length, null, inline(m[2])));
        i++;
        continue;
      }
      if (HR_RE.test(line)) {
        frag.appendChild(h('hr'));
        i++;
        continue;
      }
      if (isTableStart(lines, i)) {
        const [node, next] = renderTable(lines, i);
        frag.appendChild(node);
        i = next;
        continue;
      }
      if (QUOTE_RE.test(line)) {
        const buf = [];
        while (i < lines.length && QUOTE_RE.test(lines[i])) buf.push(lines[i++].replace(/^\s{0,3}>\s?/, ''));
        frag.appendChild(h('blockquote', null, renderBlocks(buf)));
        continue;
      }
      if (LIST_RE.test(line)) {
        const [node, next] = parseList(lines, i);
        frag.appendChild(node);
        i = next;
        continue;
      }
      const buf = [line];
      i++;
      while (i < lines.length && !isBlank(lines[i]) && !startsBlock(lines, i)) buf.push(lines[i++]);
      const p = h('p');
      buf.forEach((l, k) => {
        if (k > 0) p.appendChild(h('br'));
        append(p, inline(l.trim()));
      });
      frag.appendChild(p);
    }
    return frag;
  }

  function renderMarkdown(src) {
    const lines = String(src || '').replace(/\r\n?/g, '\n').split('\n');
    return renderBlocks(lines);
  }

  const INLINE_SPECIAL = /[`\\[*_~<]|https?:\/\//g;
  const RE_CODE = /(`+)([\s\S]*?[^`])\1(?!`)/y;
  const RE_LINK = /\[([^\]\n]*)\]\(\s*<?([^()\s<>]+(?:\([^()\s]*\))?[^()\s<>]*)>?(?:\s+"[^"\n]*")?\s*\)/y;
  const RE_ANGLE_URL = /<(https?:\/\/[^\s<>]+)>/y;
  const RE_URL = /https?:\/\/[^\s<>"'`]+/y;
  const RE_BR = /<br\s*\/?>/iy;
  const RE_BOLD_STAR = /\*\*(?!\s)([\s\S]+?)(?<!\s)\*\*/y;
  const RE_BOLD_UND = /__(?!\s)([\s\S]+?)(?<!\s)__(?![A-Za-z0-9])/y;
  const RE_EM_STAR = /\*(?![\s*])([^*\n]+?)(?<!\s)\*/y;
  const RE_EM_UND = /_(?![\s_])([^_\n]+?)(?<!\s)_(?![A-Za-z0-9])/y;
  const RE_STRIKE = /~~(?!\s)([\s\S]+?)(?<!\s)~~/y;

  function stickyMatch(re, s, i) { re.lastIndex = i; return re.exec(s); }

  /** Inline markdown -> array of DOM nodes. */
  function inline(text) {
    const s = String(text === null || text === undefined ? '' : text);
    const out = [];
    let buf = '';
    let i = 0;
    const flush = () => { if (buf) { out.push(document.createTextNode(buf)); buf = ''; } };
    while (i < s.length) {
      INLINE_SPECIAL.lastIndex = i;
      const next = INLINE_SPECIAL.exec(s);
      if (!next) { buf += s.slice(i); break; }
      if (next.index > i) { buf += s.slice(i, next.index); i = next.index; }
      const c = s[i];
      let m;
      if (c === '\\' && i + 1 < s.length && /[\\`*_{}[\]()#+\-.!|~<>]/.test(s[i + 1])) {
        buf += s[i + 1]; i += 2; continue;
      }
      if (c === '`' && (m = stickyMatch(RE_CODE, s, i))) {
        flush(); out.push(h('code', { text: m[2].replace(/^ (.*) $/, '$1') })); i += m[0].length; continue;
      }
      if (c === '[' && (m = stickyMatch(RE_LINK, s, i))) {
        flush();
        const url = safeUrl(m[2], false);
        if (url) out.push(h('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, inline(m[1] || m[2])));
        else out.push(...inline(m[1]));
        i += m[0].length; continue;
      }
      if (c === '<') {
        if ((m = stickyMatch(RE_ANGLE_URL, s, i))) { flush(); out.push(extLink(m[1], m[1])); i += m[0].length; continue; }
        if ((m = stickyMatch(RE_BR, s, i))) { flush(); out.push(h('br')); i += m[0].length; continue; }
      }
      if (c === 'h' && (m = stickyMatch(RE_URL, s, i))) {
        let url = m[0];
        const trail = url.match(/[).,;:!?\]]+$/);
        if (trail) {
          // keep balanced closing parentheses that belong to the URL
          let cut = trail[0];
          if (cut.startsWith(')') && (url.split('(').length - 1) >= (url.split(')').length - 1)) cut = cut.replace(/^\)+/, '');
          url = url.slice(0, url.length - cut.length);
        }
        flush(); out.push(extLink(url, url)); i += url.length; continue;
      }
      if (c === '*') {
        if ((m = stickyMatch(RE_BOLD_STAR, s, i))) { flush(); out.push(h('strong', null, inline(m[1]))); i += m[0].length; continue; }
        if ((m = stickyMatch(RE_EM_STAR, s, i))) { flush(); out.push(h('em', null, inline(m[1]))); i += m[0].length; continue; }
      }
      if (c === '_' && (i === 0 || !/[A-Za-z0-9]/.test(s[i - 1]))) {
        if ((m = stickyMatch(RE_BOLD_UND, s, i))) { flush(); out.push(h('strong', null, inline(m[1]))); i += m[0].length; continue; }
        if ((m = stickyMatch(RE_EM_UND, s, i))) { flush(); out.push(h('em', null, inline(m[1]))); i += m[0].length; continue; }
      }
      if (c === '~' && (m = stickyMatch(RE_STRIKE, s, i))) {
        flush(); out.push(h('del', null, inline(m[1]))); i += m[0].length; continue;
      }
      buf += c;
      i++;
    }
    flush();
    return out;
  }

  // ===================================================================
  // Header status
  // ===================================================================
  const appState = { health: null };

  async function refreshHealth() {
    const dot = $('#status-dot');
    const text = $('#status-text');
    try {
      const hdata = await api('/api/health');
      appState.health = hdata;
      const agent = hdata.agent_available ? 'Claude ready (' + hdata.model + ')' : 'Claude unavailable';
      text.textContent = fmtNum(hdata.sources) + ' sources · ' + fmtNum(hdata.configured) + ' configured · ' + agent;
      dot.className = 'dot ' + (hdata.agent_available ? 'ok' : 'warn');
      $('#status').title = hdata.agent_available ? 'Mosaic ' + hdata.version : (hdata.agent_error || 'agent unavailable');
      updateAgentBanner();
    } catch (err) {
      appState.health = null;
      text.textContent = 'Server unreachable';
      dot.className = 'dot err';
      $('#status').title = err.message;
    }
  }

  // ===================================================================
  // Router
  // ===================================================================
  const VIEWS = ['ask', 'sources', 'search', 'catalog', 'exports', 'audit'];
  const loaders = {};
  const loaded = new Set();

  function route() {
    const name = (location.hash || '#ask').slice(1).split('/')[0];
    const view = VIEWS.includes(name) ? name : 'ask';
    for (const v of VIEWS) $('#view-' + v).hidden = v !== view;
    document.querySelectorAll('.nav a').forEach((a) => {
      if (a.dataset.view === view) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
    $('#nav').classList.remove('open');
    $('#nav-toggle').setAttribute('aria-expanded', 'false');
    const first = !loaded.has(view);
    loaded.add(view);
    if (loaders[view]) loaders[view](first);
    const titles = { ask: 'Ask Mosaic', sources: 'Sources', search: 'Cross-database search', catalog: 'NIH repositories', exports: 'Exports', audit: 'Audit log' };
    document.title = titles[view] + ' · Mosaic';
  }

  // ===================================================================
  // Ask Mosaic (chat)
  // ===================================================================
  const EXAMPLES = [
    { tag: 'Drug landscape', text: 'For KRAS G12C inhibitors: approved drugs, active phase 3 trials, key PubMed papers and FAERS top reactions; save a table.' },
    { tag: 'Target dossier', text: 'Build a target dossier for EGFR: UniProt function, PDB structures with inhibitors bound, ChEMBL compounds with IC50 < 10 nM, and recruiting trials. Save the compounds as a table.' },
    { tag: 'Safety signal', text: 'Compare the most frequently reported adverse reactions for semaglutide and tirzepatide in FAERS, and check the boxed warnings on their FDA labels.' },
    { tag: 'Variant to evidence', text: 'What is known about BRCA2 c.5946delT? Combine ClinVar significance, gnomAD frequency, relevant trials and the 5 most cited papers.' },
    { tag: 'Data discovery', text: 'Which NIH repositories hold single-cell RNA-seq data for Alzheimer disease, and what datasets are available in GEO and generalist repositories?' },
    { tag: 'Literature + funding', text: 'Find the most cited 2023-2025 papers on GLP-1 receptor agonists in Alzheimer disease, link them to NIH grants that funded them, and save a table of paper, grant and PI.' },
  ];

  const chat = {
    sessionId: null,
    busy: false,
    controller: null,
    sessions: [],
  };

  function agentAvailable() { return !appState.health || appState.health.agent_available; }

  function updateAgentBanner() {
    const banner = $('#agent-banner');
    if (appState.health && !appState.health.agent_available) {
      banner.textContent = 'Ask Mosaic is unavailable: ' + (appState.health.agent_error || 'the agent is not loaded') +
        '. Sources, search, catalog, exports and audit views still work.';
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }
    updateComposer();
  }

  function updateComposer() {
    const send = $('#send');
    const stop = $('#stop');
    send.disabled = chat.busy || !agentAvailable();
    stop.hidden = !chat.busy;
    $('#composer-hint').textContent = chat.sessionId ? 'Continuing conversation ' + chat.sessionId.slice(0, 8) : 'New conversation';
  }

  function chatScroller() { return $('#chat-scroll'); }
  function nearBottom() { const el = chatScroller(); return el.scrollHeight - el.scrollTop - el.clientHeight < 120; }
  function scrollToBottom() { const el = chatScroller(); el.scrollTop = el.scrollHeight; }

  function renderExamples() {
    const box = clear($('#examples'));
    for (const ex of EXAMPLES) {
      box.appendChild(h('button', {
        type: 'button', class: 'example', role: 'listitem',
        onclick: () => { const p = $('#prompt'); p.value = ex.text; autoGrow(p); p.focus(); },
      }, h('span', { class: 'ex-tag', text: ex.tag }), h('span', { text: ex.text })));
    }
  }

  function showWelcome(show) { $('#chat-welcome').hidden = !show; }

  function userMessage(text) {
    return h('li', { class: 'msg msg-user' }, h('span', { class: 'msg-role', text: 'You' }), h('div', { class: 'bubble', text: text }));
  }

  /** Renders one assistant reply from streamed agent events. */
  class AssistantView {
    constructor(list) {
      this.timeline = h('div', { class: 'timeline' });
      this.typingLabel = h('span', { text: 'Thinking…' });
      this.typing = h('div', { class: 'typing' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), this.typingLabel);
      this.footer = h('div', { class: 'msg-footer', hidden: true });
      this.li = h('li', { class: 'msg msg-assistant' }, h('span', { class: 'msg-role', text: 'Mosaic' }), this.timeline, this.typing, this.footer);
      list.appendChild(this.li);
      this.seg = null;
      this.tools = new Map();
      this.text = '';
      this.usage = {};
      this.model = null;
      this.toolCount = 0;
      this.tables = 0;
      this.started = performance.now();
      this.pending = new Set();
      this.frame = 0;
    }

    segment(kind) {
      if (this.seg && this.seg.kind === kind) return this.seg;
      let seg;
      if (kind === 'text') {
        seg = { kind, text: '', el: h('div', { class: 'md' }) };
      } else {
        const body = h('div', { class: 'reasoning-body' });
        const label = h('span', { text: 'Reasoning' });
        const el = h('details', { class: 'reasoning' }, h('summary', null, label), body);
        seg = { kind, text: '', el, body, label };
      }
      this.timeline.appendChild(seg.el);
      this.seg = seg;
      return seg;
    }

    schedule(seg) {
      this.pending.add(seg);
      if (this.frame) return;
      this.frame = requestAnimationFrame(() => {
        this.frame = 0;
        const stick = nearBottom();
        for (const s of this.pending) {
          if (s.kind === 'text') s.el.replaceChildren(renderMarkdown(s.text));
          else s.body.textContent = s.text;
        }
        this.pending.clear();
        if (stick) scrollToBottom();
      });
    }

    flush() {
      if (this.frame) { cancelAnimationFrame(this.frame); this.frame = 0; }
      for (const s of this.pending) {
        if (s.kind === 'text') s.el.replaceChildren(renderMarkdown(s.text));
        else s.body.textContent = s.text;
      }
      this.pending.clear();
    }

    status(label) { this.typingLabel.textContent = label; }

    addNode(node) {
      const stick = nearBottom();
      this.seg = null;
      this.timeline.appendChild(node);
      if (stick) scrollToBottom();
    }

    handle(type, ev) {
      switch (type) {
        case 'session_started':
          this.model = ev.model || null;
          if (ev.session_id) setActiveSession(ev.session_id, ev.title);
          break;
        case 'thinking_delta': {
          const seg = this.segment('thinking');
          seg.text += ev.text || '';
          this.schedule(seg);
          this.status('Reasoning…');
          break;
        }
        case 'text_delta': {
          const seg = this.segment('text');
          seg.text += ev.text || '';
          this.text += ev.text || '';
          this.schedule(seg);
          this.status('Writing…');
          break;
        }
        case 'tool_call_started': this.toolStarted(ev); break;
        case 'tool_call_finished': this.toolFinished(ev); break;
        case 'server_tool': this.serverTool(ev); break;
        case 'table_saved': this.tableSaved(ev); break;
        case 'turn_completed': this.addUsage(ev.usage); break;
        case 'error':
          this.addNode(h('div', { class: 'msg-error', role: 'alert' }, (ev.retryable ? 'Temporary error: ' : 'Error: ') + (ev.message || 'unknown error')));
          break;
        case 'done': this.done(ev); break;
        default: break;
      }
    }

    toolStarted(ev) {
      this.toolCount++;
      const cat = ev.source ? categoryOf(ev.source) : null;
      const statusEl = h('span', { class: 'tool-status' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'running');
      const summary = h('div', { class: 'tool-summary', hidden: true });
      const body = h('div', { class: 'tool-body' });
      const input = ev.input && Object.keys(ev.input).length ? ev.input : null;
      if (input) body.appendChild(h('details', null, h('summary', { text: 'Input' }), h('pre', { class: 'json', text: json(input) })));
      const card = h('div', { class: 'tool-card ' + catClass(cat), role: 'group', 'aria-label': 'Tool call ' + (ev.name || '') },
        h('div', { class: 'tool-head' }, sourceBadge(ev.source || 'mosaic', cat), h('span', { class: 'tool-name', text: ev.name || 'tool' }), statusEl),
        summary, body);
      this.tools.set(ev.call_id, { card, statusEl, summary, body });
      this.addNode(card);
      this.status('Running ' + (ev.name || 'tool') + '…');
    }

    toolFinished(ev) {
      let entry = this.tools.get(ev.call_id);
      if (!entry) { this.toolStarted({ call_id: ev.call_id, name: ev.name, source: null, input: {} }); entry = this.tools.get(ev.call_id); }
      const { card, statusEl, summary, body } = entry;
      statusEl.replaceChildren(
        h('span', { class: 'badge ' + (ev.ok ? 'ok' : 'err'), text: ev.ok ? 'ok' : 'failed' }),
        ev.latency_ms !== null && ev.latency_ms !== undefined ? fmtMs(ev.latency_ms) : '',
      );
      if (!ev.ok) card.classList.add('failed');
      summary.hidden = false;
      summary.className = 'tool-summary' + (ev.ok ? '' : ' err');
      summary.textContent = ev.ok ? (ev.summary || 'done') : (ev.error || ev.summary || 'failed');
      if (ev.result_id) summary.appendChild(h('span', { class: 'muted small', text: '  · result ' + ev.result_id }));
      const preview = Array.isArray(ev.preview) ? ev.preview : [];
      if (preview.length) {
        const list = h('ul', { class: 'preview-list' });
        for (const rec of preview.slice(0, 8)) list.appendChild(previewItem(rec));
        body.appendChild(h('details', null, h('summary', { text: 'Preview (' + preview.length + ' record' + (preview.length === 1 ? '' : 's') + ')' }), list));
      }
      this.status('Thinking…');
    }

    serverTool(ev) {
      const label = (ev.phase === 'call' ? '→ ' : '← ') + (ev.name || 'server tool') + (ev.detail ? ': ' + clip(ev.detail, 140) : '');
      this.addNode(h('div', { class: 'server-tool', title: ev.detail || '' }, h('span', { class: 'badge outline', text: /^web/.test(ev.name || '') ? 'web' : 'server' }), label));
      if (ev.phase === 'call') this.status('Running ' + (ev.name || 'server tool') + '…');
    }

    tableSaved(ev) {
      this.tables++;
      this.addNode(tableCard(ev));
    }

    addUsage(usage) {
      if (!usage) return;
      for (const k of Object.keys(usage)) {
        if (typeof usage[k] === 'number') this.usage[k] = (this.usage[k] || 0) + usage[k];
      }
    }

    done(ev) {
      this.flush();
      if (!this.text && ev.final_text) {
        const seg = this.segment('text');
        seg.text = ev.final_text;
        seg.el.replaceChildren(renderMarkdown(seg.text));
      }
      this.finish(ev.usage && Object.keys(ev.usage).length ? ev.usage : this.usage, ev.stop_reason);
    }

    finish(usage, stopReason) {
      this.flush();
      this.typing.hidden = true;
      for (const entry of this.tools.values()) {
        if (entry.statusEl.querySelector('.spinner')) entry.statusEl.replaceChildren(h('span', { class: 'badge warn', text: 'no result' }));
      }
      const u = usage || {};
      const parts = [];
      if (this.model) parts.push(this.model);
      parts.push(this.toolCount + ' tool call' + (this.toolCount === 1 ? '' : 's'));
      if (u.input_tokens) parts.push(fmtNum(u.input_tokens) + ' input tokens');
      if (u.output_tokens) parts.push(fmtNum(u.output_tokens) + ' output tokens');
      if (u.cache_read_input_tokens) parts.push(fmtNum(u.cache_read_input_tokens) + ' cache read');
      if (u.cache_creation_input_tokens) parts.push(fmtNum(u.cache_creation_input_tokens) + ' cache write');
      parts.push(fmtMs(performance.now() - this.started));
      if (stopReason && stopReason !== 'end_turn') parts.push('stop: ' + stopReason);
      this.footer.replaceChildren(...parts.map((p) => h('span', { text: p })));
      this.footer.hidden = false;
    }

    abort(message) {
      this.addNode(h('div', { class: 'msg-error', role: 'alert', text: message }));
      this.finish(this.usage, null);
    }
  }

  function previewItem(rec) {
    const r = rec && typeof rec === 'object' ? rec : { title: String(rec) };
    const title = r.title || r.name || r.id || '(untitled)';
    const meta = [r.type, r.date, r.id && r.title ? r.id : null].filter(Boolean).join(' · ');
    const titleNode = r.url ? extLink(r.url, clip(title, 160)) : h('span', { text: clip(title, 160) });
    return h('li', null, titleNode, meta ? h('span', { class: 'pmeta', text: meta }) : null);
  }

  const EXPORT_ID_RE = /^[0-9a-f]{32}$/;
  const EXPORT_PATH_RE = /^\/api\/exports\/[0-9a-f]{32}\.(csv|json)$/;
  /** Same-origin download URL for an export (strictly validated), or null. */
  function exportUrl(ev, ext) {
    const given = ev ? ev[ext + '_url'] : null;
    if (typeof given === 'string' && EXPORT_PATH_RE.test(given) && given.endsWith('.' + ext)) return given;
    const id = ev ? ev.export_id : null;
    return typeof id === 'string' && EXPORT_ID_RE.test(id) ? '/api/exports/' + id + '.' + ext : null;
  }

  function tableCard(ev) {
    const cols = Array.isArray(ev.columns) ? ev.columns : [];
    const csv = exportUrl(ev, 'csv');
    const jsn = exportUrl(ev, 'json');
    return h('div', { class: 'table-card', role: 'group', 'aria-label': 'Saved table' },
      icon('table', 'tc-icon'),
      h('div', { class: 'tc-main' },
        h('div', { class: 'tc-name', text: 'Table saved: ' + (ev.name || 'extraction') }),
        h('div', { class: 'small', text: fmtNum(ev.n_rows) + ' rows · ' + cols.length + ' columns' }),
        cols.length ? h('div', { class: 'tc-cols', text: clip(cols.join(', '), 240) }) : null),
      h('div', { class: 'tc-actions' },
        csv ? h('a', { class: 'btn btn-sm', href: csv, download: '' }, 'CSV') : null,
        jsn ? h('a', { class: 'btn btn-sm', href: jsn, download: '' }, 'JSON') : null));
  }

  /** POST /api/chat and dispatch Server-Sent Events. */
  async function streamChat(body, onEvent, signal) {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(body),
      signal,
    });
    if (!res.ok) {
      let data = null;
      try { data = await res.json(); } catch (_) { data = null; }
      throw new ApiError(res.status, data && data.error ? data.error : 'HTTP ' + res.status, data);
    }
    if (!res.body) throw new ApiError(0, 'streaming is not supported by this browser');
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let sawDone = false;
    const dispatch = (frame) => {
      let event = 'message';
      const data = [];
      for (const line of frame.split('\n')) {
        if (!line || line.startsWith(':')) continue;
        const c = line.indexOf(':');
        const field = c < 0 ? line : line.slice(0, c);
        let val = c < 0 ? '' : line.slice(c + 1);
        if (val.startsWith(' ')) val = val.slice(1);
        if (field === 'event') event = val;
        else if (field === 'data') data.push(val);
      }
      if (!data.length) return;
      let payload;
      try { payload = JSON.parse(data.join('\n')); } catch (_) { return; }
      if (event === 'done') sawDone = true;
      onEvent(event, payload || {});
    };
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      buf = buf.replace(/\r\n?/g, '\n');
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        dispatch(frame);
      }
    }
    buf += decoder.decode();
    if (buf.trim()) dispatch(buf);
    return sawDone;
  }

  async function sendMessage(text) {
    const message = text.trim();
    if (!message || chat.busy) return;
    if (!agentAvailable()) { toast('Ask Mosaic is unavailable on this server', true); return; }
    showWelcome(false);
    const list = $('#messages');
    list.appendChild(userMessage(message));
    const view = new AssistantView(list);
    scrollToBottom();
    chat.busy = true;
    chat.controller = new AbortController();
    updateComposer();
    let finished = false;
    try {
      const sawDone = await streamChat({ message, session_id: chat.sessionId || undefined }, (type, ev) => {
        view.handle(type, ev);
        if (type === 'done') finished = true;
      }, chat.controller.signal);
      if (!sawDone && !finished) view.abort('The stream ended before the answer was complete.');
    } catch (err) {
      if (err && err.name === 'AbortError') view.abort('Stopped.');
      else view.abort(err && err.message ? err.message : String(err));
      if (err && err.status === 503) refreshHealth();
    } finally {
      chat.busy = false;
      chat.controller = null;
      updateComposer();
      loadSessions();
    }
  }

  function setActiveSession(id, title) {
    chat.sessionId = id;
    updateComposer();
    document.querySelectorAll('.session-item').forEach((li) => li.classList.toggle('active', li.dataset.id === id));
    if (title && !chat.sessions.some((s) => s.session_id === id)) {
      // Optimistically show the new session while the answer streams.
      chat.sessions.unshift({ session_id: id, title, updated_at: Date.now() / 1000, turns: 0 });
      renderSessions();
    }
  }

  async function loadSessions() {
    try {
      const data = await api('/api/sessions?limit=100');
      chat.sessions = Array.isArray(data.sessions) ? data.sessions : [];
    } catch (err) {
      chat.sessions = [];
      if (err.status !== 503) toast('Could not load conversations: ' + err.message, true);
    }
    renderSessions();
  }

  function renderSessions() {
    const ul = clear($('#session-list'));
    $('#sessions-empty').hidden = chat.sessions.length > 0;
    for (const s of chat.sessions) {
      const id = String(s.session_id || '');
      const title = s.title || 'Untitled conversation';
      const meta = [relTime(s.updated_at || s.created_at), s.turns ? s.turns + ' turn' + (s.turns === 1 ? '' : 's') : null].filter(Boolean).join(' · ');
      const li = h('li', { class: 'session-item' + (id === chat.sessionId ? ' active' : ''), dataset: { id } },
        h('button', { type: 'button', class: 'session-open', title, onclick: () => openSession(id) },
          h('span', { class: 's-title', text: title }), h('span', { class: 's-meta', text: meta })),
        h('button', { type: 'button', class: 'btn-icon', 'aria-label': 'Delete conversation: ' + title, title: 'Delete', onclick: () => deleteSession(id, title) }, icon('trash')));
      ul.appendChild(li);
    }
  }

  function newChat() {
    if (chat.busy && chat.controller) chat.controller.abort();
    chat.sessionId = null;
    clear($('#messages'));
    showWelcome(true);
    renderSessions();
    updateComposer();
    $('#prompt').focus();
  }

  async function openSession(id) {
    if (chat.busy) { toast('Wait for the current answer to finish (or press Stop).'); return; }
    let data;
    try {
      data = await api('/api/sessions/' + encodeURIComponent(id));
    } catch (err) {
      toast('Could not open conversation: ' + err.message, true);
      return;
    }
    chat.sessionId = id;
    const list = clear($('#messages'));
    showWelcome(false);
    for (const entry of Array.isArray(data.transcript) ? data.transcript : []) {
      if (!entry || typeof entry !== 'object') continue;
      if (entry.role === 'user') { list.appendChild(userMessage(String(entry.text || ''))); continue; }
      const view = new AssistantView(list);
      view.typing.hidden = true;
      for (const call of Array.isArray(entry.tool_calls) ? entry.tool_calls : []) {
        if (!call || typeof call !== 'object') continue;
        const callId = String(call.call_id || call.id || Math.random());
        view.toolStarted({ call_id: callId, name: call.name || call.tool, source: call.source || null, input: call.input || {} });
        view.toolFinished({
          call_id: callId, name: call.name, ok: call.ok !== false, summary: call.summary || '', error: call.error,
          latency_ms: call.latency_ms, preview: call.preview || [], result_id: call.result_id,
        });
      }
      for (const table of Array.isArray(entry.tables) ? entry.tables : []) view.tableSaved(table);
      if (entry.text) {
        const seg = view.segment('text');
        seg.text = String(entry.text);
        seg.el.replaceChildren(renderMarkdown(seg.text));
      }
    }
    renderSessions();
    updateComposer();
    scrollToBottom();
  }

  async function deleteSession(id, title) {
    if (!window.confirm('Delete the conversation "' + clip(title, 80) + '"? This cannot be undone.')) return;
    try {
      await api('/api/sessions/' + encodeURIComponent(id), { method: 'DELETE' });
      if (chat.sessionId === id) newChat();
      toast('Conversation deleted');
    } catch (err) {
      toast('Could not delete: ' + err.message, true);
    }
    loadSessions();
  }

  function autoGrow(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight + 2, 240) + 'px';
  }

  function initAsk() {
    renderExamples();
    const prompt = $('#prompt');
    $('#composer').addEventListener('submit', (e) => {
      e.preventDefault();
      const text = prompt.value;
      if (!text.trim()) return;
      prompt.value = '';
      autoGrow(prompt);
      sendMessage(text);
    });
    prompt.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        $('#composer').requestSubmit();
      }
    });
    prompt.addEventListener('input', () => autoGrow(prompt));
    $('#stop').addEventListener('click', () => { if (chat.controller) chat.controller.abort(); });
    $('#new-chat').addEventListener('click', newChat);
    updateComposer();
  }
  loaders.ask = (first) => { if (first) loadSessions(); $('#prompt').focus({ preventScroll: true }); };

  // ===================================================================
  // Sources
  // ===================================================================
  const srcView = { category: '', filter: '', health: new Map(), checking: new Set() };

  function renderSourceCats(data) {
    const box = clear($('#sources-cats'));
    const mk = (key, label, count) => h('button', {
      type: 'button', class: 'chip', 'aria-pressed': String(srcView.category === key),
      onclick: () => { srcView.category = key; renderSourceCats(data); renderSourceGroups(); },
    }, label, h('span', { class: 'count', text: String(count) }));
    box.appendChild(mk('', 'All', data.sources.length));
    for (const [key, label] of Object.entries(data.categories || {})) {
      const n = data.sources.filter((s) => s.category === key).length;
      if (n) box.appendChild(mk(key, shortCat(key, label), n));
    }
  }

  function healthLine(name) {
    if (srcView.checking.has(name)) return h('div', { class: 'health-line' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Checking…');
    const r = srcView.health.get(name);
    if (!r) return null;
    return h('div', { class: 'health-line ' + (r.ok ? 'ok' : 'err'), title: r.detail || '' },
      h('span', { class: 'badge ' + (r.ok ? 'ok' : 'err'), text: r.ok ? 'healthy' : 'unhealthy' }),
      r.latency_ms !== null && r.latency_ms !== undefined ? fmtMs(r.latency_ms) : '',
      r.detail ? h('span', { class: 'muted', text: clip(r.detail, 120) }) : null);
  }

  function sourceCard(s) {
    const health = h('div', { class: 'health-slot' }, healthLine(s.name));
    const status = s.configured
      ? h('span', { class: 'badge ok', text: 'configured' })
      : h('span', { class: 'badge warn', title: 'Set in .env: ' + (s.missing_settings || []).join(', '), text: 'needs ' + (s.missing_settings || []).join(', ') });
    const tools = Array.isArray(s.tools) ? s.tools : [];
    const toolList = h('ul', { class: 'tool-list' }, tools.map((t) => h('li', null,
      h('div', { class: 'tl-head' },
        h('span', null, h('code', { text: t.name }), t.federated ? h('span', { class: 'badge accent', text: 'federated' }) : null),
        h('button', { type: 'button', class: 'btn-link', onclick: () => selectTryTool(s.name, t.name, true) }, 'Try')),
      h('div', { class: 'tl-desc', text: t.description || '' }))));
    return h('article', { class: 'card ' + catClass(s.category), dataset: { name: s.name } },
      h('div', { class: 'card-head' },
        h('div', null, h('div', { class: 'card-title', text: s.title || s.name }), h('div', { class: 'card-sub', text: s.name })),
        status),
      h('p', { class: 'card-desc', text: s.description || '' }),
      h('div', { class: 'card-meta' },
        h('span', { class: 'badge outline', text: 'auth: ' + (s.auth || 'none') }),
        s.nih_repository ? h('span', { class: 'badge accent', title: 'NIH repository: ' + s.nih_repository, text: 'NIH repository' }) : null,
        s.homepage ? extLink(s.homepage, 'Homepage') : null,
        s.docs_url ? extLink(s.docs_url, 'API docs') : null),
      health,
      h('div', { class: 'card-actions' },
        h('button', { type: 'button', class: 'btn btn-sm', onclick: () => checkHealth([s.name]) }, 'Check health'),
        tools.length ? h('details', null, h('summary', { class: 'small', text: tools.length + ' tool' + (tools.length === 1 ? '' : 's') }), toolList) : null));
  }

  function renderSourceGroups() {
    const data = cache.sources;
    const root = clear($('#sources-groups'));
    if (!data) return;
    const q = srcView.filter.toLowerCase();
    let shown = 0;
    for (const [key, label] of Object.entries(data.categories || {})) {
      if (srcView.category && srcView.category !== key) continue;
      const group = data.sources.filter((s) => s.category === key && (!q ||
        [s.name, s.title, s.description, (s.tools || []).map((t) => t.name).join(' ')].join(' ').toLowerCase().includes(q)));
      if (!group.length) continue;
      shown += group.length;
      const configured = group.filter((s) => s.configured).length;
      root.appendChild(h('section', { class: 'cat-group ' + catClass(key), 'aria-label': label },
        h('div', { class: 'cat-group-head' },
          h('h2', null, h('span', { class: 'cat-swatch', 'aria-hidden': 'true' }), label),
          h('span', { class: 'muted small', text: configured + ' of ' + group.length + ' configured' })),
        h('div', { class: 'cards' }, group.map(sourceCard))));
    }
    if (!shown) root.appendChild(h('div', { class: 'empty', text: 'No sources match the filter.' }));
  }

  function refreshHealthSlots(names) {
    for (const name of names) {
      const card = document.querySelector('.card[data-name="' + CSS.escape(name) + '"] .health-slot');
      if (card) card.replaceChildren(...[healthLine(name)].filter(Boolean));
    }
  }

  async function checkHealth(names) {
    const all = !names;
    const targets = names || (cache.sources ? cache.sources.sources.map((s) => s.name) : []);
    targets.forEach((n) => srcView.checking.add(n));
    refreshHealthSlots(targets);
    const btn = $('#check-all');
    if (all) { btn.disabled = true; btn.textContent = 'Checking…'; }
    try {
      let results;
      if (all) {
        const data = await api('/api/health/sources', { method: 'POST', body: {} });
        results = data.results || [];
        toast(data.ok + ' healthy, ' + data.failed + ' failing (' + fmtMs(data.elapsed_ms) + ')', data.failed > 0);
      } else {
        results = [await api('/api/sources/' + encodeURIComponent(targets[0]) + '/health', { method: 'POST' })];
      }
      for (const r of results) srcView.health.set(r.source, r);
    } catch (err) {
      toast('Health check failed: ' + err.message, true);
    } finally {
      targets.forEach((n) => srcView.checking.delete(n));
      refreshHealthSlots(targets);
      if (all) { btn.disabled = false; btn.textContent = 'Check all health'; }
    }
  }

  // ---- Try a tool ----
  const tryState = { fields: [] };

  function resolveSchema(schema) {
    let s = schema && typeof schema === 'object' ? schema : {};
    if (!s.type && Array.isArray(s.anyOf)) {
      const nonNull = s.anyOf.filter((x) => x && x.type !== 'null');
      if (nonNull.length >= 1) s = Object.assign({}, nonNull[0], s, { anyOf: undefined, type: nonNull[0].type });
    }
    if (Array.isArray(s.type)) s = Object.assign({}, s, { type: s.type.find((t) => t !== 'null') });
    return s;
  }

  function schemaField(name, rawSchema, required) {
    const s = resolveSchema(rawSchema);
    const id = 'try-f-' + name.replace(/[^\w-]/g, '_');
    const hintText = [s.description, s.minimum !== undefined ? 'min ' + s.minimum : null, s.maximum !== undefined ? 'max ' + s.maximum : null]
      .filter((x) => x !== null && x !== undefined && x !== '').join(' · ');
    const hint = hintText ? h('span', { class: 'hint', id: id + '-hint', text: hintText }) : null;
    const describedBy = hint ? id + '-hint' : null;
    const has = (v) => v !== undefined && v !== null;
    let control;
    let read;
    if (Array.isArray(s.enum)) {
      control = h('select', { id, class: 'input', 'aria-describedby': describedBy },
        h('option', { value: '', text: required ? 'Choose…' : '(default' + (has(s.default) ? ': ' + s.default : '') + ')' }),
        s.enum.map((v) => h('option', { value: String(v), text: String(v) })));
      read = () => {
        if (control.value === '') return undefined;
        const v = s.enum.find((x) => String(x) === control.value);
        return v === undefined ? control.value : v;
      };
    } else if (s.type === 'boolean') {
      control = h('select', { id, class: 'input', 'aria-describedby': describedBy },
        h('option', { value: '', text: '(default' + (has(s.default) ? ': ' + s.default : '') + ')' }),
        h('option', { value: 'true', text: 'true' }), h('option', { value: 'false', text: 'false' }));
      read = () => (control.value === '' ? undefined : control.value === 'true');
    } else if (s.type === 'integer' || s.type === 'number') {
      control = h('input', {
        id, class: 'input', type: 'number', step: s.type === 'integer' ? '1' : 'any', 'aria-describedby': describedBy,
        min: has(s.minimum) ? s.minimum : null, max: has(s.maximum) ? s.maximum : null,
        value: has(s.default) ? s.default : null, placeholder: has(s.default) ? String(s.default) : null,
      });
      read = () => {
        const v = control.value.trim();
        if (v === '') return undefined;
        const n = Number(v);
        if (!isFinite(n) || (s.type === 'integer' && !Number.isInteger(n))) throw new Error(name + ': expected ' + (s.type === 'integer' ? 'an integer' : 'a number'));
        return n;
      };
    } else if (s.type === 'array') {
      const items = resolveSchema(s.items || {});
      if (Array.isArray(items.enum)) {
        control = h('select', { id, class: 'input', multiple: true, size: Math.min(items.enum.length, 5), 'aria-describedby': describedBy },
          items.enum.map((v) => h('option', { value: String(v), text: String(v), selected: Array.isArray(s.default) && s.default.includes(v) })));
        read = () => {
          const vals = Array.from(control.selectedOptions).map((o) => items.enum.find((x) => String(x) === o.value));
          return vals.length ? vals : undefined;
        };
      } else {
        control = h('input', {
          id, class: 'input', type: 'text', 'aria-describedby': describedBy, placeholder: 'comma-separated values',
          value: Array.isArray(s.default) && s.default.length ? s.default.join(', ') : null,
        });
        read = () => {
          const v = control.value.trim();
          if (!v) return undefined;
          if (v.startsWith('[')) { try { return JSON.parse(v); } catch (_) { throw new Error(name + ': invalid JSON array'); } }
          return v.split(',').map((x) => x.trim()).filter(Boolean).map((x) => {
            if (items.type === 'integer' || items.type === 'number') {
              const n = Number(x);
              if (!isFinite(n)) throw new Error(name + ': "' + x + '" is not a number');
              return n;
            }
            if (items.type === 'boolean') return x === 'true';
            return x;
          });
        };
      }
    } else if (s.type === 'object' || !s.type) {
      control = h('textarea', { id, class: 'input', rows: 3, 'aria-describedby': describedBy, placeholder: 'JSON value',
        value: has(s.default) ? JSON.stringify(s.default) : null });
      read = () => {
        const v = control.value.trim();
        if (!v) return undefined;
        try { return JSON.parse(v); } catch (_) {
          if (!s.type) return v;
          throw new Error(name + ': invalid JSON');
        }
      };
    } else {
      control = h('input', {
        id, class: 'input', type: 'text', 'aria-describedby': describedBy,
        value: has(s.default) ? s.default : null,
        placeholder: Array.isArray(s.examples) && s.examples.length ? 'e.g. ' + s.examples[0] : (s.format === 'date' ? 'YYYY-MM-DD' : null),
        maxlength: has(s.maxLength) ? s.maxLength : null,
      });
      read = () => { const v = control.value; return v.trim() === '' ? undefined : v; };
    }
    if (required) control.setAttribute('aria-required', 'true');
    const wrap = h('div', { class: 'field' },
      h('label', { for: id }, name, required ? h('span', { class: 'req', 'aria-hidden': 'true', text: '*' }) : null),
      control, hint);
    return { name, required, wrap, read, control };
  }

  function currentTool() {
    const data = cache.sources;
    if (!data) return null;
    const src = data.sources.find((s) => s.name === $('#try-source').value);
    if (!src) return null;
    return { src, tool: (src.tools || []).find((t) => t.name === $('#try-tool').value) || null };
  }

  function buildTryForm() {
    const cur = currentTool();
    const box = clear($('#try-fields'));
    clear($('#try-result'));
    $('#try-status').textContent = '';
    tryState.fields = [];
    if (!cur || !cur.tool) { $('#try-desc').textContent = ''; return; }
    $('#try-desc').textContent = cur.tool.description || '';
    const schema = cur.tool.input_schema || {};
    const required = new Set(schema.required || []);
    const props = schema.properties || {};
    const names = Object.keys(props).sort((a, b) => (required.has(b) ? 1 : 0) - (required.has(a) ? 1 : 0));
    for (const name of names) {
      const f = schemaField(name, props[name], required.has(name));
      tryState.fields.push(f);
      box.appendChild(f.wrap);
    }
    if (!names.length) box.appendChild(h('p', { class: 'muted small', text: 'This tool takes no parameters.' }));
    $('#try-run').disabled = !cur.src.configured;
    if (!cur.src.configured) $('#try-status').textContent = 'Not configured: set ' + (cur.src.missing_settings || []).join(', ') + ' in .env';
  }

  function fillTryTools() {
    const data = cache.sources;
    const sel = clear($('#try-tool'));
    const src = data && data.sources.find((s) => s.name === $('#try-source').value);
    for (const t of (src && src.tools) || []) sel.appendChild(h('option', { value: t.name, text: t.name + (t.federated ? ' (federated search)' : '') }));
    buildTryForm();
  }

  function selectTryTool(source, tool, focus) {
    $('#try-source').value = source;
    fillTryTools();
    $('#try-tool').value = tool;
    buildTryForm();
    if (focus) {
      $('#try-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
      const first = tryState.fields[0];
      (first ? first.control : $('#try-run')).focus({ preventScroll: true });
    }
  }

  function initTryPanel(data) {
    const sel = clear($('#try-source'));
    for (const [key, label] of Object.entries(data.categories || {})) {
      const group = data.sources.filter((s) => s.category === key && (s.tools || []).length);
      if (!group.length) continue;
      sel.appendChild(h('optgroup', { label }, group.map((s) =>
        h('option', { value: s.name, text: s.title + ' (' + s.name + ')' + (s.configured ? '' : ' - not configured') }))));
    }
    fillTryTools();
  }

  function recordsTable(records) {
    const rows = records.map((r) => {
      const ids = Object.entries(r.identifiers || {}).slice(0, 4);
      return h('tr', null,
        h('td', { class: 'wrap' }, r.url ? extLink(r.url, r.title || r.id) : h('span', { text: r.title || r.id }),
          r.authors && r.authors.length ? h('div', { class: 'muted small', text: clip(r.authors.slice(0, 4).join(', ') + (r.authors.length > 4 ? ', et al.' : ''), 160) }) : null),
        h('td', null, h('span', { class: 'badge outline', text: r.type || 'other' })),
        h('td', null, h('code', { text: r.id })),
        h('td', { text: r.date || '' }),
        h('td', null, h('div', { class: 'id-chips' }, ids.map(([k, v]) => h('span', { class: 'id-chip' }, h('b', { text: k + ':' }), String(v))))));
    });
    return h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
      h('thead', null, h('tr', null, ['Title', 'Type', 'ID', 'Date', 'Identifiers'].map((c) => h('th', { scope: 'col', text: c })))),
      h('tbody', null, rows)));
  }

  function renderToolResult(result, elapsed) {
    const box = clear($('#try-result'));
    const records = Array.isArray(result.records) ? result.records : [];
    box.appendChild(h('div', { class: 'result-meta' },
      sourceBadge(result.source), h('code', { text: result.tool }),
      h('span', { text: fmtNum(records.length) + (result.total !== null && result.total !== undefined ? ' of ' + fmtNum(result.total) : '') + ' record(s)' }),
      h('span', { class: 'muted', text: fmtMs(elapsed) }),
      result.next_cursor ? h('span', { class: 'badge outline', text: 'next cursor: ' + clip(result.next_cursor, 40) }) : null));
    if (result.request_url) box.appendChild(h('div', { class: 'result-meta' }, h('span', { class: 'url', text: result.request_url })));
    for (const note of result.notes || []) box.appendChild(h('div', { class: 'note-box', text: note }));
    if (records.length) box.appendChild(recordsTable(records));
    if (result.data !== null && result.data !== undefined) {
      box.appendChild(h('details', { open: !records.length }, h('summary', { class: 'small', text: 'Data payload' }), h('pre', { class: 'json', text: json(result.data) })));
    }
    if (!records.length && (result.data === null || result.data === undefined)) box.appendChild(h('div', { class: 'empty', text: 'No records returned.' }));
    box.appendChild(h('details', null, h('summary', { class: 'small', text: 'Raw JSON' }), h('pre', { class: 'json', text: json(result) })));
  }

  async function runTryTool(e) {
    e.preventDefault();
    const cur = currentTool();
    if (!cur || !cur.tool) return;
    const params = {};
    try {
      for (const f of tryState.fields) {
        const v = f.read();
        if (v === undefined) {
          if (f.required) throw new Error(f.name + ' is required');
          continue;
        }
        params[f.name] = v;
      }
    } catch (err) {
      $('#try-status').textContent = err.message;
      return;
    }
    const btn = $('#try-run');
    btn.disabled = true;
    $('#try-status').replaceChildren(h('span', { class: 'spinner', 'aria-hidden': 'true' }), ' Running ' + cur.src.name + '.' + cur.tool.name + '…');
    const started = performance.now();
    try {
      const result = await api('/api/call', { method: 'POST', body: { source: cur.src.name, tool: cur.tool.name, params } });
      $('#try-status').textContent = '';
      renderToolResult(result, performance.now() - started);
    } catch (err) {
      $('#try-status').textContent = '';
      const box = clear($('#try-result'));
      box.appendChild(errorBox(err));
      if (err.data && err.data.kind) box.appendChild(h('p', { class: 'muted small', text: 'kind: ' + err.data.kind + (err.data.upstream_status ? ' · upstream HTTP ' + err.data.upstream_status : '') }));
    } finally {
      btn.disabled = !cur.src.configured;
    }
  }

  async function initSources() {
    $('#sources-filter').addEventListener('input', (e) => { srcView.filter = e.target.value.trim(); renderSourceGroups(); });
    $('#check-all').addEventListener('click', () => checkHealth(null));
    $('#try-source').addEventListener('change', fillTryTools);
    $('#try-tool').addEventListener('change', buildTryForm);
    $('#try-form').addEventListener('submit', runTryTool);
  }
  loaders.sources = async (first) => {
    if (!first) return;
    try {
      const data = await loadSources();
      const configured = data.sources.filter((s) => s.configured).length;
      const tools = data.sources.reduce((n, s) => n + (s.tools || []).length, 0);
      $('#sources-summary').textContent = data.sources.length + ' sources in ' + Object.keys(data.categories).length + ' categories · ' +
        configured + ' configured · ' + tools + ' tools. Unconfigured sources list the setting names to add to .env.';
      renderSourceCats(data);
      renderSourceGroups();
      initTryPanel(data);
    } catch (err) {
      loaded.delete('sources');
      $('#sources-summary').textContent = '';
      $('#sources-groups').replaceChildren(errorBox(err));
    }
  };

  // ===================================================================
  // Cross-database search
  // ===================================================================
  const searchState = { cats: new Set(), controller: null };

  function renderSearchCats(data) {
    const box = $('#search-cats');
    box.querySelectorAll('.chip').forEach((c) => c.remove());
    for (const [key, label] of Object.entries(data.categories || {})) {
      const n = data.sources.filter((s) => s.category === key && s.configured && (s.tools || []).some((t) => t.federated)).length;
      if (!n) continue;
      const chip = h('button', {
        type: 'button', class: 'chip', 'aria-pressed': String(searchState.cats.has(key)), title: label,
        onclick: () => {
          if (searchState.cats.has(key)) searchState.cats.delete(key); else searchState.cats.add(key);
          chip.setAttribute('aria-pressed', String(searchState.cats.has(key)));
        },
      }, shortCat(key, label), h('span', { class: 'count', text: String(n) }));
      box.appendChild(chip);
    }
  }

  function clusterCard(c) {
    const ids = Object.entries(c.identifiers || {});
    const linked = (c.sources || []).length > 1;
    return h('article', { class: 'cluster' + (linked ? ' linked' : '') },
      h('div', { class: 'card-meta' },
        h('span', { class: 'badge outline', text: c.type || 'other' }),
        linked ? h('span', { class: 'badge accent', text: 'in ' + c.sources.length + ' databases' }) : null,
        c.date ? h('span', { class: 'muted small', text: c.date }) : null),
      h('div', { class: 'cluster-title', text: c.title || '(untitled)' }),
      ids.length ? h('div', { class: 'id-chips', 'aria-label': 'Shared identifiers' }, ids.slice(0, 10).map(([k, v]) => h('span', { class: 'id-chip' }, h('b', { text: k + ':' }), String(v)))) : null,
      h('ul', { class: 'member-list', 'aria-label': 'Records' }, (c.records || []).map((r) => h('li', null,
        sourceBadge(r.source), ' ', r.url ? extLink(r.url, clip(r.id, 40)) : h('code', { text: clip(r.id, 40) })))));
  }

  function recordItem(r) {
    const ids = Object.entries(r.identifiers || {}).slice(0, 6);
    return h('li', { class: 'record' },
      h('div', { class: 'record-title' }, r.url ? extLink(r.url, r.title || r.id) : h('span', { text: r.title || r.id })),
      h('div', { class: 'record-meta' },
        h('span', { class: 'badge outline', text: r.type || 'other' }),
        r.date ? h('span', { text: r.date }) : null,
        h('code', { text: r.id }),
        r.authors && r.authors.length ? h('span', { text: clip(r.authors.slice(0, 3).join(', ') + (r.authors.length > 3 ? ' et al.' : ''), 120) }) : null),
      ids.length ? h('div', { class: 'id-chips' }, ids.map(([k, v]) => h('span', { class: 'id-chip' }, h('b', { text: k + ':' }), String(v)))) : null,
      r.summary ? h('div', { class: 'record-summary', text: r.summary }) : null);
  }

  function renderSearchResults(res) {
    const box = clear($('#search-results'));
    const statuses = (res.sources || []).slice().sort((a, b) => (b.ok - a.ok) || a.source.localeCompare(b.source));
    const okCount = statuses.filter((s) => s.ok).length;
    const linked = (res.clusters || []).filter((c) => (c.sources || []).length > 1)
      .sort((a, b) => b.sources.length - a.sources.length || Object.keys(b.identifiers || {}).length - Object.keys(a.identifiers || {}).length);
    box.appendChild(h('p', { class: 'muted', text:
      fmtNum((res.records || []).length) + ' records from ' + okCount + ' of ' + statuses.length + ' sources · ' +
      linked.length + ' entities linked across databases · ' + fmtMs(res.elapsed_ms) }));
    box.appendChild(h('div', { class: 'status-chips', role: 'list', 'aria-label': 'Per-source status' }, statuses.map((s) =>
      h('span', { class: 'status-chip ' + (s.ok ? (s.returned ? 'ok' : 'zero') : 'err'), role: 'listitem', title: s.ok ? (s.returned ? '' : 'no matches') : (s.error || 'failed') },
        sourceBadge(s.source),
        s.ok ? fmtNum(s.returned) + (s.total !== null && s.total !== undefined ? ' / ' + fmtNum(s.total) : '') : 'failed',
        h('span', { class: 'lat', text: fmtMs(s.latency_ms) })))));
    const failed = statuses.filter((s) => !s.ok);
    if (failed.length) {
      box.appendChild(h('details', null, h('summary', { class: 'small muted', text: failed.length + ' source(s) failed - details' }),
        h('ul', { class: 'small' }, failed.map((s) => h('li', null, h('b', { text: s.source + ': ' }), s.error || 'failed')))));
    }
    box.appendChild(h('div', { class: 'section-title' }, h('h2', { text: 'Linked entities' }),
      h('span', { class: 'muted small', text: 'found in 2+ databases, joined on shared identifiers' })));
    if (linked.length) box.appendChild(h('div', { class: 'clusters' }, linked.slice(0, 60).map(clusterCard)));
    else box.appendChild(h('div', { class: 'empty', text: 'No entity in these results appears in two or more databases. Try a more specific query (a drug, gene, DOI or trial).' }));

    box.appendChild(h('div', { class: 'section-title' }, h('h2', { text: 'Records by source' })));
    const bySource = new Map();
    for (const r of res.records || []) {
      if (!bySource.has(r.source)) bySource.set(r.source, []);
      bySource.get(r.source).push(r);
    }
    if (!bySource.size) box.appendChild(h('div', { class: 'empty', text: 'No records.' }));
    for (const [name, recs] of bySource) {
      box.appendChild(h('details', { class: 'source-group', open: true },
        h('summary', null, sourceBadge(name), h('span', { text: recs.length + ' record' + (recs.length === 1 ? '' : 's') })),
        h('ul', { class: 'record-list' }, recs.map(recordItem))));
    }
  }

  async function runSearch(e) {
    e.preventDefault();
    const q = $('#search-q').value.trim();
    if (!q) { $('#search-q').focus(); return; }
    if (searchState.controller) searchState.controller.abort();
    searchState.controller = new AbortController();
    const btn = $('#search-go');
    btn.disabled = true;
    $('#search-results').replaceChildren(h('div', { class: 'typing' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Searching every selected database in parallel…'));
    try {
      const body = { query: q, limit_per_source: Number($('#search-limit').value) || 5 };
      if (searchState.cats.size) body.categories = Array.from(searchState.cats);
      const res = await api('/api/search', { method: 'POST', body, signal: searchState.controller.signal });
      renderSearchResults(res);
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      $('#search-results').replaceChildren(errorBox(err));
    } finally {
      btn.disabled = false;
    }
  }

  function renderResolve(res) {
    const box = clear($('#resolve-results'));
    const entries = Object.entries(res.agreement || {});
    box.appendChild(h('p', { class: 'muted', text:
      entries.length + ' identifier' + (entries.length === 1 ? '' : 's') + ' for ' + res.kind + ' "' + res.query + '" from ' +
      (res.candidates || []).length + ' source(s)' }));
    if (entries.length) {
      box.appendChild(h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
        h('thead', null, h('tr', null, h('th', { text: 'Identifier' }), h('th', { text: 'Value' }), h('th', { text: 'Agreeing sources' }))),
        h('tbody', null, entries.map(([key, a]) => h('tr', null,
          h('td', null, h('code', { text: key })),
          h('td', null, h('b', { text: a.value })),
          h('td', null, (a.sources || []).map((s) => sourceBadge(s.split(':')[0])))))))));
    }
    const conflicts = Object.entries(res.conflicts || {});
    if (conflicts.length) {
      box.appendChild(h('details', { open: true }, h('summary', { class: 'small', text: conflicts.length + ' conflicting identifier(s): sources disagree' }),
        h('ul', { class: 'small' }, conflicts.map(([key, options]) => h('li', null, h('code', { text: key }), ': ',
          options.map((o, i) => h('span', null, i ? '; ' : '', h('b', { text: o.value }), ' (' + (o.sources || []).join(', ') + ')')))))));
    }
    if ((res.candidates || []).length) {
      box.appendChild(h('div', { class: 'status-chips', role: 'list', 'aria-label': 'Best match per source' }, res.candidates.map((c) =>
        h('span', { class: 'status-chip ' + (c.exact_match ? 'ok' : 'zero'), role: 'listitem', title: c.exact_match ? 'exact name match' : 'top hit, not an exact match' },
          sourceBadge(c.source.split(':')[0]), c.url ? extLink(c.url, clip(c.title || c.id, 40)) : clip(c.title || c.id, 40)))));
    }
    const errors = Object.entries(res.errors || {});
    if (errors.length) {
      box.appendChild(h('details', null, h('summary', { class: 'small muted', text: errors.length + ' source(s) failed - details' }),
        h('ul', { class: 'small' }, errors.map(([s, msg]) => h('li', null, h('b', { text: s + ': ' }), msg)))));
    }
    for (const note of res.notes || []) box.appendChild(h('p', { class: 'small muted', text: note }));
  }

  async function runResolve(e) {
    e.preventDefault();
    const q = $('#resolve-q').value.trim();
    if (!q) { $('#resolve-q').focus(); return; }
    const btn = $('#resolve-go');
    btn.disabled = true;
    $('#resolve-results').replaceChildren(h('div', { class: 'typing' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Asking the authoritative databases…'));
    try {
      renderResolve(await api('/api/resolve', { method: 'POST', body: { kind: $('#resolve-kind').value, query: q } }));
    } catch (err) {
      $('#resolve-results').replaceChildren(errorBox(err));
    } finally {
      btn.disabled = false;
    }
  }

  function initSearch() {
    $('#search-form').addEventListener('submit', runSearch);
    $('#resolve-form').addEventListener('submit', runResolve);
  }
  loaders.search = async (first) => {
    if (first) {
      try { renderSearchCats(await loadSources()); } catch (err) { loaded.delete('search'); toast(err.message, true); }
    }
    $('#search-q').focus({ preventScroll: true });
  };

  // ===================================================================
  // NIH repository catalog
  // ===================================================================
  const catState = { items: [], shown: 50, timer: 0, seq: 0 };

  function accessBadges(access) {
    const parts = String(access || '').split(',').map((x) => x.trim()).filter(Boolean);
    const cls = { open: 'ok', registered: 'accent', controlled: 'warn' };
    return h('div', { class: 'access-tags' }, parts.length ? parts.map((p) => h('span', { class: 'badge ' + (cls[p.toLowerCase()] || ''), text: p })) : '-');
  }

  function renderCatalog() {
    const box = clear($('#catalog-results'));
    const items = catState.items;
    if (!items.length) { box.appendChild(h('div', { class: 'empty', text: 'No repositories match these filters.' })); return; }
    const rows = items.slice(0, catState.shown).map((r) => h('tr', null,
      h('td', { class: 'wrap' },
        h('div', { class: 'repo-name' }, r.url ? extLink(r.url, r.name) : r.name),
        r.subject_area ? h('div', { class: 'muted small', text: r.subject_area }) : null,
        r.description ? h('details', { class: 'repo-desc' }, h('summary', { text: 'Description' }), h('p', { text: r.description })) : null),
      h('td', null, h('span', { class: 'badge ' + (r.kind === 'generalist' ? 'accent' : 'outline'), text: r.kind === 'generalist' ? 'generalist' : 'domain-specific' })),
      h('td', null, accessBadges(r.access_type)),
      h('td', { class: 'small', text: (r.institutes || []).join(', ') || '-' }),
      h('td', { class: 'small', text: r.model_system || '-' }),
      h('td', null, (r.connectors || []).length
        ? h('div', { class: 'access-tags' }, r.connectors.map((c) => h('span', { class: 'badge ok', title: 'Queryable live through the ' + c + ' connector', text: 'live: ' + c })))
        : h('span', { class: 'muted small', text: '-' })),
      h('td', null, r.data_access_url ? extLink(r.data_access_url, 'Data access') : '-')));
    box.appendChild(h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
      h('thead', null, h('tr', null, ['Repository', 'Kind', 'Access', 'Institutes', 'Model system', 'Mosaic connector', 'Data access'].map((c) => h('th', { scope: 'col', text: c })))),
      h('tbody', null, rows))));
    const pager = h('div', { class: 'pager' }, h('span', { text: 'Showing ' + Math.min(catState.shown, items.length) + ' of ' + items.length }));
    if (catState.shown < items.length) {
      pager.appendChild(h('button', { type: 'button', class: 'btn btn-sm', onclick: () => { catState.shown += 50; renderCatalog(); } }, 'Show more'));
    }
    box.appendChild(pager);
  }

  async function searchCatalog() {
    const params = new URLSearchParams({ limit: '500' });
    const q = $('#cat-q').value.trim();
    if (q) params.set('q', q);
    if ($('#cat-kind').value) params.set('kind', $('#cat-kind').value);
    if ($('#cat-access').value) params.set('access_type', $('#cat-access').value);
    if ($('#cat-inst').value) params.set('institute', $('#cat-inst').value);
    if ($('#cat-live').checked) params.set('has_connector', 'true');
    const seq = ++catState.seq;
    try {
      const data = await api('/api/catalog?' + params.toString());
      if (seq !== catState.seq) return;
      catState.items = data.repositories || [];
      catState.shown = 50;
      renderCatalog();
    } catch (err) {
      if (seq === catState.seq) $('#catalog-results').replaceChildren(errorBox(err));
    }
  }

  function initCatalog() {
    $('#catalog-form').addEventListener('submit', (e) => { e.preventDefault(); searchCatalog(); });
    $('#cat-q').addEventListener('input', () => { clearTimeout(catState.timer); catState.timer = setTimeout(searchCatalog, 250); });
    ['#cat-kind', '#cat-access', '#cat-inst', '#cat-live'].forEach((s) => $(s).addEventListener('change', searchCatalog));
  }
  loaders.catalog = async (first) => {
    if (!first) return;
    try {
      const stats = await api('/api/catalog/stats');
      $('#catalog-summary').textContent = fmtNum(stats.total) + ' repositories from NIH’s “Accessing Scientific Data” list: ' +
        fmtNum(stats.domain_specific) + ' domain-specific and ' + fmtNum(stats.generalist) + ' generalist; ' +
        fmtNum(stats.with_live_connector) + ' can be queried live through a Mosaic connector.' +
        (stats.retrieved ? ' Retrieved ' + stats.retrieved + '.' : '');
      const inst = $('#cat-inst');
      for (const [name, n] of Object.entries((stats.facets && stats.facets.institutes) || {})) {
        inst.appendChild(h('option', { value: name, text: name + ' (' + n + ')' }));
      }
    } catch (err) {
      loaded.delete('catalog');
      $('#catalog-summary').textContent = err.message;
    }
    searchCatalog();
  };

  // ===================================================================
  // Exports
  // ===================================================================
  async function loadExports() {
    const box = $('#exports-list');
    try {
      const data = await api('/api/exports');
      const items = data.exports || [];
      clear(box);
      if (!items.length) {
        box.appendChild(h('div', { class: 'empty', text: 'No saved tables yet. Ask Mosaic to "save a table" during a cross-database question.' }));
        return;
      }
      const rows = items.map((e) => h('tr', null,
        h('td', { class: 'wrap' }, h('div', { class: 'repo-name', text: e.name || e.export_id }),
          e.description ? h('div', { class: 'muted small', text: clip(e.description, 200) }) : null,
          h('code', { class: 'small muted', text: e.export_id })),
        h('td', { class: 'num', text: fmtNum(e.n_rows) }),
        h('td', { class: 'wrap small', title: (e.columns || []).join(', '), text: clip((e.columns || []).join(', '), 140) || '-' }),
        h('td', { class: 'small', text: fmtTime(e.created_at) }),
        h('td', { class: 'small' }, e.session_id ? h('code', { text: String(e.session_id).slice(0, 12) }) : '-'),
        h('td', null, h('div', { class: 'tc-actions' },
          exportUrl(e, 'csv') ? h('a', { class: 'btn btn-sm', href: exportUrl(e, 'csv'), download: '' }, 'CSV') : null,
          exportUrl(e, 'json') ? h('a', { class: 'btn btn-sm', href: exportUrl(e, 'json'), download: '' }, 'JSON') : null,
          exportUrl(e, 'json') ? h('button', { type: 'button', class: 'btn btn-sm', onclick: () => previewExport(e) }, 'Preview') : null))));
      box.appendChild(h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
        h('thead', null, h('tr', null, ['Table', 'Rows', 'Columns', 'Created', 'Session', 'Download'].map((c, i) => h('th', { scope: 'col', class: i === 1 ? 'num' : null, text: c })))),
        h('tbody', null, rows))));
    } catch (err) {
      box.replaceChildren(errorBox(err));
    }
  }

  async function previewExport(e) {
    const box = $('#export-preview');
    box.replaceChildren(h('div', { class: 'typing' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Loading…'));
    try {
      const data = await api(exportUrl(e, 'json'));
      const columns = Array.isArray(data.columns) ? data.columns.map(String) : [];
      const rows = Array.isArray(data.rows) ? data.rows : [];
      const cell = (v) => {
        if (v === null || v === undefined) return '';
        if (typeof v === 'object') return JSON.stringify(v);
        return String(v);
      };
      const cellNode = (v) => {
        const text = cell(v);
        return safeUrl(text, false) && /^https?:\/\//.test(text) ? extLink(text, clip(text, 60)) : clip(text, 300);
      };
      const panel = h('section', { class: 'panel', 'aria-label': 'Export preview' },
        h('div', { class: 'panel-head row' }, h('h2', { text: data.name || e.name }),
          h('button', { type: 'button', class: 'btn btn-sm', onclick: () => clear(box) }, 'Close')),
        data.description ? h('p', { class: 'muted', text: data.description }) : null,
        h('p', { class: 'small muted', text: 'Showing ' + Math.min(rows.length, 100) + ' of ' + rows.length + ' rows' }),
        h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
          h('thead', null, h('tr', null, columns.map((c) => h('th', { scope: 'col', text: c })))),
          h('tbody', null, rows.slice(0, 100).map((row) => h('tr', null, columns.map((c, i) =>
            h('td', null, cellNode(Array.isArray(row) ? row[i] : row && row[c])))))))),
        data.provenance ? h('details', null, h('summary', { class: 'small', text: 'Provenance' }), h('pre', { class: 'json', text: json(data.provenance) })) : null);
      box.replaceChildren(panel);
      panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (err) {
      box.replaceChildren(errorBox(err));
    }
  }

  function initExports() { $('#exports-refresh').addEventListener('click', loadExports); }
  loaders.exports = () => loadExports();

  // ===================================================================
  // Audit log
  // ===================================================================
  function tile(value, label) { return h('div', { class: 'tile-stat' }, h('div', { class: 'v', text: value }), h('div', { class: 'k', text: label })); }

  function simpleTable(headers, rows, numeric) {
    if (!rows.length) return h('div', { class: 'empty', text: 'Nothing recorded in this window.' });
    const num = new Set(numeric || []);
    return h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
      h('thead', null, h('tr', null, headers.map((c, i) => h('th', { scope: 'col', class: num.has(i) ? 'num' : null, text: c })))),
      h('tbody', null, rows.map((cells) => h('tr', null, cells.map((c, i) => h('td', { class: num.has(i) ? 'num' : null }, c)))))));
  }

  async function loadAudit() {
    const hours = $('#audit-window').value || '24';
    const source = $('#audit-source').value;
    const session = $('#audit-session').value.trim();
    const qs = new URLSearchParams({ limit: '200' });
    if (source) qs.set('source', source);
    if (session) qs.set('session_id', session);
    try {
      const [stats, calls, turns] = await Promise.all([
        api('/api/audit/stats?since_hours=' + encodeURIComponent(hours)),
        api('/api/audit/tool-calls?' + qs.toString()),
        api('/api/audit/agent-turns?limit=50' + (session ? '&session_id=' + encodeURIComponent(session) : '')),
      ]);
      const tc = stats.tool_calls || {};
      const ag = stats.agent || {};
      $('#audit-tiles').replaceChildren(
        tile(fmtNum(tc.calls), 'tool calls'),
        tile(tc.success_rate === null || tc.success_rate === undefined ? '-' : (tc.success_rate * 100).toFixed(1) + '%', 'success rate'),
        tile(fmtMs(tc.avg_latency_ms), 'average latency'),
        tile(fmtNum(ag.turns), 'agent turns'),
        tile(fmtNum(ag.sessions), 'sessions'),
        tile(fmtNum(ag.input_tokens), 'input tokens'),
        tile(fmtNum(ag.output_tokens), 'output tokens'),
        tile(fmtNum(stats.exports), 'tables saved'));
      const per = stats.sources || [];
      const sel = $('#audit-source');
      const known = new Set(Array.from(sel.options).map((o) => o.value));
      for (const s of per) if (!known.has(s.source)) sel.appendChild(h('option', { value: s.source, text: s.source }));
      $('#audit-sources').replaceChildren(simpleTable(['Source', 'Calls', 'OK', 'Failed', 'Avg latency', 'Last call'],
        per.map((s) => [sourceBadge(s.source), fmtNum(s.calls), fmtNum(s.ok_calls), fmtNum(s.failed_calls), fmtMs(s.avg_latency_ms), h('span', { class: 'nowrap', text: relTime(s.last_call) })]), [1, 2, 3, 4]));
      $('#audit-turns').replaceChildren(simpleTable(['Time', 'Session', 'Model', 'Stop', 'In', 'Out', 'Tools'],
        (turns.agent_turns || []).map((t) => [fmtShort(t.ts), h('code', { text: String(t.session_id || '').slice(0, 10) }), t.model || '-', t.stop_reason || '-',
          fmtNum(t.input_tokens), fmtNum(t.output_tokens), fmtNum(t.tool_calls)]), [4, 5, 6]));
      $('#audit-calls').replaceChildren(simpleTable(['Time', 'Source', 'Tool', 'Status', 'Latency', 'Records', 'Session', 'Error'],
        (calls.tool_calls || []).map((c) => [
          fmtShort(c.ts), sourceBadge(c.source), h('code', { text: c.tool }),
          h('span', { class: 'badge ' + (c.ok ? 'ok' : 'err'), text: c.ok ? 'ok' : 'failed' }),
          fmtMs(c.latency_ms),
          c.n_records === null || c.n_records === undefined ? '-' : fmtNum(c.n_records) + (c.total !== null && c.total !== undefined ? ' / ' + fmtNum(c.total) : ''),
          c.session_id ? h('code', { text: String(c.session_id).slice(0, 10) }) : '-',
          c.error ? h('span', { class: 'clip', title: c.error, text: c.error }) : '']), [4, 5]));
    } catch (err) {
      $('#audit-tiles').replaceChildren(errorBox(err));
    }
  }

  function initAudit() {
    $('#audit-refresh').addEventListener('click', loadAudit);
    $('#audit-window').addEventListener('change', loadAudit);
    $('#audit-filter').addEventListener('submit', (e) => { e.preventDefault(); loadAudit(); });
  }
  loaders.audit = () => loadAudit();

  // ===================================================================
  // Boot
  // ===================================================================
  function init() {
    $('#nav-toggle').addEventListener('click', () => {
      const open = $('#nav').classList.toggle('open');
      $('#nav-toggle').setAttribute('aria-expanded', String(open));
    });
    initAsk();
    initSources();
    initSearch();
    initCatalog();
    initExports();
    initAudit();
    window.addEventListener('hashchange', route);
    refreshHealth();
    loadSources().catch(() => {});
    setInterval(refreshHealth, 60000);
    route();
  }

  // Exposed for debugging in the browser console only (no side effects).
  window.MosaicUI = { renderMarkdown, inline, safeUrl };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
