/* Progressive enhancement only: stored messages and API payloads stay untouched. */
(function (root) {
  'use strict';
  function inspect(raw) {
    try {
      var value = JSON.parse(raw);
      if (value === null || typeof value !== 'object') return null;
      return { label: !Array.isArray(value) && typeof value.type === 'string'
        ? value.type.slice(0, 80) : '' };
    } catch (_) { return null; }
  }

  // Tokenize the original source, never stringify parsed numbers: IDs can be
  // larger than Number.MAX_SAFE_INTEGER, and duplicate keys must survive too.
  function tokens(raw) {
    var parts = raw.match(/"(?:\\[\s\S]|[^"\\])*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null|[{}\[\],:]/g) || [];
    var depth = 0, result = [];
    function add(text, kind) { result.push({ text: text, kind: kind || '' }); }
    function newline() { add('\n' + '  '.repeat(Math.min(depth, 16))); }
    parts.forEach(function (part, i) {
      if (part === '{' || part === '[') {
        add(part); depth++;
        if (parts[i + 1] !== '}' && parts[i + 1] !== ']') newline();
      } else if (part === '}' || part === ']') {
        depth--;
        if (parts[i - 1] !== '{' && parts[i - 1] !== '[') newline();
        add(part);
      } else if (part === ',') { add(part); newline(); }
      else if (part === ':') add(': ');
      else add(part, part[0] === '"' ? (parts[i + 1] === ':' ? 'key' : 'string')
        : /^(true|false|null)$/.test(part) ? 'literal' : 'number');
    });
    return result;
  }

  function split(text) {
    if (inspect(text)) return [{ json: text }];
    var fence = /^ {0,3}(`{3,}|~{3,})([^\r\n]*)\r?\n([\s\S]*?)^ {0,3}\1[ \t]*(?=\r?$)/gm;
    var result = [], cursor = 0, match;
    while ((match = fence.exec(text))) {
      if (!/^(json)?$/i.test(match[2].trim()) || !inspect(match[3])) continue;
      if (match.index > cursor) result.push({ text: text.slice(cursor, match.index) });
      result.push({ json: match[3].trim() });
      cursor = fence.lastIndex;
    }
    if (cursor < text.length) result.push({ text: text.slice(cursor) });
    return result;
  }

  function block(raw) {
    var info = inspect(raw);
    if (!info) return null;
    var lang = document.documentElement.lang;
    var labels = lang === 'ru' ? ['Копировать JSON', 'Скопировано', 'Не удалось скопировать — выделите JSON вручную']
      : lang.indexOf('zh') === 0 ? ['复制 JSON', '已复制', '复制失败，请手动选择 JSON']
      : ['Copy JSON', 'Copied', 'Copy failed — select JSON manually'];
    var details = document.createElement('details');
    details.className = 'json-block';
    var summary = document.createElement('summary');
    summary.textContent = 'JSON' + (info.label ? ' · ' + info.label : '');
    details.appendChild(summary);
    var toolbar = document.createElement('div');
    toolbar.className = 'json-toolbar';
    var status = document.createElement('span');
    status.className = 'json-status';
    status.setAttribute('role', 'status');
    var copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'json-copy';
    copy.textContent = labels[0];
    copy.addEventListener('click', async function () {
      try { await navigator.clipboard.writeText(raw); status.textContent = labels[1]; }
      catch (_) { status.textContent = labels[2]; }
    });
    toolbar.appendChild(status);
    toolbar.appendChild(copy);
    details.appendChild(toolbar);
    var pre = document.createElement('pre');
    pre.className = 'json-code';
    pre.tabIndex = 0;
    pre.setAttribute('aria-label', 'JSON');
    var code = document.createElement('code');
    tokens(raw).forEach(function (token) {
      var node = document.createElement('span');
      if (token.kind) node.className = 'json-' + token.kind;
      node.textContent = token.text;
      code.appendChild(node);
    });
    pre.appendChild(code);
    details.appendChild(pre);
    return details;
  }

  function enhance(message) {
    var text = message.querySelector('.text');
    if (text && !text.dataset.jsonEnhanced) {
      var parts = split(text.textContent);
      if (parts.some(function (part) { return part.json !== undefined; })) {
        text.textContent = '';
        parts.forEach(function (part) {
          text.appendChild(part.json !== undefined ? block(part.json) : document.createTextNode(part.text));
        });
      }
      text.dataset.jsonEnhanced = 'true';
    }
    message.querySelectorAll('pre.message-data').forEach(function (pre) {
      var details = block(pre.textContent);
      if (details) pre.replaceWith(details);
    });
  }
  var api = { inspect: inspect, tokens: tokens, split: split, enhance: enhance };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.RoomcommJSON = api;
})(typeof window !== 'undefined' ? window : globalThis);
