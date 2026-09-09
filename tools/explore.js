/* Structural reconnaissance: find the conversation list and the message list
 * without knowing any of Reddit's class names, by looking for containers whose
 * children repeat. Both lists are, structurally, "many similar siblings". */
(() => {
  const txt = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();

  const deepAll = (sel, root = document) => {
    const out = [];
    const visit = (n) => {
      try { out.push(...n.querySelectorAll(sel)); } catch (e) {}
      for (const el of n.querySelectorAll('*')) if (el.shadowRoot) visit(el.shadowRoot);
    };
    visit(root);
    return Array.from(new Set(out));
  };

  const sig = (el) => {
    const cls = (typeof el.className === 'string' ? el.className : '')
      .split(/\s+/).filter(Boolean).slice(0, 3).join('.');
    return el.tagName.toLowerCase() + (cls ? '.' + cls : '');
  };

  const path = (el) => {
    const parts = [];
    let cur = el;
    for (let i = 0; i < 6 && cur && cur.tagName; i += 1) {
      parts.unshift(sig(cur));
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  };

  const attrs = (el) => {
    const o = {};
    for (const a of el.attributes || []) {
      if (a.name === 'style') continue;
      o[a.name] = a.value.length > 90 ? a.value.slice(0, 90) + '…' : a.value;
    }
    return o;
  };

  /** Containers whose direct children repeat the same signature >= min times. */
  const repeatedGroups = (min = 4) => {
    const groups = [];
    for (const parent of deepAll('*')) {
      const kids = Array.from(parent.children);
      if (kids.length < min) continue;
      const counts = {};
      for (const k of kids) counts[sig(k)] = (counts[sig(k)] || 0) + 1;
      for (const [s, n] of Object.entries(counts)) {
        if (n < min) continue;
        const sample = kids.filter((k) => sig(k) === s);
        const r = parent.getBoundingClientRect();
        groups.push({
          childSig: s,
          count: n,
          parentPath: path(parent),
          parentRect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
          parentScrolls: parent.scrollHeight > parent.clientHeight + 20,
          sampleAttrs: sample.slice(0, 2).map(attrs),
          sampleText: sample.slice(0, 4).map((k) => txt(k).slice(0, 90)),
          sampleHasImg: sample.slice(0, 8).filter((k) => k.querySelector('img')).length,
          sampleHref: sample.slice(0, 4).map((k) => {
            const a = k.matches('a[href]') ? k : k.querySelector('a[href]');
            return a ? a.getAttribute('href') : null;
          }),
        });
      }
    }
    return groups.sort((a, b) => b.count - a.count).slice(0, 25);
  };

  const scrollers = () => deepAll('*')
    .filter((el) => {
      const s = getComputedStyle(el);
      return /auto|scroll|overlay/.test(s.overflowY)
        && el.scrollHeight > el.clientHeight + 40 && el.clientHeight > 120;
    })
    .map((el) => {
      const r = el.getBoundingClientRect();
      return {
        path: path(el), attrs: attrs(el),
        rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
        clientHeight: el.clientHeight, scrollHeight: el.scrollHeight, scrollTop: Math.round(el.scrollTop),
        childCount: el.children.length,
        firstChildSig: el.children[0] ? sig(el.children[0]) : null,
      };
    })
    .sort((a, b) => b.scrollHeight - a.scrollHeight)
    .slice(0, 12);

  const images = () => deepAll('img')
    .map((i) => {
      const r = i.getBoundingClientRect();
      return { src: (i.currentSrc || i.src || '').slice(0, 130), alt: i.getAttribute('alt'),
               w: Math.round(r.width), h: Math.round(r.height), path: path(i) };
    })
    .filter((i) => i.w >= 40 && i.h >= 40)
    .slice(0, 25);

  const buttons = () => deepAll('button, [role="button"]')
    .filter((b) => b.getBoundingClientRect().width > 0)
    .map((b) => ({ label: b.getAttribute('aria-label'), text: txt(b).slice(0, 30), path: path(b) }))
    .slice(0, 40);

  window.__EXPLORE = {
    all: (min) => ({
      url: location.href,
      title: document.title,
      customTags: Array.from(new Set(deepAll('*').map((e) => e.tagName.toLowerCase()).filter((t) => t.includes('-')))).slice(0, 60),
      testids: Array.from(new Set(deepAll('[data-testid]').map((e) => e.getAttribute('data-testid')))).slice(0, 60),
      scrollers: scrollers(),
      repeatedGroups: repeatedGroups(min || 4),
      images: images(),
      buttons: buttons(),
    }),
    repeatedGroups, scrollers, images, buttons, path, attrs, txt, deepAll,
  };
})();
