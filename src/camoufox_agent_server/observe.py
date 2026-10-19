"""In-page observation source.

The snapshot function is stringified and evaluated in the page, so it must be
fully self-contained. It annotates interactive elements with ``data-cf-ref``
attributes, which the action layer resolves back to Playwright locators.
"""

from __future__ import annotations

REF_ATTR = "data-cf-ref"

# Runs in the main world. Receives {maxNodes, includeText, scope} and returns a
# compact tree of the parts of the page an agent can act on.
SNAPSHOT_JS = r"""
(opts) => {
  const { maxNodes = 250, includeText = true, scope = null } = opts || {};
  const REF_ATTR = 'data-cf-ref';
  const root = scope ? document.querySelector(scope) : document;

  const isVisible = (el) => {
    if (el.hasAttribute('hidden')) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return false;
    const r = el.getBoundingClientRect();
    return !(r.width === 0 && r.height === 0);
  };

  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
    if (tag === 'button') return 'button';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'submit' || t === 'button' || t === 'reset' || t === 'image') return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'range') return 'slider';
      return 'textbox';
    }
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'img') return 'img';
    if (tag === 'form') return 'form';
    if (/^h[1-6]$/.test(tag)) return 'heading';
    if (tag === 'nav') return 'navigation';
    if (tag === 'main') return 'main';
    if (tag === 'header') return 'banner';
    if (tag === 'footer') return 'contentinfo';
    if (tag === 'table') return 'table';
    if (tag === 'iframe') return 'iframe';
    return 'generic';
  };

  const INTERACTIVE = new Set([
    'button', 'link', 'textbox', 'checkbox', 'radio', 'slider',
    'combobox', 'menuitem', 'option', 'tab', 'switch', 'searchbox',
  ]);

  const accessibleName = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const joined = labelledby.split(/\s+/).map((id) => {
        const t = document.getElementById(id);
        return t ? t.textContent : '';
      }).join(' ').trim();
      if (joined) return joined;
    }
    if (el.labels && el.labels.length) {
      const t = Array.from(el.labels).map((l) => l.textContent).join(' ').trim();
      if (t) return t;
    }
    const ph = el.getAttribute('placeholder');
    if (ph) return ph.trim();
    const title = el.getAttribute('title');
    if (title) return title.trim();
    if (el.tagName === 'IMG') return (el.getAttribute('alt') || '').trim();
    if (el.tagName === 'INPUT' && (el.type === 'submit' || el.type === 'button') && el.value) {
      return el.value;
    }
    const txt = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    return txt ? txt.slice(0, 120) : '';
  };

  // Continue numbering after the highest ref already present so a newly added
  // element can never collide with an existing ref when DOM order changes.
  let refCounter = 0;
  for (const el of document.querySelectorAll('[' + REF_ATTR + ']')) {
    const n = Number((el.getAttribute(REF_ATTR) || '').replace(/^e/, ''));
    if (Number.isFinite(n) && n > refCounter) refCounter = n;
  }

  const out = [];
  const counts = {};
  let truncated = false;
  const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'SVG', 'PATH']);

  const tag = (el, role) => {
    let ref = el.getAttribute(REF_ATTR);
    if (!ref) {
      refCounter += 1;
      ref = 'e' + refCounter;
      el.setAttribute(REF_ATTR, ref);
    }
    out.push({ ref, role });
    return out[out.length - 1];
  };

  const visit = (el) => {
    if (truncated || SKIP_TAGS.has(el.tagName)) return;
    const role = roleOf(el);
    const visible = isVisible(el);
    const interactive = INTERACTIVE.has(role) || el.hasAttribute('contenteditable')
      || el.onclick != null;
    const interesting = interactive || role !== 'generic';

    if (interesting && visible) {
      if (out.length >= maxNodes) {
        truncated = true;
        return;
      }
      const entry = tag(el, role);
      const name = accessibleName(el);
      if (name) entry.name = name;
      if (role === 'textbox' || role === 'searchbox' || role === 'combobox') {
        const value = 'value' in el ? el.value : '';
        if (value) entry.value = String(value).slice(0, 120);
      }
      if (role === 'checkbox' || role === 'radio' || role === 'switch') {
        entry.checked = !!el.checked || el.getAttribute('aria-checked') === 'true';
      }
      if (role === 'combobox' && el.tagName === 'SELECT') {
        entry.value = el.value;
        entry.options = Array.from(el.options).slice(0, 30).map((o) => o.value);
      }
      if (role === 'link') {
        const href = el.getAttribute('href');
        if (href) entry.href = href.slice(0, 200);
      }
      if (role === 'heading') entry.level = Number(el.tagName.slice(1)) || undefined;
      if (el.disabled || el.getAttribute('aria-disabled') === 'true') entry.disabled = true;
      if (includeText && !name) {
        const direct = Array.from(el.childNodes)
          .filter((n) => n.nodeType === 3)
          .map((n) => n.textContent)
          .join(' ')
          .replace(/\s+/g, ' ')
          .trim();
        if (direct) entry.text = direct.slice(0, 160);
      }
      counts[role] = (counts[role] || 0) + 1;
    }
    for (const child of el.children) visit(child);
  };

  for (const child of root ? root.children : []) visit(child);

  return {
    url: location.href,
    title: document.title,
    scroll: {
      y: Math.round(window.scrollY),
      maxY: Math.max(0, Math.round(document.documentElement.scrollHeight - window.innerHeight)),
    },
    counts,
    nodes: out,
    truncated: truncated || undefined,
  };
}
"""

# Scroll position, used by the `scroll` action result.
SCROLL_POSITION_JS = r"""
() => ({
  x: Math.round(window.scrollX || 0),
  y: Math.round(window.scrollY || 0),
  maxY: Math.max(0, Math.round(document.documentElement.scrollHeight - window.innerHeight)),
})
"""
