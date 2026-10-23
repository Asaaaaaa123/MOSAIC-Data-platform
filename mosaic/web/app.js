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
    // Browsers silently drop TAB/CR/LF (and other control chars) from URLs, which can turn
    // "/	/evil.example" into a protocol-relative URL: strip them before any check.
    const v = value.replace(/[\u0000-\u001F\u007F]/g, '').trim();
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
  const VIEWS = ['ask', 'sources', 'search', 'species', 'catalog', 'exports', 'audit'];
  const loaders = {};
  const loaded = new Set();

  function route() {
    const name = (location.hash || '#ask').slice(1).split(/[/?]/)[0];
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
    const titles = { ask: 'Ask Mosaic', sources: 'Sources', search: 'Cross-database search', species: 'Cross-species comparison', catalog: 'NIH repositories', exports: 'Exports', audit: 'Audit log' };
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
        case 'species_comparison': this.speciesComparison(ev); break;
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

    speciesComparison(ev) {
      this.addNode(speciesChatCard(ev));
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
    const q = r.quality && typeof r.quality === 'object' ? r.quality : null;
    return h('li', null, q ? gradeBadge(q) : null, q ? ' ' : null, titleNode,
      meta ? h('span', { class: 'pmeta', text: meta }) : null,
      q && (q.flags || []).length ? h('span', { class: 'flags' }, q.flags.map((f) => h('span', { class: 'flag', text: f }))) : null);
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

  const PACKAGE_PATH_RE = /^\/api\/exports\/[0-9a-f]{32}\/[A-Za-z0-9_]{1,64}\.(csv|xlsx|zip|txt|json)(\/preview)?$/;
  const PACKAGE_FIGURE_RE = /^\/api\/exports\/[0-9a-f]{32}\/figures\/[a-z0-9_]{1,48}\.(svg|png|pdf|txt)$/;
  /** Same-origin URL of a data-package file (strictly validated), or null. */
  function pkgUrl(u) { return typeof u === 'string' && PACKAGE_PATH_RE.test(u) ? u : null; }

  /** Figures and data checks of a package (Figure Studio SVG / PNG / PDF files in its figures/ folder). */
  function packageFigures(ev) {
    const figs = (Array.isArray(ev.figures) ? ev.figures : []).filter((f) => f && f.urls && Object.values(f.urls).some((u) => PACKAGE_FIGURE_RE.test(u)));
    const checks = ev.checks && ev.checks.total ? ev.checks : null;
    if (!figs.length && !checks) return null;
    const ok = checks && checks.passed === checks.total;
    return h('div', { class: 'pkg-figs small' },
      checks ? h('span', { class: 'badge ' + (ok ? 'ok' : 'err'), text: (ok ? '✓ ' : '✗ ') + 'Data checks ' + checks.passed + '/' + checks.total }) : null,
      figs.length ? h('span', { class: 'muted', text: ' Figures: ' }) : null,
      figs.map((f) => h('span', { class: 'pkg-fig' }, h('b', { text: f.title || f.name }), ' ',
        ['svg', 'png', 'pdf'].filter((x) => PACKAGE_FIGURE_RE.test((f.urls || {})[x] || '')).map((x) =>
          h('a', { class: 'btn btn-xs', href: f.urls[x] + (x === 'svg' ? '' : '?download=1'), target: x === 'svg' ? '_blank' : null,
            rel: 'noopener', download: x === 'svg' ? null : '' }, x.toUpperCase())))));
  }

  /** Card for a multi-file data package (chat event or Exports entry). */
  function packageCard(ev, onPreview) {
    const files = Array.isArray(ev.files) ? ev.files : [];
    const xlsx = pkgUrl(ev.xlsx_url);
    const zip = pkgUrl(ev.zip_url);
    return h('div', { class: 'table-card package-card', role: 'group', 'aria-label': 'Saved data package' },
      icon('table', 'tc-icon'),
      h('div', { class: 'tc-main' },
        h('div', { class: 'tc-name', text: 'Data package saved: ' + (ev.name || 'export') }),
        h('div', { class: 'small', text: files.length + ' file' + (files.length === 1 ? '' : 's') + ' · ' + fmtNum(ev.n_rows) + ' rows in total' +
          (ev.group_by ? ' · split by ' + ev.group_by : '') }),
        h('ul', { class: 'pkg-files' }, files.map((f) => h('li', null,
          pkgUrl(f.url) ? h('a', { href: pkgUrl(f.url), download: '' }, f.file) : h('code', { text: f.file }),
          h('span', { class: 'muted small', text: ' ' + fmtNum(f.rows) + ' rows · ' + (f.title || '') }),
          onPreview && pkgUrl(f.preview_url) ? h('button', { type: 'button', class: 'btn btn-xs', onclick: () => onPreview(ev, f) }, 'Preview') : null))),
        packageFigures(ev),
        ev.folder ? h('div', { class: 'small muted' }, 'On disk: ', h('code', { text: ev.folder })) : null),
      h('div', { class: 'tc-actions' },
        xlsx ? h('a', { class: 'btn btn-sm btn-primary', href: xlsx, download: '' }, 'Excel workbook') : null,
        zip ? h('a', { class: 'btn btn-sm', href: zip, download: '' }, 'ZIP (all files)') : null));
  }

  function tableCard(ev) {
    if (ev && ev.kind === 'package') return packageCard(ev);
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
      for (const comp of Array.isArray(entry.comparisons) ? entry.comparisons : []) {
        if (comp && typeof comp === 'object') view.speciesComparison(comp);
      }
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
  // Results come in four tabs: the data (entity cards), a mind map, charts (drawn by Figure Studio
  // from the counted values) and a comparison (queries side by side, chosen entities side by side).
  const searchState = { cats: new Set(), controller: null, res: null, byId: new Map(), selected: [], showTab: null, showAll: null };
  const SEARCH_ID_RE = /^[0-9a-f]{32}$/;
  const MAX_COMPARE_ENTITIES = 4;
  const GROUP_CAT = {
    papers: 'literature', clinical_trials: 'clinical', safety_regulatory: 'clinical', drugs_compounds: 'drugs',
    bioactivity_assays: 'chemistry', genes_proteins: 'proteins', variants_associations: 'genomics', diseases_phenotypes: 'biology',
    pathways_ontology: 'biology', datasets_repositories: 'datasets', projects_studies: 'datasets',
  };

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

  function humanKey(key) {
    const s = String(key).replace(/_/g, ' ');
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  /** Render a cleaned field value as text: lists joined, small objects as "k: v". */
  function fieldText(v) {
    if (v === null || v === undefined) return '';
    if (typeof v === 'number') return Number.isInteger(v) && v >= 1800 && v <= 2100 ? String(v) : fmtNum(v); // years stay "2011"
    if (typeof v === 'boolean') return v ? 'yes' : 'no';
    if (Array.isArray(v)) return v.map(fieldText).filter(Boolean).join('; ');
    if (typeof v === 'object') return Object.entries(v).map(([k, x]) => humanKey(k) + ': ' + fieldText(x)).join(', ');
    return String(v);
  }

  function gradeBadge(evaluation) {
    const g = (evaluation && evaluation.grade) || '?';
    return h('span', {
      class: 'grade grade-' + g, title: ((evaluation && evaluation.reasons) || []).join('\n') || 'quality grade',
      'aria-label': 'Quality grade ' + g + (evaluation ? ', score ' + evaluation.score : ''),
    }, g, evaluation ? h('small', { text: ' ' + evaluation.score }) : null);
  }

  function fieldTable(rows, e) {
    return h('table', { class: 'data kv' }, h('tbody', null, rows.map(([k, v]) => h('tr', null,
      h('th', { text: humanKey(k), title: 'from ' + ((e.field_sources || {})[k] || '?') }),
      h('td', { text: clip(fieldText(v), 400) })))));
  }

  function entityQueries(id) {
    const vis = searchState.res && searchState.res.visuals;
    return vis && vis.compare ? ((vis.entity_queries || {})[id] || []) : [];
  }
  function queryIndex(q) { return Math.max(0, ((searchState.res && searchState.res.queries) || []).indexOf(q)); }
  function queryBadges(list) {
    return list.length ? h('span', { class: 'q-badges' }, list.map((q) => h('span', { class: 'badge q-badge q' + (queryIndex(q) % 4), text: q }))) : null;
  }

  function compareToggle(id) {
    const on = searchState.selected.includes(id);
    return h('label', { class: 'cmp-toggle small', title: 'Add to the comparison (up to ' + MAX_COMPARE_ENTITIES + ')' },
      h('input', { type: 'checkbox', checked: on, dataset: { cmp: id }, onchange: () => toggleCompare(id) }), 'Compare');
  }

  function toggleCompare(id) {
    const list = searchState.selected;
    const at = list.indexOf(id);
    if (at >= 0) list.splice(at, 1);
    else {
      list.push(id);
      if (list.length > MAX_COMPARE_ENTITIES) {
        const dropped = list.shift();
        toast('At most ' + MAX_COMPARE_ENTITIES + ' entities can be compared: removed ' + dropped);
      }
    }
    document.querySelectorAll('#search-results input[data-cmp]').forEach((box) => { box.checked = list.includes(box.dataset.cmp); });
    const count = $('#tab-count-compare');
    if (count) count.textContent = list.length ? String(list.length) : '';
    if (searchState.refreshCompare) searchState.refreshCompare();
  }

  function entityCard(e) {
    const ev = e.evaluation || {};
    const fields = Object.entries(e.fields || {}).filter(([, v]) => fieldText(v)).slice(0, 40);
    const multi = (ev.corroborated_by || []).length > 1;
    return h('article', { class: 'entity' + (multi ? ' linked' : ''), id: /^e\d+$/.test(e.entity_id) ? 'ent-' + e.entity_id : null },
      h('div', { class: 'card-meta' },
        gradeBadge(ev),
        h('span', { class: 'badge outline', text: e.type || 'other' }),
        multi ? h('span', { class: 'badge accent', text: 'confirmed by ' + ev.corroborated_by.length + ' databases' }) : null,
        e.date ? h('span', { class: 'muted small', text: e.date }) : null,
        queryBadges(entityQueries(e.entity_id)),
        h('span', { class: 'card-meta-end' }, h('code', { class: 'small muted', text: e.entity_id }), compareToggle(e.entity_id))),
      h('div', { class: 'cluster-title', text: e.title || '(untitled)' }),
      e.authors && e.authors.length ? h('div', { class: 'muted small', text: clip(e.authors.slice(0, 6).join(', ') + (e.authors.length > 6 ? ' et al.' : ''), 200) }) : null,
      (ev.flags || []).length ? h('div', { class: 'flags' }, ev.flags.map((f) => h('span', { class: 'flag', text: f }))) : null,
      fields.length ? fieldTable(fields.slice(0, 8), e) : null,
      fields.length > 8 ? h('details', { class: 'small' }, h('summary', { text: 'More fields (' + (fields.length - 8) + ')' }), fieldTable(fields.slice(8), e)) : null,
      e.summary ? h('details', { class: 'entity-summary' }, h('summary', { class: 'small', text: 'Abstract / description' }), h('p', { text: e.summary })) : null,
      Object.keys(e.identifiers || {}).length ? h('div', { class: 'id-chips', 'aria-label': 'Identifiers' },
        Object.entries(e.identifiers).slice(0, 12).map(([k, v]) => h('span', { class: 'id-chip' }, h('b', { text: k + ':' }), String(v)))) : null,
      Object.keys(e.conflicts || {}).length ? h('details', { class: 'small' }, h('summary', { text: 'Sources disagree on ' + Object.keys(e.conflicts).join(', ') }),
        h('ul', null, Object.entries(e.conflicts).map(([k, opts]) => h('li', null, h('code', { text: k }), ': ',
          opts.map((o, i) => h('span', null, i ? '; ' : '', h('b', { text: String(o.value) }), ' (' + (o.sources || []).join(', ') + ')')))))) : null,
      h('div', { class: 'citations small', 'aria-label': 'Sources' }, 'Sources: ', (e.sources || []).map((s, i) => h('span', null, i ? ', ' : '',
        sourceBadge(s.source), ' ', s.url ? extLink(s.url, clip(s.id, 36)) : h('code', { text: clip(s.id, 36) })))));
  }

  /** Switch to the data tab and bring an entity card into view. */
  function showEntity(id) {
    if (!/^e\d+$/.test(String(id || ''))) return;
    if (searchState.showTab) searchState.showTab('data');
    if (!document.getElementById('ent-' + id) && searchState.showAll) searchState.showAll();
    const el = document.getElementById('ent-' + id);
    if (!el) return;
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    el.classList.add('flash');
    setTimeout(() => el.classList.remove('flash'), 1800);
  }
  function entityLink(id) {
    return /^e\d+$/.test(id) ? h('button', { type: 'button', class: 'id-link', title: 'Show in data', onclick: () => showEntity(id) }, id)
      : h('code', { class: 'small', text: id });
  }

  // ------------------------------------------------------------- results page
  function statusChip(s) {
    return h('span', { class: 'status-chip ' + (s.ok ? (s.returned ? 'ok' : 'zero') : 'err'), role: 'listitem', title: s.ok ? (s.returned ? '' : 'no matches') : (s.error || 'failed') },
      sourceBadge(s.source),
      s.ok ? fmtNum(s.returned) + (s.total !== null && s.total !== undefined ? ' / ' + fmtNum(s.total) : '') : 'failed',
      h('span', { class: 'lat', text: fmtMs(s.latency_ms) }));
  }

  function checksSummary(checks, okText) {
    const list = Array.isArray(checks) ? checks : [];
    if (!list.length) return null;
    const passed = list.filter((c) => c.ok).length;
    const ok = passed === list.length;
    return h('details', { class: 'checks ' + (ok ? 'ok' : 'bad') },
      h('summary', null, h('span', { class: 'badge ' + (ok ? 'ok' : 'err'), text: (ok ? '✓ ' : '✗ ') + 'Data checks ' + passed + '/' + list.length }),
        h('span', { class: 'muted small', text: ok ? ' ' + (okText || 'every chart number was re-added and matches the data') : ' some checks failed: see below' })),
      h('ul', { class: 'small' }, list.map((c) => h('li', { class: c.ok ? null : 'bad' }, h('b', { text: (c.ok ? '✓ ' : '✗ ') + c.name }), ' – ' + c.detail))));
  }

  function renderSearchResults(res) {
    searchState.res = res;
    searchState.selected = [];
    searchState.refreshCompare = null;
    const curated = res.curated || { entities: [], stats: {} };
    searchState.byId = new Map((curated.entities || []).map((e) => [e.entity_id, e]));
    const box = clear($('#search-results'));
    const queries = Array.isArray(res.queries) && res.queries.length ? res.queries : [res.query];
    const statuses = (res.sources || []).slice().sort((a, b) => (b.ok - a.ok) || a.source.localeCompare(b.source));
    const okCount = statuses.filter((s) => s.ok).length;
    const st = curated.stats || {};
    const grades = Object.entries(st.grades || {}).map(([g, n]) => g + ': ' + n).join(', ');
    box.appendChild(h('p', { class: 'muted', text:
      (queries.length > 1 ? queries.join(' vs ') + ': ' : '') +
      fmtNum((res.records || []).length) + ' records from ' + okCount + ' of ' + statuses.length + ' source searches, cleaned and merged into ' +
      fmtNum(st.entities_out || 0) + ' entities (' + fmtNum(st.duplicates_merged || 0) + ' duplicates merged, ' +
      fmtNum(st.multi_source_entities || 0) + ' confirmed by 2+ databases) · grades ' + (grades || '-') + ' · ' + fmtMs(res.elapsed_ms) }));
    if (queries.length > 1) {
      for (const q of queries) {
        box.appendChild(h('div', { class: 'status-row' }, h('span', { class: 'badge q-badge q' + (queryIndex(q) % 4), text: q }),
          h('div', { class: 'status-chips', role: 'list', 'aria-label': 'Per-source status for ' + q }, statuses.filter((s) => s.query === q).map(statusChip))));
      }
    } else {
      box.appendChild(h('div', { class: 'status-chips', role: 'list', 'aria-label': 'Per-source status' }, statuses.map(statusChip)));
    }
    const failed = statuses.filter((s) => !s.ok);
    if (failed.length) {
      box.appendChild(h('details', null, h('summary', { class: 'small muted', text: failed.length + ' source search(es) failed - details' }),
        h('ul', { class: 'small' }, failed.map((s) => h('li', null, h('b', { text: s.source + (s.query && queries.length > 1 ? ' (' + s.query + ')' : '') + ': ' }), s.error || 'failed')))));
    }
    const vis = res.visuals;
    if (vis) box.appendChild(checksSummary(vis.checks));
    if ((curated.entities || []).length) box.appendChild(exportPanel());
    box.appendChild(searchTabs(res));
  }

  function searchTabs(res) {
    const vis = res.visuals;
    const tabs = [
      ['data', '', 'Data', () => dataPanel(res)],
      vis ? ['mindmap', '', 'Mind map', () => mindMapPanel(res)] : null,
      vis ? ['charts', '', 'Charts', () => chartsPanel(res)] : null,
      ['body', '', 'Body map', () => bodyPanel(res)],
      ['compare', '', 'Compare', () => comparePanel(res)],
    ].filter(Boolean);
    const list = h('div', { class: 'tabs', role: 'tablist', 'aria-label': 'Search results' });
    const panels = h('div', { class: 'tab-panels' });
    const built = new Map();
    function show(key) {
      for (const [k] of tabs) {
        const btn = list.querySelector('[data-tab="' + k + '"]');
        const panel = panels.querySelector('[data-panel="' + k + '"]');
        const on = k === key;
        btn.setAttribute('aria-selected', String(on));
        btn.tabIndex = on ? 0 : -1;
        panel.hidden = !on;
        if (on && (!built.has(k) || k === 'compare')) {
          panel.replaceChildren(tabs.find((t) => t[0] === k)[3]());
          built.set(k, true);
        }
      }
    }
    for (const [key, , en] of tabs) {
      const btn = h('button', {
        type: 'button', class: 'tab', role: 'tab', id: 'tab-' + key, 'aria-controls': 'panel-' + key, dataset: { tab: key },
        onclick: () => show(key),
        onkeydown: (e) => {
          if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
          const i = tabs.findIndex((t) => t[0] === key);
          const next = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length][0];
          show(next);
          list.querySelector('[data-tab="' + next + '"]').focus();
        },
      }, en, key === 'compare' ? h('span', { class: 'tab-count', id: 'tab-count-compare' }) : null);
      list.appendChild(btn);
      panels.appendChild(h('div', { role: 'tabpanel', id: 'panel-' + key, 'aria-labelledby': 'tab-' + key, dataset: { panel: key }, hidden: true }));
    }
    searchState.showTab = show;
    show('data');
    return h('div', { class: 'results-tabs' }, list, panels);
  }

  function dataPanel(res) {
    const curated = res.curated || { entities: [], stats: {} };
    const st = curated.stats || {};
    const entities = curated.entities || [];
    const box = h('div');
    box.appendChild(h('div', { class: 'section-title' }, h('h2', { text: 'Data' }),
      h('span', { class: 'muted small', text: 'cleaned, de-duplicated across databases and quality-graded (hover a grade for how it was scored); tick Compare to put entities side by side' })));
    const grid = h('div', { class: 'entities' });
    const PAGE = 60;
    let shown = 0;
    const more = h('div', { class: 'show-more' });
    function renderUpTo(n) {
      for (; shown < Math.min(n, entities.length); shown++) grid.appendChild(entityCard(entities[shown]));
      more.replaceChildren(shown < entities.length ? h('button', { type: 'button', class: 'btn', onclick: () => renderUpTo(entities.length) },
        'Show all ' + entities.length + ' entities (' + (entities.length - shown) + ' more)') : '');
    }
    searchState.showAll = () => renderUpTo(entities.length);
    if (entities.length) {
      box.appendChild(grid);
      box.appendChild(more);
      renderUpTo(PAGE);
    } else {
      box.appendChild(h('div', { class: 'empty', text: 'No data matched. Try a more specific query (a drug, gene, DOI or trial).' }));
    }
    if ((st.cleaning || []).length) box.appendChild(h('p', { class: 'muted small', text: 'Cleaning applied: ' + st.cleaning.join('; ') }));
    const bySource = new Map();
    for (const r of res.records || []) {
      if (!bySource.has(r.source)) bySource.set(r.source, []);
      bySource.get(r.source).push(r);
    }
    if (bySource.size) {
      box.appendChild(h('details', { class: 'raw-records' },
        h('summary', null, h('h2', { class: 'inline', text: 'Raw records by source' })),
        Array.from(bySource, ([name, recs]) => h('details', { class: 'source-group' },
          h('summary', null, sourceBadge(name), h('span', { text: recs.length + ' record' + (recs.length === 1 ? '' : 's') })),
          h('ul', { class: 'record-list' }, recs.map(recordItem))))));
    }
    return box;
  }

  // ------------------------------------------------------------- mind map
  function walkMind(node, fn) {
    fn(node);
    for (const child of node.children || []) walkMind(child, fn);
  }
  function mindLabel(n) {
    if (n.kind === 'group') return n.label;
    if (n.kind === 'shared') return n.label;
    return n.label || '';
  }
  /** Approximate rendered text width (CJK glyphs are about one em wide). */
  function textWidth(s, size) {
    let w = 0;
    for (const ch of String(s || '')) w += ch.codePointAt(0) > 0x2e80 ? size : size * 0.56;
    return w;
  }
  function mindColor(n) {
    if (n.kind === 'root') return 'mm-root';
    if (n.kind === 'query') return 'mm-q' + (queryIndex(n.label) % 4);
    if (n.kind === 'shared') return 'mm-shared';
    return 'cat-' + (GROUP_CAT[n.group] || 'other');
  }

  /** Interactive mind map: query -> kinds of data -> best entities, collapsible, with entity details. */
  function mindMapView(vis, onPick) {
    const collapsed = new Set();
    walkMind(vis.mindmap, (n) => { if (vis.compare && n.kind === 'group') collapsed.add(n.id); });
    let selected = null;
    const holder = h('div', { class: 'mm-scroll' });

    function size(n) {
      const label = clip(mindLabel(n), n.kind === 'entity' ? 64 : 48);
      const sub = n.kind === 'entity' ? clip(n.detail || '', 70) : n.kind === 'group' ? '' : clip(n.detail || '', 60);
      const kids = (n.children || []).length > 0;
      const pad = n.kind === 'entity' ? 44 : 26;
      const w = Math.min(420, Math.max(96, Math.max(textWidth(label, 13), textWidth(sub, 11.5)) + pad + (kids && n.kind !== 'root' ? 34 : 0)));
      return { label, sub, w, hgt: sub ? 40 : 30, kids };
    }

    function draw() {
      const items = [];
      const colW = [];
      let y = 10;
      function place(n, d, parent) {
        const m = size(n);
        const kids = collapsed.has(n.id) ? [] : (n.children || []);
        const it = { n, d, parent, ...m };
        colW[d] = Math.max(colW[d] || 0, m.w);
        items.push(it);
        if (!kids.length) { it.y = y + m.hgt / 2; y += m.hgt + 10; } else {
          const placed = kids.map((k) => place(k, d + 1, it));
          it.y = (placed[0].y + placed[placed.length - 1].y) / 2;
        }
        return it;
      }
      place(vis.mindmap, 0, null);
      const xs = [8];
      for (let d = 1; d < colW.length; d++) xs[d] = xs[d - 1] + colW[d - 1] + 54;
      const W = xs[colW.length - 1] + colW[colW.length - 1] + 10;
      const H = Math.max(y, 60);
      const svg = svgEl('svg', { class: 'mm', viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, role: 'group', 'aria-label': 'Mind map of ' + vis.mindmap.label });
      const edges = svgEl('g', { class: 'mm-edges', 'aria-hidden': 'true' });
      const nodes = svgEl('g', { class: 'mm-nodes' });
      svg.append(edges, nodes);
      for (const it of items) {
        const x = xs[it.d];
        const n = it.n;
        if (it.parent) {
          const px = xs[it.parent.d] + it.parent.w;
          const mx = (px + x) / 2;
          edges.appendChild(svgEl('path', { class: 'mm-edge ' + mindColor(n.kind === 'entity' ? it.parent.n : n), d: 'M' + px + ',' + it.parent.y + ' C' + mx + ',' + it.parent.y + ' ' + mx + ',' + it.y + ' ' + x + ',' + it.y }));
        }
        const toggles = it.kids && n.kind !== 'root';
        const isOpen = !collapsed.has(n.id);
        const g = svgEl('g', {
          class: 'mm-node mm-' + n.kind + ' ' + mindColor(n) + (selected === n.id ? ' is-sel' : ''), transform: 'translate(' + x + ',' + (it.y - it.hgt / 2) + ')',
          tabindex: n.kind === 'root' ? null : 0, role: n.kind === 'root' ? 'img' : 'button',
          'aria-expanded': toggles ? String(isOpen) : null,
          'aria-label': mindLabel(n) + (n.count !== null && n.count !== undefined ? ', ' + n.count + ' entities' : '') + (n.detail ? ', ' + n.detail : ''),
        });
        g.appendChild(svgEl('rect', { class: 'mm-box', width: it.w, height: it.hgt, rx: n.kind === 'entity' ? 6 : it.hgt / 2 }));
        let tx = 12;
        if (n.kind === 'entity') {
          g.appendChild(svgEl('rect', { class: 'mm-grade grade-' + (n.grade || 'x'), x: 8, y: it.hgt / 2 - 9, width: 22, height: 18, rx: 9 }));
          g.appendChild(svgEl('text', { class: 'mm-grade-t', x: 19, y: it.hgt / 2 + 4, 'text-anchor': 'middle', text: n.grade || '?' }));
          tx = 38;
        }
        g.appendChild(svgEl('text', { class: 'mm-t', x: tx, y: it.sub ? 17 : it.hgt / 2 + 4.5, text: it.label }));
        if (it.sub) g.appendChild(svgEl('text', { class: 'mm-sub', x: tx, y: 32, text: it.sub }));
        if (toggles) {
          const cx = it.w - 16;
          if (n.count !== null && n.count !== undefined && n.kind === 'group') {
            g.appendChild(svgEl('text', { class: 'mm-count', x: cx - 12, y: it.hgt / 2 + 4.5, 'text-anchor': 'end', text: String(n.count) }));
          }
          g.appendChild(svgEl('text', { class: 'mm-caret', x: cx, y: it.hgt / 2 + 4.5, 'text-anchor': 'middle', text: isOpen ? '−' : '+' }));
        }
        const act = () => {
          if (n.kind === 'entity') { selected = n.id; hideTip(); onPick(n); rerender(); return; }
          if (!toggles) return;
          if (collapsed.has(n.id)) collapsed.delete(n.id); else collapsed.add(n.id);
          rerender();
          const again = holder.querySelector('[data-id="' + CSS.escape(n.id) + '"]');
          if (again) again.focus();
        };
        g.setAttribute('data-id', n.id);
        if (n.kind !== 'root') activate(g, act);
        if (n.kind === 'entity') {
          withTip(g, [n.label, (n.type || '') + (n.date ? ' · ' + n.date : '') + ' · grade ' + (n.grade || '?') + (n.score !== null && n.score !== undefined ? ' (' + n.score + ')' : ''),
            ...(n.facts || []).slice(0, 4).map((f) => f[0] + ': ' + f[1]), 'Click for details']);
        }
        nodes.appendChild(g);
      }
      return svg;
    }
    function rerender() { holder.replaceChildren(draw()); }
    rerender();
    return {
      el: holder,
      expandAll: () => { collapsed.clear(); rerender(); },
      collapseAll: () => { walkMind(vis.mindmap, (n) => { if (n.kind === 'group' || n.kind === 'shared') collapsed.add(n.id); }); rerender(); },
    };
  }

  function figuresOn(res) { return !!(res.figures && res.figures.available && SEARCH_ID_RE.test(res.search_id || '')); }
  function figUrl(res, name, fmt, download) {
    return '/api/search/' + res.search_id + '/figures/' + encodeURIComponent(name) + '.' + fmt + (download ? '?download=1' : '');
  }
  function figDownloads(res, name) {
    if (!figuresOn(res) || !(res.figures.names || []).includes(name)) return null;
    return h('span', { class: 'fig-dl' }, ['svg', 'png', 'pdf'].map((fmt) =>
      h('a', { class: 'btn btn-xs', href: figUrl(res, name, fmt, true), download: '', title: 'Figure Studio ' + fmt.toUpperCase() }, fmt.toUpperCase())));
  }
  function figureNote(res) {
    if (figuresOn(res)) return null;
    return h('p', { class: 'notice small' }, 'Journal-style figures are drawn by Figure Studio, which is not available: ',
      h('span', { text: (res.figures && res.figures.error) || 'unknown reason' }), '. The charts below are drawn in the browser from the same numbers.');
  }

  function entityDetail(n) {
    const e = searchState.byId.get(n.entity);
    if (!e) return h('p', { class: 'muted small', text: n.label });
    return h('div', { class: 'mm-detail panel' },
      h('div', { class: 'card-meta' }, gradeBadge(e.evaluation), h('span', { class: 'badge outline', text: e.type || 'other' }),
        e.date ? h('span', { class: 'muted small', text: e.date }) : null, queryBadges(entityQueries(e.entity_id)),
        h('span', { class: 'card-meta-end' }, h('code', { class: 'small muted', text: e.entity_id }))),
      h('h3', { class: 'mm-detail-title', text: e.title || n.label }),
      (n.facts || []).length ? h('table', { class: 'data kv' }, h('tbody', null, n.facts.map((f) => h('tr', null, h('th', { text: f[0] }), h('td', { text: f[1] }))))) : null,
      e.summary ? h('p', { class: 'small mm-detail-sum', text: clip(e.summary, 600) }) : null,
      h('div', { class: 'citations small' }, 'Sources: ', (e.sources || []).map((s, i) => h('span', null, i ? ', ' : '', sourceBadge(s.source), ' ',
        s.url ? extLink(s.url, clip(s.id, 36)) : h('code', { text: clip(s.id, 36) })))),
      h('div', { class: 'form-actions' },
        h('button', { type: 'button', class: 'btn btn-sm btn-primary', onclick: () => showEntity(e.entity_id) }, 'Show in data'),
        h('button', { type: 'button', class: 'btn btn-sm', onclick: () => { if (!searchState.selected.includes(e.entity_id)) toggleCompare(e.entity_id); searchState.showTab('compare'); } }, 'Compare')));
  }

  function mindMapPanel(res) {
    const vis = res.visuals;
    const detail = h('div', { class: 'mm-detail-slot' }, h('p', { class: 'muted small', text: 'Click an entry to see its data; click a branch to open or close it.' }));
    const view = mindMapView(vis, (n) => detail.replaceChildren(entityDetail(n)));
    const sid = SEARCH_ID_RE.test(res.search_id || '') ? res.search_id : null;
    return h('div', { class: 'mm-panel' },
      h('div', { class: 'panel-head row' },
        h('div', null, h('h2', null, 'Mind map'),
          h('p', { class: 'muted small', text: vis.compare
            ? 'Each query, the kinds of data it found, and what they share'
            : 'Kinds of data (entities) and the best-graded entries of each' })),
        h('div', { class: 'head-actions' },
          h('button', { type: 'button', class: 'btn btn-sm', onclick: view.expandAll }, 'Expand'),
          h('button', { type: 'button', class: 'btn btn-sm', onclick: view.collapseAll }, 'Collapse'),
          figDownloads(res, 'mindmap'),
          sid ? h('a', { class: 'btn btn-xs', href: '/api/search/' + sid + '/mindmap.json', download: '' }, 'JSON') : null)),
      figureNote(res),
      h('div', { class: 'panel mm-frame' }, view.el),
      detail);
  }

  // ------------------------------------------------------------- charts
  function seriesClass(chart, s, i) {
    if (/^Grade [ABCD]$/.test(s.name)) return 'vz-grade-' + s.name.slice(-1);
    const q = ((searchState.res && searchState.res.queries) || []).indexOf(s.name);
    return 'vz-s' + ((q >= 0 ? q : i) % 6);
  }

  /** Same numbers as the figure, drawn in the browser (used when Figure Studio is unavailable). */
  function htmlChart(chart) {
    const series = chart.series || [];
    const cats = chart.categories || [];
    if (chart.kind === 'heatmap') {
      const max = Math.max(1, ...series.flatMap((s) => s.values));
      return h('div', { class: 'table-wrap' }, h('table', { class: 'data vz-heat' },
        h('thead', null, h('tr', null, h('th', { text: '' }), cats.map((c) => h('th', { class: 'num', text: c })))),
        h('tbody', null, series.map((s) => h('tr', null, h('th', { text: s.name }), s.values.map((v) => {
          const td = h('td', { class: 'num', text: v ? fmtNum(v) : '' });
          td.style.setProperty('--heat', String(v / max));
          return td;
        }))))));
    }
    const stacked = chart.kind === 'stacked';
    const totals = cats.map((_, i) => series.reduce((a, s) => a + (s.values[i] || 0), 0));
    const max = Math.max(1, ...(stacked ? totals : series.flatMap((s) => s.values)));
    return h('div', { class: 'vz-bars' },
      series.length > 1 ? h('div', { class: 'vz-legend small' }, series.map((s, i) => h('span', null, h('i', { class: 'vz-sw ' + seriesClass(chart, s, i) }), s.name))) : null,
      cats.map((c, ci) => h('div', { class: 'vz-row' }, h('span', { class: 'vz-cat', text: c }),
        stacked
          ? h('span', { class: 'vz-stack' }, series.map((s, i) => {
            const bar = h('i', { class: 'vz-bar ' + seriesClass(chart, s, i), title: s.name + ': ' + s.values[ci] });
            bar.style.width = (100 * (s.values[ci] || 0) / max) + '%';
            return bar;
          }), h('b', { class: 'vz-val', text: fmtNum(totals[ci]) }))
          : h('span', { class: 'vz-group' }, series.map((s, i) => {
            const bar = h('i', { class: 'vz-bar ' + seriesClass(chart, s, i), title: s.name + ': ' + s.values[ci] });
            bar.style.width = (100 * (s.values[ci] || 0) / max) + '%';
            return h('span', { class: 'vz-line' }, bar, h('b', { class: 'vz-val', text: fmtNum(s.values[ci] || 0) }));
          })))));
  }

  function numbersTable(chart) {
    const rows = [];
    for (const s of chart.series || []) {
      (chart.categories || []).forEach((c, i) => {
        const counted = (chart.members || {})[c + '|' + s.name] || [];
        if (!s.values[i] && !counted.length) return;
        rows.push([c, s.name, fmtNum(s.values[i]), h('span', { class: 'counted' }, counted.slice(0, 14).map((id) => entityLink(id)),
          counted.length > 14 ? h('span', { class: 'muted small', text: ' +' + (counted.length - 14) + ' more (figure_data.csv)' }) : null)]);
      });
    }
    return miniTable(['Category', 'Series', 'Value', 'What was counted'], rows, [2]);
  }

  function chartCard(res, chart) {
    let fig;
    if (figuresOn(res) && (res.figures.names || []).includes(chart.id)) {
      const img = h('img', { class: 'vz-img', src: figUrl(res, chart.id, 'svg'), alt: chart.title + ' (drawn by Figure Studio from the numbers below)', loading: 'lazy' });
      img.addEventListener('error', () => img.replaceWith(htmlChart(chart)), { once: true });
      fig = h('div', { class: 'vz-paper' }, img);
    } else {
      fig = htmlChart(chart);
    }
    return h('section', { class: 'panel vz-card', id: 'chart-' + chart.id },
      h('div', { class: 'panel-head row' },
        h('div', null, h('h3', { text: chart.title }),
          h('p', { class: 'muted small vz-def', text: chart.definition })),
        figDownloads(res, chart.id)),
      fig,
      h('details', { class: 'vz-data' }, h('summary', { class: 'small', text: 'The numbers and what each one counted' }), numbersTable(chart)));
  }

  function chartsPanel(res) {
    const vis = res.visuals;
    const sid = SEARCH_ID_RE.test(res.search_id || '') ? res.search_id : null;
    const box = h('div', { class: 'vz' },
      h('div', { class: 'panel-head row' },
        h('div', null, h('h2', null, 'Charts'),
          h('p', { class: 'muted small', text: 'Every number is counted from the returned records; figures are drawn by Figure Studio.' })),
        h('div', { class: 'head-actions' }, sid ? h('a', { class: 'btn btn-sm', href: '/api/search/' + sid + '/figure_data.csv', download: '' }, 'figure_data.csv') : null)),
      figureNote(res));
    if (figuresOn(res) && (res.figures.names || []).includes('overview')) {
      const img = h('img', { class: 'vz-img', src: figUrl(res, 'overview', 'svg'), alt: 'Overview figure, panels a-d', loading: 'lazy' });
      img.addEventListener('error', () => img.closest('.vz-card') && img.closest('.vz-card').remove(), { once: true });
      box.appendChild(h('section', { class: 'panel vz-card vz-overview' },
        h('div', { class: 'panel-head row' }, h('h3', null, 'Overview figure (a–d), Nature double-column width'), figDownloads(res, 'overview')),
        h('div', { class: 'vz-paper' }, img)));
    }
    const grid = h('div', { class: 'vz-grid' });
    for (const chart of vis.charts || []) grid.appendChild(chartCard(res, chart));
    box.appendChild(grid);
    return box;
  }

  // ------------------------------------------------------------- compare
  /** Queries side by side, read from the chart numbers (so the table and the figures always agree). */
  function queryComparison(vis) {
    const queries = vis.queries || [];
    const sections = [];
    const byId = new Map((vis.charts || []).map((c) => [c.id, c]));
    const sum = (arr) => arr.reduce((a, b) => a + b, 0);
    const types = byId.get('entities_by_type');
    const recs = byId.get('records_by_source');
    const overview = [];
    if (types) overview.push(['Entities found', queries.map((q) => sum((types.series.find((s) => s.name === q) || { values: [] }).values))]);
    if (recs) {
      overview.push(['Records returned', queries.map((q) => sum((recs.series.find((s) => s.name === q) || { values: [] }).values))]);
    }
    const overlap = byId.get('overlap');
    if (overlap) {
      const shared = overlap.series[0].values[overlap.categories.length - 1];
      overview.push(['Shared with another query', queries.map(() => shared)]);
      overview.push(['Found only by this query', queries.map((q) => overlap.series[0].values[overlap.categories.indexOf('Only ' + q)] || 0)]);
    }
    sections.push(['Overview', overview]);
    for (const id of ['entities_by_type', 'trial_phases', 'trial_status']) {
      const c = byId.get(id);
      if (!c) continue;
      sections.push([c.title, c.categories.map((cat, i) => [cat, queries.map((q) => {
        const s = c.series.find((x) => x.name === q);
        return s ? s.values[i] : 0;
      })])]);
    }
    const grades = byId.get('quality_grades');
    if (grades) {
      sections.push([grades.title, grades.series.map((s) => [s.name, queries.map((q) => s.values[grades.categories.indexOf(q)] || 0)])]);
    }
    const table = h('table', { class: 'data cmp-q' },
      h('thead', null, h('tr', null, h('th', { text: '' }), queries.map((q) => h('th', { class: 'num' }, h('span', { class: 'badge q-badge q' + (queryIndex(q) % 4), text: q }))))),
      sections.map(([title, rows]) => h('tbody', null,
        h('tr', { class: 'cmp-sec' }, h('th', { colspan: queries.length + 1, text: title })),
        rows.map(([label, values]) => {
          const top = Math.max(...values);
          return h('tr', null, h('th', { text: label }), values.map((v) => h('td', { class: 'num' + (v === top && v > 0 && values.filter((x) => x === top).length < values.length ? ' best' : ''), text: fmtNum(v) })));
        }))));
    return h('section', { class: 'panel' },
      h('div', { class: 'panel-head' }, h('h3', null, 'Queries side by side'),
        h('p', { class: 'muted small', text: 'Same counts as the charts (bold = highest).' })),
      h('div', { class: 'table-wrap' }, table));
  }

  function entityComparison() {
    const ids = searchState.selected.filter((id) => searchState.byId.has(id));
    const holder = h('section', { class: 'panel' });
    const onlyDiff = h('input', { type: 'checkbox', id: 'cmp-diff' });
    function cellText(v) { return clip(fieldText(v), 300); }
    function render() {
      const ents = ids.map((id) => searchState.byId.get(id));
      const head = h('div', { class: 'panel-head row' },
        h('div', null, h('h3', null, 'Entities side by side'),
          h('p', { class: 'muted small', text: 'Tick Compare on entity cards or in the mind map (up to ' + MAX_COMPARE_ENTITIES + ').' })),
        ents.length > 1 ? h('label', { class: 'small field-check' }, onlyDiff, 'Only differences') : null);
      if (!ents.length) { holder.replaceChildren(head, h('div', { class: 'empty', text: 'No entities chosen yet.' })); return; }
      const rows = [
        ['Type', ents.map((e) => e.type || '')],
        ['Date', ents.map((e) => e.date || '')],
        ['Quality', ents.map((e) => (e.evaluation || {}).grade + ' (' + (e.evaluation || {}).score + ')')],
        ['Confirmed by', ents.map((e) => ((e.evaluation || {}).corroborated_by || []).join(', '))],
        ['Flags', ents.map((e) => ((e.evaluation || {}).flags || []).join('; '))],
      ];
      if (searchState.res.visuals && searchState.res.visuals.compare) rows.push(['Found by', ents.map((e) => entityQueries(e.entity_id).join(', '))]);
      const idKeys = [...new Set(ents.flatMap((e) => Object.keys(e.identifiers || {})))];
      for (const k of idKeys) rows.push(['ID ' + k, ents.map((e) => (e.identifiers || {})[k] || '')]);
      const fieldCount = new Map();
      for (const e of ents) for (const [k, v] of Object.entries(e.fields || {})) if (fieldText(v)) fieldCount.set(k, (fieldCount.get(k) || 0) + 1);
      const fieldKeys = [...fieldCount.keys()].sort((a, b) => fieldCount.get(b) - fieldCount.get(a) || a.localeCompare(b)).slice(0, 60);
      for (const k of fieldKeys) rows.push([humanKey(k), ents.map((e) => cellText((e.fields || {})[k]))]);
      rows.push(['Sources', ents.map((e) => (e.sources || []).map((s) => s.source + ':' + s.id).join('; '))]);
      const body = h('tbody');
      for (const [label, values] of rows) {
        const filled = values.filter((v) => v !== '' && v !== null && v !== undefined);
        const same = filled.length === values.length && new Set(values.map(String)).size === 1;
        if (!filled.length) continue;
        body.appendChild(h('tr', { class: same ? 'same' : 'diff' }, h('th', { text: label }), values.map((v) => h('td', { text: String(v || '–') }))));
      }
      const table = h('table', { class: 'data cmp-e' + (onlyDiff.checked ? ' only-diff' : '') },
        h('thead', null, h('tr', null, h('th', { text: '' }), ents.map((e) => h('th', null,
          h('div', { class: 'cmp-head' }, gradeBadge(e.evaluation), entityLink(e.entity_id),
            h('button', { type: 'button', class: 'btn-link small', title: 'Remove', onclick: () => toggleCompare(e.entity_id) }, '×')),
          h('div', { class: 'cmp-title', text: clip(e.title || e.entity_id, 140) }))))),
        body);
      onlyDiff.onchange = () => table.classList.toggle('only-diff', onlyDiff.checked);
      holder.replaceChildren(head, h('div', { class: 'table-wrap' }, table));
    }
    render();
    return holder;
  }

  function comparePanel(res) {
    const vis = res.visuals;
    const box = h('div', { class: 'cmp' });
    box.appendChild(h('div', { class: 'panel-head' }, h('h2', null, 'Compare'),
      vis && !vis.compare ? h('p', { class: 'muted small', text: 'To compare searches, enter more terms in “Compare with” under the search box.' }) : null));
    if (vis && vis.compare) box.appendChild(queryComparison(vis));
    const slot = h('div');
    const fill = () => slot.replaceChildren(entityComparison());
    fill();
    searchState.refreshCompare = () => { if (slot.isConnected) fill(); };
    box.appendChild(slot);
    return box;
  }

  // ------------------------------------------------------------- body map
  // Where a drug / compound / disease acts in the body (POST /api/body): key facts, an interactive
  // body drawn from the organ layout, the evidence per organ, and Figure Studio's illustrated figure.
  const BODY_LEVELS = {
    primary: 'Approved use / disease site', safety: 'Adverse effects reported',
    investigational: 'In clinical trials', expression: 'Target / gene expressed',
  };
  const BODY_KINDS = {
    treats: 'Approved indication', studied: 'Tested in trials for', disease: 'Disease site', phenotype: 'Clinical feature (HPO)',
    adverse_event: 'Adverse event reports (FAERS)', label: 'FDA label warning', toxicity: 'Toxicity',
    expression: 'Target / gene expression (GTEx)',
  };
  const BODY_TYPES = { drug: 'Drug', compound: 'Chemical compound', disease: 'Disease' };

  /** Catmull-Rom spline through points as an SVG path (the same curve Figure Studio draws). */
  function smoothPath(pts, closed) {
    const n = pts.length;
    if (n < 2) return '';
    const p = (i) => pts[closed ? (i + n) % n : Math.max(0, Math.min(n - 1, i))];
    let d = 'M' + pts[0][0].toFixed(1) + ',' + pts[0][1].toFixed(1);
    for (let i = 0; i < (closed ? n : n - 1); i++) {
      const p0 = p(i - 1); const p1 = p(i); const p2 = p(i + 1); const p3 = p(i + 2);
      const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6];
      const c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
      d += ' C' + c1.map((v) => v.toFixed(1)).join(',') + ' ' + c2.map((v) => v.toFixed(1)).join(',') + ' ' + p2.map((v) => v.toFixed(1)).join(',');
    }
    return d + (closed ? ' Z' : '');
  }

  /**
   * Interactive body map built from Figure Studio's illustrations: one picture per organ and per body
   * system (skeleton, vessels, nerves), coloured by evidence. Zoom (wheel, pinch, buttons, double-click,
   * + / -), pan (drag, arrow keys), hover and click organs (the view flies to them), filter evidence
   * levels, labels, all organs, natural colours, female / male organs, organ search and full screen.
   * Without Figure Studio the organs are drawn as plain shapes from the same layout.
   */
  function bodyViewer(bm, onPick) {
    const L = bm.layout || { organs: [] };
    const art = L.art && (bm.figures || {}).available ? L.art : null;
    const found = new Map((bm.organs || []).map((f) => [f.key, f]));
    const organsByKey = new Map((L.organs || []).map((o) => [o.key, o]));
    const K = 500;                                     // svg units per body unit (the body is 1000 tall)
    const BASE = { x: -660, y: -545, w: 1320, h: 1090 };
    const MAX_ZOOM = 14;
    const COL_W = 360;                                 // label column width (svg units)
    const st = { levels: new Set(Object.keys(BODY_LEVELS)), labels: true, systems: true, all: false, natural: false, sex: 'auto', sel: null, vb: Object.assign({}, BASE) };
    const P = (u, v) => [u * K, -v * K];
    const nodes = new Map();
    const hits = new Map();
    const labels = new Map();
    let anim = 0;

    const svg = svgEl('svg', { class: 'bv-svg', viewBox: [BASE.x, BASE.y, BASE.w, BASE.h].join(' '), role: 'application', tabindex: 0,
      'aria-roledescription': 'zoomable body map', 'aria-label': 'Body map of ' + bm.name +
        '. Scroll or press + and - to zoom, drag or use the arrow keys to move, Tab to the organs, Enter to open one.' });
    const gBody = svgEl('g', { class: 'bv-body-layer' });
    const gArt = svgEl('g', { class: 'bv-art' });
    const gHit = svgEl('g', { class: 'bv-hits' });
    const gLab = svgEl('g', { class: 'bv-labels' });
    svg.append(gBody, gArt, gHit, gLab);

    const artUrl = (glyph, tone) => '/api/body/art/' + encodeURIComponent(glyph) + '.svg?tone=' + tone + '&v=' + encodeURIComponent(art.version);
    const toneOf = (key) => {
      const f = found.get(key);
      return f && st.levels.has(f.level) ? f.level : (st.natural ? 'natural' : 'none');
    };
    const visible = (o) => {
      if (o.system && !st.systems && o.key !== st.sel) return false;
      if (o.sex) return st.sex === 'auto' ? found.has(o.key) : st.sex === o.sex;
      return found.has(o.key) || o.always || st.all;
    };
    const geom = (o) => {
      const H = o.size * K;
      return { H, W: ((art && art.aspects && art.aspects[o.glyph]) || o.aspect || 1) * H };
    };
    const anchorOf = (o) => P.apply(null, o.anchor || [o.u, o.v]);

    function picture(glyph, tone, x, y, W, H, flip, rotation) {
      const pad = art.pad * H;
      const el = svgEl('image', { href: artUrl(glyph, tone), x: x - W / 2 - pad, y: y - H / 2 - pad, width: W + 2 * pad,
        height: H + 2 * pad, preserveAspectRatio: 'none' });
      const t = [];
      if (rotation) t.push('rotate(' + (-rotation) + ' ' + x + ' ' + y + ')');
      if (flip) t.push('translate(' + (2 * x) + ' 0) scale(-1 1)');
      if (t.length) el.setAttribute('transform', t.join(' '));
      return el;
    }
    function plainShape(o, x, y, W, H) {
      if (o.system) return null;
      if (o.key === 'colon') {
        return svgEl('path', { class: 'bm-frame', 'stroke-width': (W * 0.14).toFixed(1),
          d: 'M' + (x - W * 0.41) + ',' + (y + H * 0.45) + ' V' + (y - H * 0.36) + ' H' + (x + W * 0.41) + ' V' + (y + H * 0.3) + ' L' + (x + 0.03 * W) + ',' + (y + H * 0.42) });
      }
      if (o.key === 'lung') {
        return svgEl('g', null, [-1, 1].map((s) => svgEl('ellipse', { class: 'bm-shape', cx: x + s * W * 0.24, cy: y + H * 0.06, rx: W * 0.22, ry: H * 0.44 })));
      }
      return svgEl('ellipse', { class: 'bm-shape', cx: x, cy: y, rx: W / 2, ry: H / 2 });
    }
    function hitShapes(o, x, y, W, H) {
      if (o.key === 'colon') {
        return svgEl('path', { class: 'bv-hit-stroke', 'stroke-width': (W * 0.2).toFixed(1),
          d: 'M' + (x - W * 0.41) + ',' + (y + H * 0.45) + ' V' + (y - H * 0.36) + ' H' + (x + W * 0.41) + ' V' + (y + H * 0.3) });
      }
      if (o.key === 'lung') return svgEl('g', null, [-1, 1].map((s) => svgEl('ellipse', { cx: x + s * W * 0.24, cy: y + H * 0.04, rx: W * 0.24, ry: H * 0.42 })));
      return svgEl('ellipse', { cx: x, cy: y, rx: Math.max(10, W * 0.42), ry: Math.max(10, H * 0.42) });
    }

    function tipLines(o) {
      const f = found.get(o.key);
      if (!f) return [o.en, 'No evidence found'];
      return [o.en, BODY_LEVELS[f.level], f.summary,
        Object.entries(f.kinds || {}).map(([kk, n]) => (BODY_KINDS[kk] || kk) + ' ' + n).join(' · '), 'Click to zoom in and see the evidence'];
    }
    function hover(key, on) {
      const n = nodes.get(key);
      if (n) n.classList.toggle('is-hover', on);
      const lab = labels.get(key);
      if (lab) lab.classList.toggle('is-hover', on);
    }

    function draw() {
      for (const g of [gBody, gArt, gHit, gLab]) g.replaceChildren();
      nodes.clear();
      hits.clear();
      labels.clear();
      if (art) {
        const bodyH = 2 * K;
        gBody.appendChild(picture('human_body', st.natural ? 'natural' : 'none', 0, 0, ((art.aspects || {}).human_body || 0.33) * bodyH, bodyH, false, 0));
      } else {
        gBody.appendChild(svgEl('path', { class: 'bm-body', d: smoothPath((L.outline || []).map(([u, v]) => P(u, v)), true) }));
        if (L.head) {
          const [hu, hv, ru, rv] = L.head;
          const [x, y] = P(hu, hv);
          gBody.appendChild(svgEl('ellipse', { class: 'bm-body', cx: x, cy: y, rx: ru * K, ry: rv * K }));
        }
      }
      const shown = (L.organs || []).filter(visible).sort((a, b) => b.size - a.size);
      for (const o of shown) {
        const f = found.get(o.key);
        const tone = toneOf(o.key);
        const level = BODY_LEVELS[tone] ? tone : 'none';
        const { H, W } = geom(o);
        const copies = [[o.u, o.v, false]].concat(o.mirror ? [[-o.u, o.v, true]] : []);
        const g = svgEl('g', { class: 'bv-o lv-' + level + (o.system ? ' is-system' : '') + (f ? ' has-ev' : ''), 'data-organ': o.key });
        const hit = svgEl('g', { class: 'bv-hit' + (f ? ' has-ev' : ''), 'data-organ': o.key, tabindex: 0, role: 'button',
          'aria-label': o.en + (f ? ': ' + BODY_LEVELS[f.level] + '. ' + f.summary : ': no evidence') });
        for (const [u, v, flip] of copies) {
          const [x, y] = P(u, v);
          const pic = art ? picture(o.glyph, tone, x, y, W, H, flip, o.rotation) : plainShape(o, x, y, W, H);
          if (pic) g.appendChild(pic);
          if (!o.system) hit.appendChild(hitShapes(o, x, y, W, H));
        }
        if (o.system) {  // a whole-body system: grab it at its anchor (arm artery, femur, leg nerve)
          const [ax, ay] = anchorOf(o);
          hit.appendChild(svgEl('circle', { cx: ax, cy: ay, r: 24 }));
          if (f) g.appendChild(svgEl('circle', { class: 'bv-anchor', cx: ax, cy: ay, r: 9, 'data-r': 9 }));
        }
        withTip(hit, tipLines(o));
        hit.addEventListener('mouseenter', () => hover(o.key, true));
        hit.addEventListener('mouseleave', () => hover(o.key, false));
        hit.addEventListener('focus', () => hover(o.key, true));
        hit.addEventListener('blur', () => hover(o.key, false));
        hit.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); select(o.key, { fly: true }); }
        });
        gArt.appendChild(g);
        gHit.appendChild(hit);
        nodes.set(o.key, g);
        hits.set(o.key, hit);
      }
      drawLabels(shown);
      applySelection();
      fitText();
    }

    function drawLabels(shown) {
      if (!st.labels) return;
      const rows = { left: [], right: [] };
      for (const o of shown) {
        const f = found.get(o.key);
        if (!f || !st.levels.has(f.level)) continue;
        const [ax, ay] = anchorOf(o);
        const u = (o.anchor || [o.u])[0];
        const side = u < 0 ? 'left' : u > 0 ? 'right' : (rows.left.length <= rows.right.length ? 'left' : 'right');
        const tx = o.mirror ? (side === 'left' ? -Math.abs(ax) : Math.abs(ax)) : ax;
        rows[side].push({ o, f, ax: tx, ay, y: ay });
      }
      const LINE = 62;
      for (const side of ['left', 'right']) {
        const list = rows[side].sort((a, b) => a.y - b.y);
        for (let i = 1; i < list.length; i++) list[i].y = Math.max(list[i].y, list[i - 1].y + LINE);
        const hi = BASE.y + BASE.h - 40;
        const over = list.length ? list[list.length - 1].y - hi : 0;
        if (over > 0) {
          list[list.length - 1].y -= over;
          for (let i = list.length - 2; i >= 0; i--) list[i].y = Math.min(list[i].y, list[i + 1].y - LINE);
        }
        const left = side === 'left';
        const tx = left ? BASE.x + 16 : BASE.x + BASE.w - 16;
        const edge = left ? BASE.x + 16 + COL_W : BASE.x + BASE.w - 16 - COL_W;
        for (const r of list) {
          const y = Math.max(BASE.y + 30, r.y);
          const g = svgEl('g', { class: 'bv-label lv-' + r.f.level, 'data-organ': r.o.key });
          g.appendChild(svgEl('path', { class: 'bv-leader', d: 'M' + edge + ',' + (y - 6) + ' L' + r.ax + ',' + r.ay }));
          g.appendChild(svgEl('circle', { class: 'bv-leader-dot', cx: r.ax, cy: r.ay, r: 4, 'data-r': 4 }));
          const anchor = left ? 'start' : 'end';
          g.appendChild(svgEl('text', { class: 'bv-name', x: tx, y: y - 4, 'text-anchor': anchor, 'data-size': 27, text: r.o.en }));
          g.appendChild(svgEl('text', { class: 'bv-detail', x: tx, y: y + 22, 'text-anchor': anchor, 'data-size': 20,
            text: clip((r.f.summary || '').split(' · ')[0], 46) }));
          g.addEventListener('mouseenter', () => hover(r.o.key, true));
          g.addEventListener('mouseleave', () => hover(r.o.key, false));
          labels.set(r.o.key, g);
          gLab.appendChild(g);
        }
      }
    }

    // ---- view: zoom and pan
    const zoom = () => BASE.w / st.vb.w;
    function clampView(v) {
      const w = Math.min(BASE.w, Math.max(BASE.w / MAX_ZOOM, v.w));
      const h = w * BASE.h / BASE.w;
      const cx = Math.min(BASE.x + BASE.w, Math.max(BASE.x, v.x + v.w / 2));
      const cy = Math.min(BASE.y + BASE.h, Math.max(BASE.y, v.y + v.h / 2));
      return { x: cx - w / 2, y: cy - h / 2, w, h };
    }
    function setView(v) {
      st.vb = clampView(v);
      svg.setAttribute('viewBox', [st.vb.x, st.vb.y, st.vb.w, st.vb.h].map((n) => n.toFixed(1)).join(' '));
      zoomOut.textContent = Math.round(zoom() * 100) + '%';
      fitText();
    }
    function fitText() {  // labels keep their size on screen at any zoom
      const z = zoom();
      for (const t of gLab.querySelectorAll('text')) t.setAttribute('font-size', (Number(t.getAttribute('data-size')) / Math.pow(z, 0.85)).toFixed(2));
      for (const c of svg.querySelectorAll('circle[data-r]')) c.setAttribute('r', (Number(c.getAttribute('data-r')) / Math.pow(z, 0.85)).toFixed(2));
      svg.style.setProperty('--bv-z', String(z));
      svg.classList.toggle('is-zoomed', z > 1.8);
    }
    function animateTo(target, ms) {
      cancelAnimationFrame(anim);
      const from = Object.assign({}, st.vb);
      const to = clampView(target);
      const t0 = performance.now();
      const dur = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : (ms || 380);
      const step = (now) => {
        const t = dur ? Math.min(1, (now - t0) / dur) : 1;
        const e = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
        setView({ x: from.x + (to.x - from.x) * e, y: from.y + (to.y - from.y) * e, w: from.w + (to.w - from.w) * e, h: from.h + (to.h - from.h) * e });
        if (t < 1) anim = requestAnimationFrame(step);
      };
      anim = requestAnimationFrame(step);
    }
    function zoomAt(factor, px, py, smooth) {
      const v = st.vb;
      const w = Math.min(BASE.w, Math.max(BASE.w / MAX_ZOOM, v.w / factor));
      const k = w / v.w;
      const target = { x: px - (px - v.x) * k, y: py - (py - v.y) * k, w, h: v.h * k };
      if (smooth) animateTo(target, 220); else setView(target);
    }
    const centre = () => [st.vb.x + st.vb.w / 2, st.vb.y + st.vb.h / 2];
    function toSvg(clientX, clientY) {
      const m = svg.getScreenCTM();
      if (!m) return centre();
      const p = new DOMPoint(clientX, clientY).matrixTransform(m.inverse());
      return [p.x, p.y];
    }
    function focusRect(key) {
      const o = organsByKey.get(key);
      if (!o) return BASE;
      let cx; let cy; let w; let h;
      if (o.system) {
        [cx, cy] = anchorOf(o);
        w = 340; h = 340;
      } else {
        const g = geom(o);
        [cx, cy] = P(o.mirror ? 0 : o.u, o.v);
        w = (o.mirror ? 2 * Math.abs(o.u) * K : 0) + g.W;
        h = g.H;
      }
      const span = Math.max(w * 2.6, h * 2.6 * BASE.w / BASE.h, BASE.w / MAX_ZOOM * 1.5);
      const hh = span * BASE.h / BASE.w;
      return { x: cx - span / 2, y: cy - hh / 2, w: span, h: hh };
    }

    function applySelection() {
      for (const [kk, n] of nodes) n.classList.toggle('is-sel', kk === st.sel);
      for (const [kk, n] of labels) n.classList.toggle('is-sel', kk === st.sel);
      svg.classList.toggle('has-sel', !!st.sel);
      const o = st.sel ? organsByKey.get(st.sel) : null;
      const f = st.sel ? found.get(st.sel) : null;
      if (!o) {
        info.replaceChildren(h('span', { class: 'muted', text: 'Scroll or pinch to zoom, drag to move, click an organ. Keys: + − 0, arrows, N / P.' }));
        return;
      }
      info.replaceChildren(
        h('div', { class: 'bv-info-head' }, h('span', { class: 'bm-dot lv-' + (f ? f.level : 'none') }), h('b', { text: o.en }),
          h('button', { type: 'button', class: 'btn-link', 'aria-label': 'Close', onclick: () => select(null, { fly: true }) }, '✕')),
        f ? h('div', { class: 'small' }, h('b', { text: BODY_LEVELS[f.level] })) : h('div', { class: 'small muted', text: 'No evidence found' }),
        f ? h('div', { class: 'small', text: f.summary }) : null,
        f ? h('div', { class: 'small muted', text: Object.entries(f.kinds || {}).map(([kk, n]) => (BODY_KINDS[kk] || kk) + ' ' + n).join(' · ') }) : null,
        h('div', { class: 'bv-info-actions' },
          h('button', { type: 'button', class: 'btn btn-xs', onclick: () => animateTo(focusRect(o.key)) }, 'Zoom to'),
          f ? h('button', { type: 'button', class: 'btn btn-xs', onclick: () => onPick(o.key, true) }, 'Evidence ↓') : null));
    }
    function select(key, opts) {
      opts = opts || {};
      hideTip();
      st.sel = key && organsByKey.has(key) ? key : null;
      const o = st.sel ? organsByKey.get(st.sel) : null;
      if (!st.systems || (o && !visible(o))) draw(); else applySelection();  // a hidden system or organ shows while selected
      if (opts.fly) animateTo(st.sel ? focusRect(st.sel) : BASE);
      if (opts.notify !== false && st.sel) onPick(st.sel, false);
    }
    function cycle(dir) {
      const keys = (bm.organs || []).map((f) => f.key).filter((kk) => organsByKey.has(kk) && hits.has(kk));
      if (!keys.length) return;
      const i = keys.indexOf(st.sel);
      select(keys[(i + dir + keys.length) % keys.length], { fly: true });
    }

    // ---- pointer, wheel and keyboard
    const pointers = new Map();
    let gesture = null;
    svg.addEventListener('wheel', (e) => {
      e.preventDefault();
      const [px, py] = toSvg(e.clientX, e.clientY);
      cancelAnimationFrame(anim);
      zoomAt(Math.exp(-e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0015)), px, py, false);
    }, { passive: false });
    svg.addEventListener('pointerdown', (e) => {
      if (e.button !== 0 && e.pointerType === 'mouse') return;
      try { svg.setPointerCapture(e.pointerId); } catch (_) { /* synthetic or finished pointer */ }
      pointers.set(e.pointerId, [e.clientX, e.clientY]);
      cancelAnimationFrame(anim);
      const pts = Array.from(pointers.values());
      gesture = { view: Object.assign({}, st.vb), pts, target: e.target, moved: false, scale: (svg.getScreenCTM() || { a: 1 }).a };
      svg.classList.add('is-dragging');
    });
    svg.addEventListener('pointermove', (e) => {
      if (!gesture || !pointers.has(e.pointerId)) return;
      pointers.set(e.pointerId, [e.clientX, e.clientY]);
      const pts = Array.from(pointers.values());
      if (pts.length === 1 && gesture.pts.length === 1) {
        const dx = pts[0][0] - gesture.pts[0][0];
        const dy = pts[0][1] - gesture.pts[0][1];
        if (Math.abs(dx) + Math.abs(dy) > 4) gesture.moved = true;
        if (gesture.moved) {
          hideTip();
          setView({ x: gesture.view.x - dx / gesture.scale, y: gesture.view.y - dy / gesture.scale, w: gesture.view.w, h: gesture.view.h });
        }
      } else if (pts.length === 2) {
        if (gesture.pts.length !== 2) gesture = { view: Object.assign({}, st.vb), pts, target: null, moved: true, scale: gesture.scale };
        const d0 = Math.hypot(gesture.pts[0][0] - gesture.pts[1][0], gesture.pts[0][1] - gesture.pts[1][1]) || 1;
        const d1 = Math.hypot(pts[0][0] - pts[1][0], pts[0][1] - pts[1][1]) || 1;
        const mid = toSvg((pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2);
        setView(gesture.view);
        zoomAt(d1 / d0, mid[0], mid[1], false);
      }
    });
    const endPointer = (e) => {
      if (!pointers.has(e.pointerId)) return;
      pointers.delete(e.pointerId);
      if (pointers.size) return;
      svg.classList.remove('is-dragging');
      const g = gesture;
      gesture = null;
      if (!g || g.moved || e.type === 'pointercancel') return;
      const el = g.target && g.target.closest ? g.target.closest('[data-organ]') : null;
      const key = el ? el.getAttribute('data-organ') : null;
      if (key) select(key, { fly: true });
      else select(null, { fly: false });
    };
    svg.addEventListener('pointerup', endPointer);
    svg.addEventListener('pointercancel', endPointer);
    svg.addEventListener('dblclick', (e) => {
      e.preventDefault();
      const [px, py] = toSvg(e.clientX, e.clientY);
      zoomAt(e.shiftKey ? 0.5 : 2, px, py, true);
    });
    svg.addEventListener('keydown', (e) => {
      const step = st.vb.w * 0.12;
      const [cx, cy] = centre();
      const keys = {
        '+': () => zoomAt(1.5, cx, cy, true), '=': () => zoomAt(1.5, cx, cy, true), '-': () => zoomAt(1 / 1.5, cx, cy, true),
        '_': () => zoomAt(1 / 1.5, cx, cy, true), '0': () => animateTo(BASE),
        ArrowLeft: () => setView(Object.assign({}, st.vb, { x: st.vb.x - step })), ArrowRight: () => setView(Object.assign({}, st.vb, { x: st.vb.x + step })),
        ArrowUp: () => setView(Object.assign({}, st.vb, { y: st.vb.y - step })), ArrowDown: () => setView(Object.assign({}, st.vb, { y: st.vb.y + step })),
        n: () => cycle(1), N: () => cycle(1), p: () => cycle(-1), P: () => cycle(-1),
        Escape: () => select(null, { fly: true }),
      };
      if (keys[e.key] && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); keys[e.key](); }
    });

    // ---- toolbar
    const zoomOut = h('span', { class: 'bv-zoom small muted', 'aria-live': 'polite', text: '100%' });
    const toggle = (label, getter, setter, title) => {
      const b = h('button', { type: 'button', class: 'chip bv-toggle', 'aria-pressed': String(getter()), title: title || null, onclick: () => {
        setter(!getter());
        b.setAttribute('aria-pressed', String(getter()));
        draw();
      } }, label);
      return b;
    };
    const levelChips = Object.entries(BODY_LEVELS).map(([lv, en]) => {
      const n = (bm.organs || []).filter((f) => f.level === lv).length;
      const b = h('button', { type: 'button', class: 'chip bv-level', 'aria-pressed': 'true', disabled: !n, title: en, onclick: () => {
        if (st.levels.has(lv)) st.levels.delete(lv); else st.levels.add(lv);
        b.setAttribute('aria-pressed', String(st.levels.has(lv)));
        draw();
      } }, h('span', { class: 'bm-dot lv-' + lv }), en, h('span', { class: 'count', text: String(n) }));
      return b;
    });
    const sexSel = h('select', { class: 'input bv-sex', 'aria-label': 'Reproductive organs shown', onchange: () => { st.sex = sexSel.value; draw(); } },
      h('option', { value: 'auto', text: 'Reproductive: with evidence' }),
      h('option', { value: 'female', text: 'Female' }), h('option', { value: 'male', text: 'Male' }));
    const listId = 'bv-organs-' + bm.map_id;
    const search = h('input', { type: 'search', class: 'input bv-search', list: listId, placeholder: 'Find organ…', 'aria-label': 'Find an organ' });
    const options = h('datalist', { id: listId }, (L.organs || []).map((o) => h('option', { value: o.en })));
    search.addEventListener('change', () => {
      const q = search.value.trim().toLowerCase();
      if (!q) return;
      const o = (L.organs || []).find((x) => x.en.toLowerCase() === q) ||
        (L.organs || []).find((x) => x.en.toLowerCase().includes(q) || x.key.includes(q));
      if (!o) { toast('No organ called "' + search.value + '"', true); return; }
      if (!visible(o)) { st.all = true; allBtn.setAttribute('aria-pressed', 'true'); if (o.sex) { st.sex = o.sex; sexSel.value = o.sex; } draw(); }
      select(o.key, { fly: true });
    });
    const allBtn = toggle('All organs', () => st.all, (v) => { st.all = v; }, 'Draw every organ, also those without evidence');
    const box = h('div', { class: 'bv' });
    const fullBtn = h('button', { type: 'button', class: 'btn btn-sm', title: 'Full screen', onclick: () => {
      if (document.fullscreenElement) document.exitFullscreen(); else if (box.requestFullscreen) box.requestFullscreen().catch(() => toast('Full screen is not available here', true));
    } }, '⛶ Full screen');
    const toolbar = h('div', { class: 'bv-toolbar' },
      h('div', { class: 'bv-tools' },
        h('button', { type: 'button', class: 'btn btn-sm', 'aria-label': 'Zoom in', onclick: () => zoomAt(1.5, centre()[0], centre()[1], true) }, '+'),
        h('button', { type: 'button', class: 'btn btn-sm', 'aria-label': 'Zoom out', onclick: () => zoomAt(1 / 1.5, centre()[0], centre()[1], true) }, '−'),
        h('button', { type: 'button', class: 'btn btn-sm', onclick: () => animateTo(BASE) }, '⟲ Fit'),
        zoomOut, fullBtn, search, options),
      h('div', { class: 'bv-tools' }, levelChips,
        toggle('Labels', () => st.labels, (v) => { st.labels = v; }),
        toggle('Body systems', () => st.systems, (v) => { st.systems = v; }, 'Skeleton, blood vessels and nerves'),
        allBtn,
        toggle('Natural colours', () => st.natural, (v) => { st.natural = v; }, 'Organs without evidence in their natural colours instead of grey'),
        sexSel));
    const info = h('div', { class: 'bv-info small', 'aria-live': 'polite' });
    box.append(toolbar, h('div', { class: 'bv-stage' }, svg, info));
    if (!art) box.appendChild(h('p', { class: 'small muted', text: 'Figure Studio is not available: organs are drawn as simple shapes.' }));
    draw();
    return { el: box, select: (key, opts) => select(key, Object.assign({ notify: false }, opts || {})) };
  }

  function bodyEvidenceTable(f) {
    return h('div', { class: 'table-wrap' }, h('table', { class: 'data bm-ev' },
      h('thead', null, h('tr', null, h('th', { text: 'Evidence' }), h('th', { text: 'What the database says' }),
        h('th', { class: 'num', text: 'Value' }), h('th', { text: 'Source' }))),
      h('tbody', null, (f.evidence || []).map((ev) => h('tr', null,
        h('td', null, h('span', { class: 'bm-dot lv-' + ((BODY_KINDS[ev.kind] && ev.kind) ? levelOfKind(ev.kind) : 'none') }), BODY_KINDS[ev.kind] || ev.kind),
        h('td', null, ev.text, h('div', { class: 'muted small' }, 'matched: ', h('code', { text: ev.matched }))),
        h('td', { class: 'num', text: ev.value !== null && ev.value !== undefined ? fmtVal(ev.value) + (ev.unit ? ' ' + ev.unit : '') : '–' }),
        h('td', null, sourceBadge(ev.source), ' ', ev.url ? extLink(ev.url, clip(ev.record_id || 'link', 28)) : h('code', { text: clip(ev.record_id || '', 28) })))))));
  }
  function levelOfKind(kind) {
    return { treats: 'primary', disease: 'primary', phenotype: 'primary', adverse_event: 'safety', label: 'safety',
      toxicity: 'safety', studied: 'investigational', expression: 'expression' }[kind] || 'none';
  }

  function bodyFigureUrl(bm, name, fmt, download) {
    return '/api/body/' + bm.map_id + '/figures/' + encodeURIComponent(name) + '.' + fmt + (download ? '?download=1' : '');
  }

  function bodyView(bm) {
    const okId = SEARCH_ID_RE.test(bm.map_id || '');
    const list = h('div', { class: 'bm-list' });
    const cards = new Map();
    let viewer = null;
    const pick = (key, fromList, scroll) => {
      for (const [kk, card] of cards) {
        const on = kk === key;
        card.classList.toggle('is-sel', on);
        const det = card.querySelector('details');
        if (det && on) det.open = true;
      }
      if (viewer && fromList) viewer.select(key, { fly: true });
      const card = cards.get(key);
      if (!card) return;
      if (scroll) card.scrollIntoView({ behavior: 'smooth', block: 'start' });  // asked for: bring the evidence into view
      else if (!fromList) list.scrollTo({ top: card.offsetTop - 8, behavior: 'smooth' });  // only inside the list
    };
    let lastLevel = null;
    for (const f of bm.organs || []) {
      if (f.level !== lastLevel) {
        list.appendChild(h('h4', { class: 'bm-level-h' }, h('span', { class: 'bm-dot lv-' + f.level }), BODY_LEVELS[f.level]));
        lastLevel = f.level;
      }
      const card = h('div', { class: 'bm-card lv-' + f.level, dataset: { organ: f.key } },
        h('div', { class: 'bm-card-head', role: 'button', tabindex: 0, onclick: () => pick(f.key, true),
          onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(f.key, true); } } },
        h('b', { text: f.en }),
        h('span', { class: 'bm-kinds small muted', text: Object.entries(f.kinds || {}).map(([kk, n]) => (BODY_KINDS[kk] || kk) + ' ' + n).join(' · ') })),
        h('div', { class: 'small bm-summary', text: f.summary }),
        h('details', { class: 'small' }, h('summary', { text: (f.evidence || []).length + ' evidence items' }), bodyEvidenceTable(f)));
      cards.set(f.key, card);
      list.appendChild(card);
    }
    if (!(bm.organs || []).length) list.appendChild(h('div', { class: 'empty', text: 'No organ evidence was found for this query.' }));
    viewer = bodyViewer(bm, (key, scroll) => pick(key, false, scroll));

    const facts = (bm.facts || []).length ? h('section', { class: 'panel bm-facts' },
      h('h3', null, 'Key facts'),
      h('table', { class: 'data kv' }, h('tbody', null, bm.facts.map((f) => h('tr', null,
        h('th', { text: f.label }),
        h('td', null, f.value, f.source ? h('span', null, ' ', sourceBadge(f.source)) : null,
          f.url ? h('span', null, ' ', extLink(f.url, '↗', 'small')) : null))))))
      : null;

    const figs = bm.figures || {};
    const figOk = figs.available && okId;
    const figBlock = h('section', { class: 'panel' },
      h('div', { class: 'panel-head row' },
        h('div', null, h('h3', null, 'Figure Studio figures'),
          h('p', { class: 'muted small', text: 'Drawn by Figure Studio with its body and organ illustrations' })),
        okId ? h('button', { type: 'button', class: 'btn btn-primary btn-sm', onclick: async (e) => {
          const btn = e.currentTarget;
          btn.disabled = true;
          exportOut.replaceChildren(h('div', { class: 'typing' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Writing the data package…'));
          try { exportOut.replaceChildren(packageCard(await api('/api/body/' + bm.map_id + '/export', { method: 'POST' }))); }
          catch (err) { exportOut.replaceChildren(errorBox(err)); }
          finally { btn.disabled = false; }
        } }, 'Export data') : null),
      figOk ? null : h('p', { class: 'notice small' }, 'Figure Studio is not available: ', h('span', { text: figs.error || '' })),
      figOk ? h('div', { class: 'bm-figs' }, (figs.names || []).map((name) => {
        const img = h('img', { class: 'vz-img', src: bodyFigureUrl(bm, name, 'svg'), alt: name === 'body_map' ? 'Body map of ' + bm.name : 'Evidence per organ', loading: 'lazy' });
        img.addEventListener('error', () => img.replaceWith(h('p', { class: 'muted small', text: 'figure could not be drawn' })), { once: true });
        return h('figure', { class: 'bm-fig' }, h('div', { class: 'vz-paper' }, img),
          h('figcaption', { class: 'small' }, name === 'body_map' ? 'Body map' : 'Evidence per organ', ' ',
            h('span', { class: 'fig-dl' }, ['svg', 'png', 'pdf'].map((fmt) => h('a', { class: 'btn btn-xs', href: bodyFigureUrl(bm, name, fmt, true), download: '' }, fmt.toUpperCase())))));
      })) : null);
    const exportOut = h('div', { 'aria-live': 'polite' });
    figBlock.appendChild(exportOut);

    return h('div', { class: 'bm' },
      h('div', { class: 'bm-head' },
        h('h2', null, bm.name + ' ', h('span', { class: 'badge outline', text: BODY_TYPES[bm.kind] || bm.kind })),
        h('div', { class: 'id-chips' }, Object.entries(bm.identifiers || {}).slice(0, 8).map(([kk, v]) => h('span', { class: 'id-chip' }, h('b', { text: kk + ':' }), String(v))))),
      checksSummary(bm.checks, 'every organ comes from a database record, re-checked against its own words and numbers'),
      facts,
      h('div', { class: 'bm-grid' },
        h('section', { class: 'panel bm-figure' }, h('h3', null, 'Interactive body map: zoom, drag, click an organ'), viewer.el),
        h('section', { class: 'panel bm-organs' }, h('h3', null, 'Organs and their evidence'), list)),
      (bm.notes || []).length ? h('ul', { class: 'small muted bm-notes' }, bm.notes.map((n) => h('li', { text: n }))) : null,
      Object.keys(bm.errors || {}).length ? h('details', { class: 'small' }, h('summary', { class: 'muted', text: Object.keys(bm.errors).length + ' source call(s) failed' }),
        h('ul', null, Object.entries(bm.errors).map(([kk, v]) => h('li', null, h('b', { text: kk + ': ' }), v)))) : null,
      figBlock);
  }

  function bodyPanel(res) {
    const queries = (res.queries && res.queries.length ? res.queries : [res.query]).filter(Boolean);
    const qSel = h('select', { class: 'input', 'aria-label': 'Which query' }, queries.map((q) => h('option', { value: q, text: q })));
    const kSel = h('select', { class: 'input', 'aria-label': 'What the query is' },
      h('option', { value: 'auto', text: 'Auto' }), h('option', { value: 'drug', text: 'Drug' }),
      h('option', { value: 'compound', text: 'Chemical' }), h('option', { value: 'disease', text: 'Disease' }));
    const out = h('div', { 'aria-live': 'polite' });
    let seq = 0;
    const run = async () => {
      const mine = ++seq;
      out.replaceChildren(h('div', { class: 'typing' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }),
        'Looking up indications, adverse events, label warnings, phenotypes and tissue expression…'));
      try {
        const data = await api('/api/body', { method: 'POST', body: { query: qSel.value, kind: kSel.value } });
        if (mine === seq) out.replaceChildren(bodyView(data));
      } catch (err) {
        if (mine === seq) out.replaceChildren(errorBox(err));
      }
    };
    const box = h('div', { class: 'bm-panel' },
      h('div', { class: 'panel-head row' },
        h('div', null, h('h2', null, 'Where it acts in the body'),
          h('p', { class: 'muted small', text: 'Drugs: indications, FAERS adverse events, FDA label warnings and target tissue expression; diseases: disease site, HPO phenotypes and gene expression.' })),
        h('div', { class: 'head-actions' }, queries.length > 1 ? qSel : null, kSel,
          h('button', { type: 'button', class: 'btn btn-sm', onclick: run }, 'Rebuild'))),
      out);
    qSel.addEventListener('change', run);
    kSel.addEventListener('change', run);
    run();
    return box;
  }

  // ------------------------------------------------------------- export, run, resolve
  function exportPanel() {
    const sel = h('select', { class: 'input', 'aria-label': 'Split the files by' },
      h('option', { value: 'type', text: 'one file per data type (papers, trials, datasets ...)' }),
      h('option', { value: 'species', text: 'one file per species (human, mouse, plant ...)' }),
      h('option', { value: 'source', text: 'one file per database' }));
    const figs = h('input', { type: 'checkbox', checked: true, id: 'export-figs' });
    const out = h('div', { 'aria-live': 'polite' });
    const btn = h('button', { type: 'button', class: 'btn btn-primary', onclick: async () => {
      btn.disabled = true;
      out.replaceChildren(h('div', { class: 'typing' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }),
        'Fetching full records, writing the tables' + (figs.checked ? ' and drawing the figures' : '') + '…'));
      try {
        const body = { query: searchState.lastQuery, limit_per_source: searchState.lastLimit, group_by: sel.value, include_figures: figs.checked };
        if (searchState.lastCats && searchState.lastCats.length) body.categories = searchState.lastCats;
        if (searchState.lastCompare && searchState.lastCompare.length) body.compare = searchState.lastCompare;
        out.replaceChildren(packageCard(await api('/api/search/export', { method: 'POST', body })));
      } catch (err) {
        out.replaceChildren(errorBox(err));
      } finally {
        btn.disabled = false;
      }
    } }, 'Export data');
    return h('div', { class: 'panel export-panel' },
      h('div', { class: 'search-row' }, h('b', { text: 'Download this data:' }), sel, btn),
      h('div', { class: 'field-check small' }, figs, h('label', { for: 'export-figs', text: 'Include the mind map and charts (SVG / PNG / PDF) and figure_data.csv' })),
      h('p', { class: 'small muted', text: 'Saves the actual data (full records re-fetched from each database, not links) as one CSV per kind of data, an Excel workbook (a sheet per file, plus the figures), the figures and a ZIP of everything.' }),
      out);
  }

  function parseCompare(text) {
    return String(text || '').split(/[,;，；\n]+/).map((s) => s.trim()).filter(Boolean).slice(0, 3);
  }

  async function runSearch(e) {
    e.preventDefault();
    const q = $('#search-q').value.trim();
    if (!q) { $('#search-q').focus(); return; }
    if (searchState.controller) searchState.controller.abort();
    searchState.controller = new AbortController();
    const btn = $('#search-go');
    btn.disabled = true;
    const compare = parseCompare($('#search-compare') ? $('#search-compare').value : '');
    $('#search-results').replaceChildren(h('div', { class: 'typing' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }),
      compare.length ? 'Searching and comparing ' + (compare.length + 1) + ' queries…' : 'Searching every selected database in parallel…'));
    try {
      const body = { query: q, limit_per_source: Number($('#search-limit').value) || 5 };
      if (searchState.cats.size) body.categories = Array.from(searchState.cats);
      if (compare.length) body.compare = compare;
      searchState.lastQuery = q;
      searchState.lastLimit = body.limit_per_source;
      searchState.lastCats = body.categories || [];
      searchState.lastCompare = compare;
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
      const packages = items.filter((e) => e.kind === 'package');
      if (packages.length) {
        box.appendChild(h('h2', { class: 'section-h', text: 'Data packages' }));
        for (const e of packages) box.appendChild(packageCard(e, previewPackageFile));
      }
      const tables = items.filter((e) => e.kind !== 'package');
      if (!tables.length) return;
      box.appendChild(h('h2', { class: 'section-h', text: 'Single tables' }));
      const rows = tables.map((e) => h('tr', null,
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

  async function previewPackageFile(e, f) {
    const box = $('#export-preview');
    const url = pkgUrl(f.preview_url);
    if (!url) return;
    box.replaceChildren(h('div', { class: 'typing' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Loading…'));
    try {
      const data = await api(url);
      const columns = (data.columns || []).map(String);
      const rows = data.rows || [];
      const cellNode = (v) => {
        const text = v === null || v === undefined ? '' : String(v);
        return /^https?:\/\//.test(text) && safeUrl(text, false) && !text.includes('; ') ? extLink(text, clip(text, 60)) : clip(text, 300);
      };
      const panel = h('section', { class: 'panel', 'aria-label': 'Table preview' },
        h('div', { class: 'panel-head row' }, h('h2', { text: (e.name || '') + ' · ' + f.file }),
          h('button', { type: 'button', class: 'btn btn-sm', onclick: () => clear(box) }, 'Close')),
        h('p', { class: 'small muted', text: 'Showing ' + rows.length + (data.truncated ? '+' : '') + ' rows · ' + columns.length + ' columns' }),
        h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
          h('thead', null, h('tr', null, columns.map((c) => h('th', { scope: 'col', text: c })))),
          h('tbody', null, rows.map((row) => h('tr', null, columns.map((c) => h('td', null, cellNode(row[c])))))))));
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
  // Cross-species comparison: form, dashboard, chat card
  // ===================================================================
  // Renders a SpeciesComparison (mosaic/species/models.py). Charts are hand-written SVG; every
  // server string goes through textContent; colours come from CSS variables (light + dark).
  const SP_ID_RE = /^[0-9a-f]{32}$/;
  const SP_GROUP_ORDER = ['human', 'primate', 'mammal', 'bird', 'amphibian', 'fish', 'insect', 'nematode', 'fungus', 'plant'];
  const SP_FORM_GROUPS = [
    ['', 'Human & primates', ['human', 'primate']],
    ['', 'Mammals', ['mammal']],
    ['', 'Birds', ['bird']],
    ['', 'Amphibians & fish', ['amphibian', 'fish']],
    ['', 'Invertebrates', ['insect', 'nematode']],
    ['', 'Fungi & plants', ['fungus', 'plant']],
  ];
  const SP_EVIDENCE = {
    clinical: { rank: 4, en: 'Clinical', mark: 'C' },
    in_vivo: { rank: 3, en: 'In vivo', mark: 'V' },
    in_vitro: { rank: 2, en: 'In vitro', mark: 'T' },
    computational: { rank: 1, en: 'Computational', mark: 'P' },
    none: { rank: 0, en: 'None', mark: '' },
  };
  const SP_KINDS = {
    potency: ['', 'Potency'],
    in_vivo_efficacy: ['', 'In vivo efficacy'],
    pharmacokinetics: ['', 'Pharmacokinetics'],
    toxicity: ['', 'Toxicity'],
    other: ['', 'Other'],
  };
  const SP_KIND_ORDER = Object.keys(SP_KINDS);
  const SP_COMPOUND_KIND = { drug: 'Drug', compound: 'Compound', unknown: 'Unknown type' };
  const SP_BINS = [
    // [class suffix, lower bound, label]
    [5, 98, '≥ 98%'], [4, 90, '90–98%'], [3, 75, '75–90%'], [2, 50, '50–75%'], [1, -Infinity, '< 50%'],
  ];
  const spState = {
    meta: null, metaPromise: null, cache: new Map(), current: null, selected: null,
    paperFilter: '', sort: { key: 'order', dir: 1 }, metric: null, running: false,
  };

  function svgEl(tag, attrs) {
    const el = document.createElementNS(SVG_NS, tag);
    if (attrs) {
      for (const k of Object.keys(attrs)) {
        const v = attrs[k];
        if (v === null || v === undefined || v === false) continue;
        if (k === 'text') el.textContent = String(v);
        else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
        else el.setAttribute(k, String(v));
      }
    }
    for (const kid of Array.prototype.slice.call(arguments, 2).flat(Infinity)) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }

  const isNum = (v) => typeof v === 'number' && isFinite(v);
  function fmtPct(v) { return isNum(v) ? (Math.round(v * 10) / 10) + '%' : '–'; }
  function fmtVal(v) {
    if (!isNum(v)) return '–';
    const a = Math.abs(v);
    return a !== 0 && (a < 0.01 || a >= 1e6) ? v.toExponential(2) : String(Math.round(v * 100) / 100);
  }
  function idBin(v) {
    if (!isNum(v)) return 0;
    for (const [n, lo] of SP_BINS) if (v >= lo) return n;
    return 1;
  }
  function spLabel(s) { return s ? (s.label || s.species || '') : ''; }
  function evInfo(level) { return SP_EVIDENCE[level] || SP_EVIDENCE.none; }
  function evBadge(level) {
    const e = evInfo(level);
    return h('span', { class: 'sp-ev sp-ev-' + (SP_EVIDENCE[level] ? level : 'none'), title: 'Evidence level: ' + e.en },
      h('span', { class: 'sp-ev-dots', 'aria-hidden': 'true' }, [1, 2, 3, 4].map((i) => h('i', { class: i <= e.rank ? 'on' : null }))),
      e.en);
  }
  function pubmedUrl(pmid) { return /^\d{1,9}$/.test(String(pmid || '')) ? 'https://pubmed.ncbi.nlm.nih.gov/' + pmid + '/' : null; }
  function doiUrl(doi) { return typeof doi === 'string' && /^10\.\S+$/.test(doi.trim()) ? 'https://doi.org/' + encodeURI(doi.trim()) : null; }
  function uniprotUrl(acc) { return /^[A-Z0-9]{6,10}(-\d+)?$/.test(String(acc || '')) ? 'https://www.uniprot.org/uniprotkb/' + acc + '/entry' : null; }
  const ID_LINKS = {
    chembl: (v) => /^CHEMBL\d+$/.test(v) ? 'https://www.ebi.ac.uk/chembl/explore/compound/' + v : null,
    pubchem_cid: (v) => /^\d+$/.test(v) ? 'https://pubchem.ncbi.nlm.nih.gov/compound/' + v : null,
    drugbank: (v) => /^DB\d+$/.test(v) ? 'https://go.drugbank.com/drugs/' + v : null,
    inchikey: (v) => /^[A-Z]{14}-[A-Z]{10}-[A-Z]$/.test(v) ? 'https://pubchem.ncbi.nlm.nih.gov/#query=' + v : null,
  };

  /** Shared floating tooltip for SVG marks (hover + keyboard focus). */
  let spTip = null;
  function showTip(target, lines) {
    if (!spTip) { spTip = h('div', { class: 'sp-tip', role: 'tooltip', hidden: true }); document.body.appendChild(spTip); }
    spTip.replaceChildren(...lines.filter(Boolean).map((l, i) => h('div', { class: i === 0 ? 'sp-tip-h' : null, text: l })));
    spTip.hidden = false;
    const r = target.getBoundingClientRect();
    const tw = spTip.offsetWidth;
    const th = spTip.offsetHeight;
    let x = r.left + r.width / 2 - tw / 2;
    let y = r.top - th - 8;
    if (y < 8) y = r.bottom + 8;
    x = Math.max(8, Math.min(window.innerWidth - tw - 8, x));
    spTip.style.left = x + 'px';
    spTip.style.top = y + 'px';
  }
  function hideTip() { if (spTip) spTip.hidden = true; }
  function withTip(el, lines) {
    el.addEventListener('mouseenter', () => showTip(el, lines));
    el.addEventListener('mouseleave', hideTip);
    el.addEventListener('focus', () => showTip(el, lines));
    el.addEventListener('blur', hideTip);
    return el;
  }
  function activate(el, fn) {
    el.addEventListener('click', fn);
    el.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fn(e); } });
    return el;
  }

  /** Re-draws ``draw(width)`` into ``holder`` whenever its width changes. */
  function responsive(holder, draw) {
    let last = 0;
    const render = (force) => {
      const w = Math.round(holder.clientWidth);
      if (!w || (force !== true && Math.abs(w - last) < 8)) return;
      last = w;
      holder.replaceChildren(draw(Math.max(300, w)));
    };
    holder.spRedraw = () => render(true);
    if (window.ResizeObserver) {
      const ro = new ResizeObserver(() => { if (!holder.isConnected && last) { ro.disconnect(); return; } render(); });
      ro.observe(holder);
    } else {
      window.addEventListener('resize', render);
    }
    requestAnimationFrame(render);
    return holder;
  }

  /** Two-line bilingual row label (Chinese first) right-aligned at (x, y). */
  function rowLabel(s, x, y) {
    return svgEl('text', { class: 'sp-lbl', x, y, 'text-anchor': 'end' },
      svgEl('tspan', { class: 'sp-lbl-n', x, dy: 4, text: s.label || s.species }));
  }

  function legend(items) {
    return h('div', { class: 'sp-legend' }, items.map(([cls, label, shape]) =>
      h('span', { class: 'sp-legend-item' }, h('span', { class: 'sp-sw ' + cls + (shape ? ' sp-sw-' + shape : ''), 'aria-hidden': 'true' }), label)));
  }

  function chartBlock(title, sub, body, table) {
    return h('section', { class: 'panel sp-chart' },
      h('div', { class: 'panel-head' }, h('h3', { text: title }), sub ? h('p', { class: 'muted small', text: sub }) : null),
      body,
      table ? h('details', { class: 'sp-table-alt' }, h('summary', { text: 'View as table' }), table) : null);
  }

  function miniTable(headers, rows, numeric) {
    const num = new Set(numeric || []);
    return h('div', { class: 'table-wrap' }, h('table', { class: 'data' },
      h('thead', null, h('tr', null, headers.map((c, i) => h('th', { scope: 'col', class: num.has(i) ? 'num' : null, text: c })))),
      h('tbody', null, rows.map((cells) => h('tr', null, cells.map((c, i) => h('td', { class: num.has(i) ? 'num' : null }, c)))))));
  }

  /** Species summaries sorted human first, then by evolutionary distance (group order). */
  function orderedSpecies(list) {
    const arr = (Array.isArray(list) ? list : []).filter((s) => s && typeof s === 'object' && s.species);
    return arr.map((s, i) => [s, i]).sort((a, b) => {
      const ga = SP_GROUP_ORDER.indexOf(a[0].group); const gb = SP_GROUP_ORDER.indexOf(b[0].group);
      return (ga < 0 ? 99 : ga) - (gb < 0 ? 99 : gb) || a[1] - b[1];
    }).map((x) => x[0]);
  }
  function speciesIndex(c) {
    const map = new Map();
    for (const s of orderedSpecies(c.species)) map.set(s.species, s);
    return map;
  }
  function meanSimilarity(c, key) {
    const vals = (c.similarity || []).filter((o) => o && o.species === key && isNum(o.similarity_pct)).map((o) => o.similarity_pct);
    return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
  }

  // ------------------------------------------------------------- overview graph
  /** Radial graph: human at the centre, species around it in evolutionary order; distance and
   * edge shade encode mean target identity, node area encodes number of papers. */
  function overviewGraph(speciesList, opts) {
    opts = opts || {};
    const compact = !!opts.compact;
    const all = orderedSpecies(speciesList);
    const human = all.find((s) => s.species === 'human') || { species: 'human', label: 'Human', mean_identity_pct: 100, n_papers: 0, evidence_level: 'none' };
    const others = all.filter((s) => s.species !== 'human');
    const W = compact ? 460 : 840;
    const H = compact ? 400 : 690;
    const cx = W / 2;
    const cy = H / 2;
    const rMin = compact ? 84 : 160;
    const rMax = compact ? 156 : 262;
    const DMAX = 60; // identity 40% and below sit on the outer guide ring
    const radius = (id) => isNum(id) ? rMin + Math.sqrt(Math.min(Math.max(0, 100 - id), DMAX) / DMAX) * (rMax - rMin) : rMax + (compact ? 16 : 26);
    // scale node area against the non-human species: human (often 10x more papers) would otherwise
    // shrink every other node to the minimum size; the human node has its own minimum radius
    const maxPapers = Math.max(1, ...(others.length ? others : all).map((s) => s.n_papers || 0));
    const nodeR = (n) => (compact ? 5 : 7) + Math.sqrt(Math.min(1, Math.max(0, n || 0) / maxPapers)) * (compact ? 9 : 17);
    const n = Math.max(1, others.length);
    const step = 360 / n;
    const svg = svgEl('svg', {
      class: 'sp-svg sp-graph' + (compact ? ' compact' : ''), viewBox: '0 0 ' + W + ' ' + H, role: 'group',
      'aria-label': 'Species overview graph: human at the centre, species placed by mean target identity',
    });
    svg.appendChild(svgEl('title', { text: 'Cross-species overview: closer and darker = higher target identity to human; larger node = more papers' }));
    // identity guide rings
    const ringAngle = -Math.PI / 2; // species start half a step clockwise of 12 o'clock, so the guide labels sit in the gap
    const guides = svgEl('g', { class: 'sp-guides', 'aria-hidden': 'true' });
    for (const v of compact ? [100, 90, 50] : [100, 90, 75, 50]) {
      const r = radius(v);
      guides.appendChild(svgEl('circle', { class: 'sp-ring', cx, cy, r }));
      guides.appendChild(svgEl('text', { class: 'tm', x: cx + Math.cos(ringAngle) * r, y: cy + Math.sin(ringAngle) * r - 3, 'text-anchor': 'middle', text: v + '%' }));
    }
    if (others.some((s) => !isNum(s.mean_identity_pct))) {
      const r = radius(null);
      guides.appendChild(svgEl('circle', { class: 'sp-ring sp-ring-none', cx, cy, r }));
      if (!compact) guides.appendChild(svgEl('text', { class: 'tm', x: cx + Math.cos(ringAngle) * r, y: cy + Math.sin(ringAngle) * r - 3, 'text-anchor': 'middle', text: 'no ortholog' }));
    }
    svg.appendChild(guides);
    const edges = svgEl('g', { class: 'sp-edges', 'aria-hidden': 'true' });
    const nodes = svgEl('g', { class: 'sp-nodes' });
    svg.appendChild(edges);
    svg.appendChild(nodes);

    function node(s, x, y, ang) {
      const r = s.species === 'human' ? Math.max(nodeR(s.n_papers), compact ? 13 : 20) : nodeR(s.n_papers);
      const bin = s.species === 'human' ? 'human' : 'b' + idBin(s.mean_identity_pct);
      const ev = evInfo(s.evidence_level);
      const stats = (s.species === 'human' ? 'reference' : fmtPct(s.mean_identity_pct)) + ' · ' + fmtNum(s.n_papers || 0) + ' papers';
      const g = svgEl('g', {
        class: 'sp-node' + (opts.selected === s.species ? ' is-sel' : ''), tabindex: opts.onSelect ? 0 : null,
        role: opts.onSelect ? 'button' : 'img', 'data-species': s.species,
        'aria-label': spLabel(s) + ': ' + (s.species === 'human' ? 'human reference' : 'mean target identity ' + fmtPct(s.mean_identity_pct)) +
          ', ' + fmtNum(s.n_papers || 0) + ' papers, ' + fmtNum(s.n_measurements || 0) + ' measurements, evidence ' + ev.en,
      });
      g.appendChild(svgEl('circle', { class: 'sp-hit', cx: x, cy: y, r: r + 8 }));
      g.appendChild(svgEl('circle', { class: 'n sp-fill-' + bin, cx: x, cy: y, r }));
      if (ev.mark) {
        const ma = ang + Math.PI * 1.25; // on the inner side of the node, away from its label
        const mx = x + Math.cos(ma) * r * 0.85;
        const my = y + Math.sin(ma) * r * 0.85;
        g.appendChild(svgEl('circle', { class: 'sp-evm', cx: mx, cy: my, r: compact ? 5.5 : 7 }));
        g.appendChild(svgEl('text', { class: 'sp-evm-t', x: mx, y: my, 'text-anchor': 'middle', 'dominant-baseline': 'central', text: ev.mark }));
      }
      // label, pushed outward along the spoke
      const c = Math.cos(ang);
      const sn = Math.sin(ang);
      let lx = x;
      let ly = y + r + 14;
      let anchor = 'middle';
      if (s.species !== 'human') {
        anchor = c > 0.2 ? 'start' : c < -0.2 ? 'end' : 'middle';
        if (anchor === 'middle') {
          ly = sn > 0 ? y + r + 14 : y - r - (compact ? 18 : 22);
        } else {
          lx = x + (c > 0 ? 1 : -1) * (r + 6);
          ly = y - 3;
        }
      }
      const label = svgEl('text', { class: 'sp-lbl', x: lx, y: ly, 'text-anchor': anchor },
        svgEl('tspan', { class: 'sp-lbl-n', x: lx, text: compact ? s.label : spLabel(s) }),
        svgEl('tspan', { class: 'tm', x: lx, dy: compact ? 12 : 14, text: compact ? (s.species === 'human' ? '' : fmtPct(s.mean_identity_pct)) : stats }));
      g.appendChild(label);
      withTip(g, [spLabel(s) + (s.scientific_name ? ' (' + s.scientific_name + ')' : ''),
        s.species === 'human' ? 'Human reference' : 'Mean target identity: ' + fmtPct(s.mean_identity_pct),
        'Papers: ' + fmtNum(s.n_papers || 0) + ' · Measurements: ' + fmtNum(s.n_measurements || 0),
        'Evidence: ' + ev.en,
        opts.onSelect ? 'Click for details' : null]);
      if (opts.onSelect) activate(g, () => { hideTip(); opts.onSelect(s.species); });
      return g;
    }

    others.forEach((s, i) => {
      const ang = (-90 + step / 2 + i * step) * Math.PI / 180;
      const r = radius(s.mean_identity_pct);
      const x = cx + Math.cos(ang) * r;
      const y = cy + Math.sin(ang) * r;
      const bin = idBin(s.mean_identity_pct);
      edges.appendChild(svgEl('line', { class: 'sp-edge sp-stroke-b' + bin, x1: cx, y1: cy, x2: x, y2: y }));
      nodes.appendChild(node(s, x, y, ang));
    });
    nodes.appendChild(node(human, cx, cy, Math.PI / 2));
    return svg;
  }

  function graphLegend() {
    return h('div', { class: 'sp-legend-wrap' },
      legend(SP_BINS.map(([n, , label]) => ['sp-bg-b' + n, label]).concat([['sp-bg-b0', 'none']])),
      h('div', { class: 'sp-legend' },
        h('span', { class: 'sp-legend-item' }, 'Closer & stronger colour = more similar target'),
        h('span', { class: 'sp-legend-item' }, 'Larger node = more papers'),
        h('span', { class: 'sp-legend-item' }, 'Label numbers: mean identity · papers'),
        h('span', { class: 'sp-legend-item' }, 'Marker: C clinical · V in vivo · T in vitro · P computational')));
  }

  // ------------------------------------------------------------- comparison table (heatmap)
  function heatTable(c, onSelect) {
    const targets = (c.targets || []).map((t) => t.gene_symbol).filter(Boolean);
    const species = orderedSpecies(c.species);
    const maxPapers = Math.max(1, ...species.map((s) => s.n_papers || 0));
    const maxMeas = Math.max(1, ...species.map((s) => s.n_measurements || 0));
    const order = new Map(species.map((s, i) => [s.species, i]));
    const cols = [
      { key: 'order', label: 'Species' },
      ...targets.map((t) => ({ key: 'id:' + t, label: t + ' identity', num: true })),
      targets.length > 1 ? { key: 'mean', label: 'Mean identity', num: true } : null,
      { key: 'sim', label: 'Similarity', num: true },
      { key: 'papers', label: 'PubMed papers', num: true },
      { key: 'meas', label: 'ChEMBL measurements', num: true },
      { key: 'pchembl', label: 'Median pChEMBL', num: true },
      { key: 'ev', label: 'Evidence', num: false },
    ].filter(Boolean);
    const value = (s, key) => {
      if (key === 'order') return order.get(s.species);
      if (key.startsWith('id:')) return (s.per_target_identity || {})[key.slice(3)];
      if (key === 'mean') return s.mean_identity_pct;
      if (key === 'sim') return meanSimilarity(c, s.species);
      if (key === 'papers') return s.n_papers || 0;
      if (key === 'meas') return s.n_measurements || 0;
      if (key === 'pchembl') return s.median_pchembl;
      if (key === 'ev') return evInfo(s.evidence_level).rank;
      return null;
    };
    const wrap = h('div', { class: 'table-wrap sp-heat' });
    if (!species.length) {
      wrap.appendChild(h('div', { class: 'empty', text: 'No species in this comparison.' }));
      return wrap;
    }
    function draw() {
      const { key, dir } = spState.sort;
      const rows = species.slice().sort((a, b) => {
        const va = value(a, key); const vb = value(b, key);
        const na = !isNum(va); const nb = !isNum(vb);
        if (na || nb) return na === nb ? order.get(a.species) - order.get(b.species) : na ? 1 : -1;
        return (va - vb) * dir || order.get(a.species) - order.get(b.species);
      });
      const cellPct = (v) => h('td', { class: 'num sp-cell sp-b' + idBin(v) }, fmtPct(v));
      const bar = (v, max) => {
        const td = h('td', { class: 'num sp-dbar' }, fmtNum(v || 0));
        td.style.setProperty('--w', Math.round(100 * (v || 0) / max) + '%');
        return td;
      };
      wrap.replaceChildren(h('table', { class: 'data' },
        h('caption', { class: 'sr-only', text: 'Target identity and evidence per species' }),
        h('thead', null, h('tr', null, cols.map((col) => {
          const active = key === col.key;
          return h('th', { scope: 'col', class: col.num ? 'num' : null, 'aria-sort': active ? (dir > 0 ? 'ascending' : 'descending') : 'none' },
            h('button', {
              type: 'button', class: 'sp-sort', onclick: () => {
                spState.sort = active ? { key: col.key, dir: -dir } : { key: col.key, dir: col.key === 'order' ? 1 : -1 };
                draw();
              },
            }, col.label, h('span', { class: 'sp-sort-i', 'aria-hidden': 'true', text: active ? (dir > 0 ? ' ▲' : ' ▼') : ' ↕' })));
        }))),
        h('tbody', null, rows.map((s) => h('tr', { class: spState.selected === s.species ? 'is-sel' : null },
          h('th', { scope: 'row', class: 'sp-rowh' }, h('button', { type: 'button', class: 'btn-link', onclick: () => onSelect(s.species) }, spLabel(s)),
            h('div', { class: 'muted small sp-sci', text: s.scientific_name || '' })),
          targets.length ? targets.map((t) => cellPct((s.per_target_identity || {})[t])) : null,
          targets.length > 1 ? cellPct(s.mean_identity_pct) : null,
          cellPct(meanSimilarity(c, s.species)),
          bar(s.n_papers, maxPapers),
          bar(s.n_measurements, maxMeas),
          h('td', { class: 'num' }, isNum(s.median_pchembl) ? s.median_pchembl.toFixed(2) : '–'),
          h('td', null, evBadge(s.evidence_level)))))));
    }
    draw();
    return wrap;
  }

  // ------------------------------------------------------------- identity bars
  function identityBars(c) {
    const targets = (c.targets || []).map((t) => t.gene_symbol).filter(Boolean);
    const species = orderedSpecies(c.species);
    if (!targets.length || !species.length) {
      return chartBlock('Target identity per species', null,
        h('div', { class: 'empty', text: 'No targets or species to plot.' }));
    }
    const multi = targets.length > 1;
    const shown = targets.slice(0, 8);
    const holder = responsive(h('div', { class: 'sp-chart-body' }), (W) => {
      const labelW = 130;
      const right = 54;
      const barH = multi ? 9 : 14;
      const rowH = Math.max(30, shown.length * (barH + 2) + 12);
      const top = 22;
      const H = top + species.length * rowH + 8;
      const plotW = Math.max(60, W - labelW - right);
      const x = (v) => labelW + (Math.max(0, Math.min(100, v)) / 100) * plotW;
      const svg = svgEl('svg', { class: 'sp-svg', viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, role: 'group',
        'aria-label': 'Bar chart of ortholog identity to human per species' });
      for (const t of [0, 25, 50, 75, 100]) {
        svg.appendChild(svgEl('line', { class: 'sp-grid', x1: x(t), x2: x(t), y1: top - 4, y2: H - 6 }));
        svg.appendChild(svgEl('text', { class: 'tm', x: x(t), y: 12, 'text-anchor': 'middle', text: t + '%' }));
      }
      species.forEach((s, i) => {
        const y0 = top + i * rowH;
        svg.appendChild(rowLabel(s, labelW - 10, y0 + rowH / 2 - 2));
        shown.forEach((t, j) => {
          const v = (s.per_target_identity || {})[t];
          const y = y0 + 4 + j * (barH + 2);
          if (!isNum(v)) {
            svg.appendChild(svgEl('text', { class: 'tm', x: labelW + 4, y: y + barH / 2, 'dominant-baseline': 'central', text: (multi ? t + ': ' : '') + 'no ortholog' }));
            return;
          }
          const w = Math.max(2, x(v) - labelW);
          const g = svgEl('g', { tabindex: 0, class: 'sp-mark', role: 'img', 'aria-label': spLabel(s) + ' ' + t + ': ' + fmtPct(v) });
          g.appendChild(svgEl('rect', { class: 'sp-hit', x: labelW, y: y - 1, width: plotW, height: barH + 2 }));
          // rounded data end, square at the baseline
          const rr = Math.min(4, barH / 2, w);
          g.appendChild(svgEl('path', {
            class: 'sp-bar sp-c' + (multi ? j + 1 : 1),
            d: 'M' + labelW + ',' + y + 'h' + (w - rr) + 'a' + rr + ',' + rr + ' 0 0 1 ' + rr + ',' + rr + 'v' + (barH - 2 * rr) +
              'a' + rr + ',' + rr + ' 0 0 1 -' + rr + ',' + rr + 'h-' + (w - rr) + 'z',
          }));
          if (!multi || j === 0 || W >= 520) svg.appendChild(svgEl('text', { class: 't2 sp-val', x: labelW + w + 6, y: y + barH / 2, 'dominant-baseline': 'central', text: fmtPct(v) }));
          withTip(g, [spLabel(s), (multi ? t + ' ' : '') + 'Identity: ' + fmtPct(v)]);
          svg.appendChild(g);
        });
      });
      return svg;
    });
    const table = miniTable(['Species'].concat(targets.map((t) => t + ' identity %')),
      species.map((s) => [spLabel(s)].concat(targets.map((t) => fmtPct((s.per_target_identity || {})[t])))), targets.map((_, i) => i + 1));
    const body = h('div', null, multi ? legend(shown.map((t, j) => ['sp-bg-c' + (j + 1), t])) : null, holder,
      targets.length > 8 ? h('p', { class: 'muted small', text: 'Only the first 8 targets are drawn; the table lists all.' }) : null);
    return chartBlock('Target identity per species',
      multi ? 'One bar per target' : 'Sequence identity of each ortholog to human ' + targets[0], body, table);
  }

  // ------------------------------------------------------------- dose-effect scatter
  function dosePoints(c) {
    const pts = [];
    for (const p of c.papers || []) {
      if (!p || !isNum(p.dose_mg_kg) || p.dose_mg_kg <= 0 || !isNum(p.effect_value)) continue;
      pts.push({
        species: p.species, dose: p.dose_mg_kg, value: p.effect_value, unit: (p.effect_unit || '').trim(), source: 'paper',
        what: p.effect || p.finding || '', ref: p.pmid ? 'PMID ' + p.pmid : p.doi ? 'DOI ' + p.doi : clip(p.title, 80),
        title: p.title, url: p.url || pubmedUrl(p.pmid) || doiUrl(p.doi),
      });
    }
    for (const m of c.measurements || []) {
      if (!m || !isNum(m.dose_mg_kg) || m.dose_mg_kg <= 0 || !isNum(m.value)) continue;
      pts.push({
        species: m.species, dose: m.dose_mg_kg, value: m.value, unit: (m.units || '').trim(), source: 'measurement',
        what: [m.standard_type, m.description].filter(Boolean).join(' · '),
        ref: m.pmid ? 'PMID ' + m.pmid : m.source_id ? (m.source || 'chembl').toUpperCase() + ' ' + m.source_id : (m.source || 'ChEMBL'),
        title: m.description, url: m.url || pubmedUrl(m.pmid) || doiUrl(m.doi),
      });
    }
    return pts.filter((p) => p.species);
  }

  function niceMax(v) {
    if (!(v > 0)) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
    return 10 * p;
  }

  function doseScatter(c) {
    const sIndex = speciesIndex(c);
    const pts = dosePoints(c);
    const title = 'Dose–effect';
    if (!pts.length) {
      return chartBlock(title, null, h('div', { class: 'empty' },
        h('span', { text: 'No dose–effect data yet: it needs papers or measurements with a dose in mg/kg and a numeric effect. Tick “extract dose/effect with Claude” to fill them from abstracts.' })));
    }
    // one y-axis per unit: group by unit and let the reader pick
    const byUnit = new Map();
    for (const p of pts) { const k = p.unit || '(no unit)'; if (!byUnit.has(k)) byUnit.set(k, []); byUnit.get(k).push(p); }
    const units = [...byUnit.keys()].sort((a, b) => byUnit.get(b).length - byUnit.get(a).length);
    if (!spState.metric || !byUnit.has(spState.metric)) spState.metric = units[0];
    // colour follows the species (fixed order of first appearance in evolutionary order); > 8 folds into "Other"
    const speciesKeys = [...new Set(pts.map((p) => p.species))].sort((a, b) => {
      const ia = [...sIndex.keys()].indexOf(a); const ib = [...sIndex.keys()].indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    });
    const slot = (k) => { const i = speciesKeys.indexOf(k); return i >= 0 && i < 8 ? 'sp-c' + (i + 1) : 'sp-c-other'; };
    const nameOf = (k) => sIndex.has(k) ? spLabel(sIndex.get(k)) : k;
    const select = h('select', { class: 'input', id: 'sp-metric', onchange: (e) => { spState.metric = e.target.value; redraw(); } },
      units.map((u) => h('option', { value: u, selected: u === spState.metric }, 'Effect (' + u + ') · ' + byUnit.get(u).length)));
    const holder = h('div', { class: 'sp-chart-body' });
    const tableBox = h('div');
    const legendBox = h('div');
    let observed = false;
    function redraw() {
      const data = byUnit.get(spState.metric) || [];
      const present = speciesKeys.filter((k) => data.some((p) => p.species === k));
      const shapes = [['paper', 'Paper', 'circle'], ['measurement', 'ChEMBL measurement', 'square']].filter(([src]) => data.some((p) => p.source === src));
      legendBox.replaceChildren(legend(present.filter((k) => speciesKeys.indexOf(k) < 8).map((k) => [slot(k).replace('sp-', 'sp-bg-'), nameOf(k)])
        .concat(present.some((k) => speciesKeys.indexOf(k) >= 8) ? [['sp-bg-c-other', 'Other']] : [])
        .concat(shapes.map(([, label, shape]) => ['sp-sw-ink', label, shape]))));
      // a single ResizeObserver per holder, always drawing the currently selected unit
      // (re-creating one per change left stale observers redrawing old data on resize)
      if (!observed) { observed = true; responsive(holder, (W) => drawScatter(W, byUnit.get(spState.metric) || [], slot, nameOf)); }
      else if (holder.spRedraw) holder.spRedraw();
      tableBox.replaceChildren(miniTable(['Species', 'Dose (mg/kg)', 'Effect', 'Unit', 'Description', 'Reference'],
        data.map((p) => [nameOf(p.species), fmtVal(p.dose), fmtVal(p.value), p.unit, clip(p.what, 120), p.url ? extLink(p.url, p.ref) : p.ref]), [1, 2]));
    }
    redraw();
    const controls = h('div', { class: 'sp-controls' }, h('label', { for: 'sp-metric', class: 'small', text: 'Y axis' }), select);
    return chartBlock(title, 'X = dose (mg/kg, log scale) · Y = effect value · click a point to open its reference',
      h('div', null, controls, legendBox, holder), tableBox);
  }

  function drawScatter(W, data, slot, nameOf) {
    const H = W < 520 ? 260 : 320;
    const m = { l: 52, r: 18, t: 14, b: 40 };
    const pw = W - m.l - m.r;
    const ph = H - m.t - m.b;
    const doses = data.map((p) => p.dose);
    // log-scale domain padded so points never sit on the frame; at least one decade wide
    let la = Math.log10(Math.min(...doses)) - 0.2;
    let lb = Math.log10(Math.max(...doses)) + 0.2;
    if (lb - la < 1) { const mid = (la + lb) / 2; la = mid - 0.5; lb = mid + 0.5; }
    const lx = (d) => m.l + (Math.log10(d) - la) / (lb - la) * pw;
    const vals = data.map((p) => p.value);
    const vMax = Math.max(0, ...vals);
    const vMin = Math.min(0, ...vals);
    const ymin = vMin < 0 ? -niceMax(-vMin) : 0;
    let ymax = vMax > 0 ? niceMax(vMax) : 0;
    if (ymax <= ymin) ymax = ymin + 1;
    const y = (v) => m.t + ph - (v - ymin) / (ymax - ymin) * ph;
    const svg = svgEl('svg', { class: 'sp-svg', viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, role: 'group',
      'aria-label': 'Dose–effect scatter plot, ' + data.length + ' points' });
    const mults = lb - la <= 2.5 ? [1, 3] : [1];
    for (let e = Math.floor(la); e <= Math.ceil(lb); e++) {
      for (const k of mults) {
        const t = k * Math.pow(10, e);
        const lt = Math.log10(t);
        if (lt < la || lt > lb) continue;
        svg.appendChild(svgEl('line', { class: 'sp-grid', x1: lx(t), x2: lx(t), y1: m.t, y2: m.t + ph }));
        svg.appendChild(svgEl('text', { class: 'tm', x: lx(t), y: m.t + ph + 16, 'text-anchor': 'middle', text: fmtVal(t) }));
      }
    }
    for (let i = 0; i <= 4; i++) {
      const v = ymin + (ymax - ymin) * i / 4;
      svg.appendChild(svgEl('line', { class: 'sp-grid' + (v === 0 ? ' sp-base' : ''), x1: m.l, x2: m.l + pw, y1: y(v), y2: y(v) }));
      svg.appendChild(svgEl('text', { class: 'tm', x: m.l - 6, y: y(v), 'text-anchor': 'end', 'dominant-baseline': 'central', text: fmtVal(v) }));
    }
    svg.appendChild(svgEl('text', { class: 'tm', x: m.l + pw / 2, y: H - 4, 'text-anchor': 'middle', text: 'Dose (mg/kg)' }));
    svg.appendChild(svgEl('text', { class: 'tm', x: 12, y: m.t + ph / 2, 'text-anchor': 'middle', transform: 'rotate(-90 12 ' + (m.t + ph / 2) + ')', text: 'Effect (' + (spState.metric || '') + ')' }));
    const seen = new Map(); // nudge coincident points apart so each one stays visible and hoverable
    for (const p of data) {
      const key = Math.round(lx(p.dose) / 6) + ':' + Math.round(y(p.value) / 6);
      const k = seen.get(key) || 0;
      seen.set(key, k + 1);
      const px = lx(p.dose) + (k ? (k % 2 ? 1 : -1) * Math.ceil(k / 2) * 9 : 0);
      const py = y(p.value);
      const g = svgEl('g', { class: 'sp-mark sp-pt', tabindex: 0, role: p.url ? 'link' : 'img',
        'aria-label': nameOf(p.species) + ': ' + fmtVal(p.dose) + ' mg/kg, ' + fmtVal(p.value) + ' ' + p.unit + ', ' + p.ref });
      g.appendChild(svgEl('circle', { class: 'sp-hit', cx: px, cy: py, r: 12 }));
      if (p.source === 'paper') g.appendChild(svgEl('circle', { class: 'ring ' + slot(p.species), cx: px, cy: py, r: 6 }));
      else g.appendChild(svgEl('rect', { class: 'ring ' + slot(p.species), x: px - 5.5, y: py - 5.5, width: 11, height: 11, rx: 2 }));
      withTip(g, [nameOf(p.species), 'Dose: ' + fmtVal(p.dose) + ' mg/kg', 'Effect: ' + fmtVal(p.value) + ' ' + p.unit + (p.what ? ' — ' + clip(p.what, 90) : ''),
        (p.source === 'paper' ? 'Paper: ' : 'Measurement: ') + p.ref, p.title && p.source === 'paper' ? clip(p.title, 110) : null]);
      const url = safeUrl(p.url, false);
      if (url) activate(g, () => window.open(url, '_blank', 'noopener,noreferrer'));
      svg.appendChild(g);
    }
    return svg;
  }

  // ------------------------------------------------------------- measurements by kind
  function measureChart(c) {
    const species = orderedSpecies(c.species).filter((s) => (s.n_measurements || 0) > 0);
    const title = 'Measurements by kind';
    if (!species.length) return chartBlock(title, null, h('div', { class: 'empty', text: 'No ChEMBL measurements for this compound in the selected species.' }));
    const kindsPresent = SP_KIND_ORDER.filter((k) => species.some((s) => ((s.measurements_by_kind || {})[k] || 0) > 0));
    const countOf = (s, k) => (s.measurements_by_kind || {})[k] || 0;
    const total = (s) => SP_KIND_ORDER.reduce((a, k) => a + countOf(s, k), 0) || s.n_measurements || 0;
    const max = Math.max(1, ...species.map(total));
    const holder = responsive(h('div', { class: 'sp-chart-body' }), (W) => {
      const labelW = 130;
      const right = 44;
      const rowH = 30;
      const barH = 14;
      const H = 22 + species.length * rowH;
      const pw = Math.max(60, W - labelW - right);
      const svg = svgEl('svg', { class: 'sp-svg', viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, role: 'group',
        'aria-label': 'Stacked bars of measurement counts by kind per species' });
      const tickStep = niceMax(max / 4);
      for (let t = 0; t <= max + 1e-9; t += tickStep) {
        const x = labelW + t / max * pw;
        svg.appendChild(svgEl('line', { class: 'sp-grid', x1: x, x2: x, y1: 18, y2: H - 4 }));
        svg.appendChild(svgEl('text', { class: 'tm', x, y: 12, 'text-anchor': 'middle', text: fmtNum(t) }));
      }
      species.forEach((s, i) => {
        const y = 22 + i * rowH + (rowH - barH) / 2;
        svg.appendChild(rowLabel(s, labelW - 10, y + barH / 2));
        let x = labelW;
        const segs = kindsPresent.filter((k) => countOf(s, k) > 0);
        segs.forEach((k, j) => {
          const n = countOf(s, k);
          const w = n / max * pw;
          const last = j === segs.length - 1;
          const g = svgEl('g', { class: 'sp-mark', tabindex: 0, role: 'img', 'aria-label': spLabel(s) + ' ' + SP_KINDS[k][1] + ': ' + n });
          const ww = Math.max(1, w - (last ? 0 : 2)); // 2px surface gap between segments
          const rr = last ? Math.min(4, barH / 2, ww) : 0;
          g.appendChild(svgEl('path', {
            class: 'sp-bar sp-c' + (SP_KIND_ORDER.indexOf(k) + 1),
            d: 'M' + x + ',' + y + 'h' + (ww - rr) + (rr ? 'a' + rr + ',' + rr + ' 0 0 1 ' + rr + ',' + rr : '') + 'v' + (barH - 2 * rr) +
              (rr ? 'a' + rr + ',' + rr + ' 0 0 1 -' + rr + ',' + rr : '') + 'h-' + (ww - rr) + 'z',
          }));
          withTip(g, [spLabel(s), SP_KINDS[k][1] + ': ' + n]);
          svg.appendChild(g);
          x += w;
        });
        svg.appendChild(svgEl('text', { class: 't2 sp-val', x: x + 6, y: y + barH / 2, 'dominant-baseline': 'central', text: fmtNum(total(s)) }));
      });
      return svg;
    });
    const table = miniTable(['Species'].concat(kindsPresent.map((k) => SP_KINDS[k][1])).concat(['Total']),
      species.map((s) => [spLabel(s)].concat(kindsPresent.map((k) => fmtNum(countOf(s, k)))).concat([fmtNum(total(s))])),
      kindsPresent.map((_, i) => i + 1).concat([kindsPresent.length + 1]));
    return chartBlock(title, 'Number of ChEMBL bioactivity, PK, in vivo efficacy and toxicity records',
      h('div', null, legend(kindsPresent.map((k) => ['sp-bg-c' + (SP_KIND_ORDER.indexOf(k) + 1), SP_KINDS[k][1]])), holder), table);
  }

  // ------------------------------------------------------------- papers
  function paperItem(p, sIndex) {
    const url = p.url || pubmedUrl(p.pmid) || doiUrl(p.doi);
    const s = sIndex.get(p.species);
    const facts = [
      p.model ? ['Model', p.model] : null,
      p.dose ? ['Dose', p.dose + (isNum(p.dose_mg_kg) && !/mg\s*\/\s*kg/i.test(p.dose) ? ' (' + fmtVal(p.dose_mg_kg) + ' mg/kg)' : '')] : null,
      p.effect ? ['Effect', p.effect] : null,
      p.finding && p.finding !== p.effect ? ['Finding', p.finding] : null,
    ].filter(Boolean);
    return h('li', { class: 'sp-paper' },
      h('div', { class: 'sp-paper-title' }, url ? extLink(url, p.title || '(untitled)') : h('span', { text: p.title || '(untitled)' })),
      h('div', { class: 'sp-paper-meta small' },
        h('span', { class: 'badge outline', text: s ? spLabel(s) : String(p.species || '') }),
        evBadge(p.evidence),
        p.journal ? h('span', { class: 'muted', text: p.journal }) : null,
        p.year ? h('span', { class: 'muted', text: String(p.year) }) : null,
        p.pmid && pubmedUrl(p.pmid) ? extLink(pubmedUrl(p.pmid), 'PMID ' + p.pmid, 'id-chip') : null,
        p.doi && doiUrl(p.doi) ? extLink(doiUrl(p.doi), 'DOI ' + clip(p.doi, 40), 'id-chip') : null),
      facts.length ? h('dl', { class: 'sp-facts small' }, facts.map(([k, v]) => [h('dt', { text: k }), h('dd', { text: clip(v, 400) })])) : null);
  }

  function paperList(c, fixedSpecies) {
    const sIndex = speciesIndex(c);
    const papers = (c.papers || []).filter((p) => p && typeof p === 'object');
    const box = h('div');
    const list = h('ol', { class: 'sp-papers' });
    function draw() {
      const f = fixedSpecies || spState.paperFilter;
      const shown = papers.filter((p) => !f || p.species === f);
      list.replaceChildren(...shown.map((p) => paperItem(p, sIndex)));
      if (!shown.length) list.replaceChildren(h('li', { class: 'empty', text: 'No papers' + (f ? ' for this species' : '') + '.' }));
      if (chips) chips.querySelectorAll('.chip').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.key === (spState.paperFilter || ''))));
    }
    let chips = null;
    if (!fixedSpecies) {
      const counts = new Map();
      for (const p of papers) counts.set(p.species, (counts.get(p.species) || 0) + 1);
      const keys = [...sIndex.keys()].filter((k) => counts.has(k)).concat([...counts.keys()].filter((k) => !sIndex.has(k)));
      if (spState.paperFilter && !counts.has(spState.paperFilter)) spState.paperFilter = '';
      chips = h('div', { class: 'chips', role: 'group', 'aria-label': 'Filter papers by species' },
        [['', 'All', papers.length]].concat(keys.map((k) => [k, sIndex.has(k) ? spLabel(sIndex.get(k)) : k, counts.get(k)])).map(([k, label, n]) =>
          h('button', { type: 'button', class: 'chip', 'data-key': k, 'aria-pressed': 'false', onclick: () => { spState.paperFilter = k; draw(); } },
            label, h('span', { class: 'count', text: String(n) }))));
      box.appendChild(chips);
    }
    box.appendChild(list);
    draw();
    return box;
  }

  // ------------------------------------------------------------- species detail
  function speciesDetail(c, key, onClose) {
    const sIndex = speciesIndex(c);
    const s = sIndex.get(key);
    if (!s) return null;
    const orth = (c.similarity || []).filter((o) => o && o.species === key);
    const meas = (c.measurements || []).filter((m) => m && m.species === key);
    const nPapers = (c.papers || []).filter((p) => p && p.species === key).length;
    return h('section', { class: 'panel sp-detail', id: 'sp-detail', tabindex: '-1', 'aria-labelledby': 'sp-detail-title' },
      h('div', { class: 'panel-head row' },
        h('div', null, h('h3', { id: 'sp-detail-title', text: spLabel(s) }), h('div', { class: 'muted small' }, h('i', { text: s.scientific_name || '' }))),
        h('button', { type: 'button', class: 'btn btn-sm', onclick: onClose }, 'Close')),
      h('div', { class: 'tiles' },
        tile(key === 'human' ? '100%' : fmtPct(s.mean_identity_pct), 'Mean identity'),
        tile(fmtPct(meanSimilarity(c, key)), 'Similarity'),
        tile(fmtNum(s.n_papers || 0), 'PubMed papers'),
        tile(fmtNum(s.n_measurements || 0), 'ChEMBL measurements'),
        tile(isNum(s.median_pchembl) ? s.median_pchembl.toFixed(2) : '–', 'Median pChEMBL'),
        h('div', { class: 'tile-stat' }, h('div', { class: 'v sp-tile-ev' }, evBadge(s.evidence_level)), h('div', { class: 'k', text: 'Evidence level' }))),
      s.summary ? h('p', { class: 'sp-summary', text: s.summary }) : null,
      h('h4', { text: 'Orthologs' }),
      orth.length ? miniTable(['Human target', 'Ortholog', 'ID', 'UniProt', 'Identity', 'Similarity', 'Coverage', 'Relationship', 'Method', 'Note'],
        orth.map((o) => [o.target || '', o.ortholog_symbol || '–', o.ortholog_id ? (o.url ? extLink(o.url, o.ortholog_id) : o.ortholog_id) : '–',
          o.uniprot ? extLink(uniprotUrl(o.uniprot), o.uniprot) : '–',
          h('span', { class: 'sp-chip-pct sp-b' + idBin(o.identity_pct), text: fmtPct(o.identity_pct) }), fmtPct(o.similarity_pct), fmtPct(o.coverage_pct),
          o.relationship || '–', o.method || '–', o.note || '']), [4, 5, 6]) : h('p', { class: 'muted small', text: 'No ortholog rows.' }),
      h('h4', { text: 'Measurements (' + meas.length + ')' }),
      meas.length ? miniTable(['Kind', 'Type', 'Value', 'pChEMBL', 'Dose (mg/kg)', 'Target', 'Description', 'Source'],
        meas.slice(0, 200).map((m) => [(SP_KINDS[m.kind] || SP_KINDS.other)[1], m.standard_type || '–',
          ((m.relation && m.relation !== '=' ? m.relation + ' ' : '') + fmtVal(m.value) + ' ' + (m.units || '')).trim(),
          isNum(m.pchembl) ? m.pchembl.toFixed(2) : '–', fmtVal(m.dose_mg_kg), m.target || '–', clip(m.description || '', 160),
          m.url ? extLink(m.url, (m.source || 'chembl') + (m.source_id ? ' ' + m.source_id : '')) : (m.pmid && pubmedUrl(m.pmid) ? extLink(pubmedUrl(m.pmid), 'PMID ' + m.pmid) : (m.source || '') + (m.source_id ? ' ' + m.source_id : ''))]),
        [2, 3, 4]) : h('p', { class: 'muted small', text: 'No measurements.' }),
      h('h4', { text: 'Papers (' + nPapers + ' / PubMed ' + fmtNum(s.n_papers || 0) + ')' }),
      paperList(c, key));
  }

  // ------------------------------------------------------------- dashboard
  function compoundHeader(c) {
    const comp = c.compound || {};
    const ids = comp.identifiers && typeof comp.identifiers === 'object' ? comp.identifiers : {};
    const phase = isNum(comp.max_phase) ? comp.max_phase : null;
    return h('section', { class: 'panel sp-header' },
      h('div', { class: 'sp-title-row' },
        h('h2', { class: 'sp-compound', text: comp.name || c.query || '' }),
        h('span', { class: 'badge accent', text: SP_COMPOUND_KIND[comp.kind] || SP_COMPOUND_KIND.unknown }),
        phase !== null ? h('span', { class: 'badge ' + (phase >= 4 ? 'ok' : 'outline'), title: 'ChEMBL max phase' },
          'Max phase ' + phase + (phase >= 4 ? ' · approved' : '')) : null,
        comp.url ? extLink(comp.url, 'Source ↗', 'small') : null),
      h('div', { class: 'muted small' }, 'Query: ', h('code', { text: c.query || '' }), ' · ', fmtTime(c.created_at)),
      Object.keys(ids).length ? h('div', { class: 'id-chips sp-ids' }, Object.keys(ids).map((k) => {
        const v = String(ids[k]);
        const link = ID_LINKS[k] ? ID_LINKS[k](v) : null;
        const chip = [h('b', { text: k + ' ' }), v];
        return link ? h('a', { class: 'id-chip', href: link, target: '_blank', rel: 'noopener noreferrer' }, chip) : h('span', { class: 'id-chip' }, chip);
      })) : null,
      h('h3', { class: 'sp-sub', text: 'Human targets (' + (c.targets || []).length + ')' }),
      (c.targets || []).length ? h('ul', { class: 'sp-targets' }, (c.targets || []).map((t) => h('li', null,
        h('strong', { text: t.gene_symbol || '?' }),
        t.name ? h('span', { class: 'muted', text: ' ' + t.name }) : null,
        t.action ? h('span', { class: 'badge outline', text: t.action }) : null,
        t.uniprot && uniprotUrl(t.uniprot) ? extLink(uniprotUrl(t.uniprot), 'UniProt ' + t.uniprot, 'id-chip') : null,
        t.chembl_target ? extLink('https://www.ebi.ac.uk/chembl/explore/target/' + encodeURIComponent(t.chembl_target), t.chembl_target, 'id-chip') : null,
        h('span', { class: 'muted small', text: ' via ' + (t.source || '') })))) :
        h('p', { class: 'note-box', text: 'No targets found, so no sequence similarity could be computed.' }));
  }

  function methodsBlock(c) {
    const methods = c.methods && typeof c.methods === 'object' ? c.methods : {};
    const errors = c.errors && typeof c.errors === 'object' ? c.errors : {};
    const notes = Array.isArray(c.notes) ? c.notes : [];
    return h('section', { class: 'panel' },
      h('h3', { text: 'Methods, notes and errors' }),
      notes.map((n) => h('p', { class: 'note-box', text: String(n) })),
      Object.keys(methods).length ? h('dl', { class: 'sp-facts small' }, Object.keys(methods).map((k) => [h('dt', { text: humanKey(k) }), h('dd', { text: String(methods[k]) })])) : null,
      Object.keys(errors).length ? h('div', null, h('h4', { text: 'Failed sources' }),
        Object.keys(errors).map((k) => h('div', { class: 'error-box small' }, h('strong', { text: k + ': ' }), String(errors[k])))) : null,
      h('p', { class: 'muted small', text: 'Similarity = sequence identity (% identical residues) and similarity (% residues with BLOSUM62 > 0) of each ortholog relative to the human target protein.' }));
  }

  function renderDashboard(c) {
    const box = $('#sp-dashboard');
    hideTip();
    const sIndex = speciesIndex(c);
    if (spState.selected && !sIndex.has(spState.selected)) spState.selected = null;
    const species = orderedSpecies(c.species);
    const totalPapers = species.reduce((a, s) => a + (s.n_papers || 0), 0);
    const totalMeas = species.reduce((a, s) => a + (s.n_measurements || 0), 0);
    const detailSlot = h('div', { id: 'sp-detail-slot' });
    const graphHolder = h('div', { class: 'sp-graph-wrap' });
    const heatSlot = h('div');
    function select(key) {
      spState.selected = spState.selected === key ? null : key;
      drawSelected(true);
    }
    function closeDetail() {
      const key = spState.selected;
      spState.selected = null;
      drawSelected(false);
      // return keyboard focus to the species node that opened the panel
      const n = key && [...graphHolder.querySelectorAll('.sp-node')].find((g) => g.getAttribute('data-species') === key);
      if (n) n.focus({ preventScroll: true });
    }
    function drawSelected(focus) {
      graphHolder.replaceChildren(species.length ? overviewGraph(c.species, { onSelect: select, selected: spState.selected })
        : h('div', { class: 'empty', text: 'No species in this comparison.' }));
      heatSlot.replaceChildren(heatTable(c, (k) => { spState.selected = k; drawSelected(true); }));
      const d = spState.selected ? speciesDetail(c, spState.selected, closeDetail) : null;
      if (d) d.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); closeDetail(); } });
      detailSlot.replaceChildren(...(d ? [d] : []));
      if (d && focus) { d.focus({ preventScroll: true }); d.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }
    }
    const pkg = c.package && typeof c.package === 'object' ? c.package : null;
    box.replaceChildren(
      compoundHeader(c),
      h('div', { class: 'tiles' },
        tile(fmtNum((c.targets || []).length), 'Targets'),
        tile(fmtNum(species.length), 'Species'),
        tile(fmtNum(totalPapers), 'PubMed papers'),
        tile(fmtNum(totalMeas), 'ChEMBL measurements')),
      h('section', { class: 'panel' },
        h('div', { class: 'panel-head' }, h('h3', { text: 'Overview graph' }),
          h('p', { class: 'muted small', text: 'Human at the centre; species in evolutionary order. Click a species for details.' })),
        graphHolder, graphLegend()),
      detailSlot,
      h('section', { class: 'panel' },
        h('div', { class: 'panel-head' }, h('h3', { text: 'Comparison table (heatmap)' }),
          h('p', { class: 'muted small', text: 'Click a column header to sort, a species name for details.' })),
        heatSlot),
      h('div', { class: 'sp-charts' }, identityBars(c), measureChart(c)),
      doseScatter(c),
      h('section', { class: 'panel' },
        h('div', { class: 'panel-head' }, h('h3', { text: 'Papers (' + (c.papers || []).length + ')' }),
          h('p', { class: 'muted small', text: 'Top PubMed papers per species.' })),
        paperList(c)),
      h('section', { class: 'sp-package' },
        h('h3', { text: 'Data package' }),
        pkg ? packageCard(pkg) : h('p', { class: 'muted small', text: 'No data package was written for this comparison.' })),
      methodsBlock(c));
    drawSelected(false);
  }

  // ------------------------------------------------------------- form, history, routing
  function loadSpeciesMeta() {
    if (!spState.metaPromise) {
      spState.metaPromise = api('/api/species/meta').then((m) => { spState.meta = m; return m; })
        .catch((err) => { spState.metaPromise = null; throw err; });
    }
    return spState.metaPromise;
  }

  function buildSpeciesForm(meta) {
    const list = Array.isArray(meta && meta.species) ? meta.species : [];
    const defaults = new Set(Array.isArray(meta && meta.default) ? meta.default : list.map((s) => s.key));
    const box = $('#sp-species-groups');
    box.replaceChildren(...SP_FORM_GROUPS.map(([, en, groups]) => {
      const members = list.filter((s) => groups.includes(s.group));
      if (!members.length) return null;
      return h('div', { class: 'sp-group' }, h('div', { class: 'sp-group-h', text: en }),
        members.map((s) => h('label', { class: 'sp-opt', title: s.scientific_name },
          h('input', { type: 'checkbox', value: s.key, checked: defaults.has(s.key) }), ' ' + spLabel(s))));
    }).filter(Boolean));
  }

  async function loadSpeciesHistory() {
    const ul = $('#sp-history');
    try {
      const data = await api('/api/species?limit=50');
      const items = Array.isArray(data && data.comparisons) ? data.comparisons : [];
      if (!items.length) { ul.replaceChildren(h('li', { class: 'muted small', text: 'No comparisons yet.' })); return; }
      const cur = spState.current && spState.current.comparison_id;
      ul.replaceChildren(...items.filter((it) => SP_ID_RE.test(it.comparison_id || '')).map((it) => h('li', null,
        h('a', { href: '#species?id=' + it.comparison_id, class: 'sp-hist' + (it.comparison_id === cur ? ' active' : ''), 'aria-current': it.comparison_id === cur ? 'true' : null },
          h('span', { class: 'sp-hist-name', text: it.compound || it.query || it.comparison_id }),
          h('span', { class: 'muted small', text: [(it.targets || []).slice(0, 4).join(', '), (it.n_species || 0) + ' species', relTime(it.created_at)].filter(Boolean).join(' · ') })))));
    } catch (err) {
      ul.replaceChildren(h('li', null, errorBox(err)));
    }
  }

  function emptyDashboard() {
    $('#sp-dashboard').replaceChildren(h('div', { class: 'empty sp-empty' },
      h('p', { text: 'Enter a drug or compound and press Run, or open a previous comparison.' })));
  }

  async function openComparison(id, sp) {
    const box = $('#sp-dashboard');
    if (!spState.current || spState.current.comparison_id !== id) {
      spState.selected = null;
      spState.paperFilter = '';
      spState.metric = null;
      spState.sort = { key: 'order', dir: 1 };
    }
    if (sp) spState.selected = sp;
    const token = (spState.openToken = (spState.openToken || 0) + 1);
    let c = spState.cache.get(id);
    if (!c) {
      box.replaceChildren(h('div', { class: 'typing' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Loading comparison…'));
      try {
        c = await api('/api/species/' + id);
        spState.cache.set(id, c);
      } catch (err) {
        if (token === spState.openToken) box.replaceChildren(errorBox(err));
        return;
      }
      if (token !== spState.openToken) return; // a newer comparison was opened meanwhile
    }
    spState.current = c;
    renderDashboard(c);
    loadSpeciesHistory();
  }

  function hashParams() {
    const q = (location.hash || '').split('?')[1] || '';
    try { return new URLSearchParams(q); } catch (_) { return new URLSearchParams(); }
  }

  async function runComparison(e) {
    e.preventDefault();
    if (spState.running) return;
    const query = $('#sp-q').value.trim();
    if (!query) { $('#sp-q').focus(); return; }
    const species = [...document.querySelectorAll('#sp-species-groups input[type=checkbox]:checked')].map((i) => i.value);
    if (!species.length) { toast('Select at least one species', true); return; }
    const status = $('#sp-status');
    const btn = $('#sp-run');
    const started = Date.now();
    const steps = ['resolving the compound', 'finding human targets', 'aligning orthologs',
      'searching papers per species', 'collecting measurements', 'writing the data package'];
    const tick = () => {
      const s = Math.round((Date.now() - started) / 1000);
      const stepText = steps[Math.min(steps.length - 1, Math.floor(s / 12))];
      status.replaceChildren(h('span', { class: 'spinner', 'aria-hidden': 'true' }),
        h('span', { text: ' Comparing… ' + s + ' s (usually 30–90 s) · ' + stepText }));
    };
    spState.running = true;
    btn.disabled = true;
    tick();
    const timer = setInterval(tick, 1000);
    try {
      const res = await api('/api/species/compare', {
        method: 'POST',
        body: { query, species, max_papers: Number($('#sp-papers').value) || 5, extract_with_claude: $('#sp-claude').checked },
      });
      status.replaceChildren(h('span', { class: 'badge ok', text: 'done' }), ' ' + Math.round((Date.now() - started) / 1000) + ' s');
      if (res && SP_ID_RE.test(res.comparison_id || '')) {
        spState.cache.set(res.comparison_id, res);
        const target = '#species?id=' + res.comparison_id;
        if ($('#view-species').hidden) {
          // the user moved to another tab during the run: do not pull them back, just announce it
          toast('Cross-species comparison ready: ' + ((res.compound && res.compound.name) || query));
          loadSpeciesHistory();
        } else if (location.hash === target) openComparison(res.comparison_id); else location.hash = target;
      } else if (res) {
        spState.current = res;
        renderDashboard(res);
      }
    } catch (err) {
      status.replaceChildren(errorBox(err));
    } finally {
      clearInterval(timer);
      spState.running = false;
      btn.disabled = false;
    }
  }

  function initSpecies() {
    $('#sp-form').addEventListener('submit', runComparison);
    $('#sp-all').addEventListener('click', () => document.querySelectorAll('#sp-species-groups input').forEach((i) => { i.checked = true; }));
    $('#sp-none').addEventListener('click', () => document.querySelectorAll('#sp-species-groups input').forEach((i) => { i.checked = false; }));
    $('#sp-history-refresh').addEventListener('click', loadSpeciesHistory);
    $('#main').addEventListener('scroll', hideTip, { passive: true });
    window.addEventListener('hashchange', hideTip);
  }

  loaders.species = (first) => {
    if (first) {
      loadSpeciesMeta().then(buildSpeciesForm).catch((err) => $('#sp-species-groups').replaceChildren(errorBox(err)));
    }
    const params = hashParams();
    const id = params.get('id');
    const sp = params.get('sp');
    if (id && SP_ID_RE.test(id)) {
      openComparison(id, sp && /^[a-z_]{1,32}$/.test(sp) ? sp : null);
    } else {
      if (first || !spState.current) emptyDashboard();
      loadSpeciesHistory();
    }
  };

  // ------------------------------------------------------------- chat card
  /** Compact card for a ``species_comparison`` chat event (live stream or restored transcript). */
  function speciesChatCard(ev) {
    const sum = ev && typeof ev.summary === 'object' && ev.summary ? ev.summary : {};
    const id = SP_ID_RE.test(String(ev && ev.comparison_id || '')) ? ev.comparison_id : null;
    const href = id ? '/#species?id=' + id : null;
    const species = orderedSpecies(sum.species);
    const targets = Array.isArray(sum.targets) ? sum.targets.map(String) : [];
    const pkg = sum.package && typeof sum.package === 'object' ? sum.package : null;
    const graph = species.length > 1 ? overviewGraph(species, {
      compact: true,
      onSelect: id ? (k) => { location.hash = '#species?id=' + id + '&sp=' + encodeURIComponent(k); } : null,
    }) : null;
    const rows = species.map((s) => h('tr', null,
      h('th', { scope: 'row', text: spLabel(s) }),
      h('td', { class: 'num' }, s.species === 'human' ? 'ref' : h('span', { class: 'sp-chip-pct sp-b' + idBin(s.mean_identity_pct), text: fmtPct(s.mean_identity_pct) })),
      h('td', { class: 'num', text: fmtNum(s.n_papers || 0) }),
      h('td', null, evBadge(s.evidence_level))));
    return h('div', { class: 'sp-card', role: 'group', 'aria-label': 'Cross-species comparison' },
      h('div', { class: 'sp-card-head' },
        h('div', null,
          h('div', { class: 'sp-card-kicker', text: 'Cross-species comparison' }),
          h('div', { class: 'sp-card-title', text: sum.compound || ev.title || '' }),
          targets.length ? h('div', { class: 'small' }, 'Targets: ', targets.slice(0, 8).map((t) => h('span', { class: 'id-chip', text: t }))) : null),
        href ? h('a', { class: 'btn btn-sm btn-primary', href }, 'Open full dashboard') : null),
      h('div', { class: 'sp-card-body' },
        graph ? h('div', { class: 'sp-card-graph' }, graph) : null,
        rows.length ? h('div', { class: 'table-wrap sp-card-table' }, h('table', { class: 'data' },
          h('thead', null, h('tr', null, ['Species', 'Identity', 'Papers', 'Evidence'].map((t, i) => h('th', { scope: 'col', class: i === 1 || i === 2 ? 'num' : null, text: t })))),
          h('tbody', null, rows))) : h('p', { class: 'muted small', text: 'No species data.' })),
      pkg && (pkgUrl(pkg.xlsx_url) || pkgUrl(pkg.zip_url)) ? h('div', { class: 'sp-card-foot small' }, 'Data package: ',
        pkgUrl(pkg.xlsx_url) ? h('a', { href: pkgUrl(pkg.xlsx_url), download: '' }, 'Excel') : null, ' ',
        pkgUrl(pkg.zip_url) ? h('a', { href: pkgUrl(pkg.zip_url), download: '' }, 'ZIP') : null,
        ' · ' + (Array.isArray(pkg.files) ? pkg.files.length : 0) + ' CSV') : null);
  }

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
    initSpecies();
    window.addEventListener('hashchange', route);
    refreshHealth();
    loadSources().catch(() => {});
    setInterval(refreshHealth, 60000);
    route();
  }

  // Exposed for debugging in the browser console only (no side effects).
  window.MosaicUI = { renderMarkdown, inline, safeUrl, speciesChatCard };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
