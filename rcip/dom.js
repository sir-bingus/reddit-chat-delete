/*
 * Injected into reddit.com/chat. All DOM knowledge lives here.
 *
 * Verified structure (Sep 2026):
 *
 *   rs-rooms-nav > rs-virtual-scroll            <- sidebar scroller (virtualised)
 *     rs-rooms-nav-room[room="!id:reddit.com"]  <- one conversation, shadow DOM
 *
 *   rs-timeline[room] > div.overflow-y-auto     <- timeline scroller
 *     rs-virtual-scroll-dynamic[itemselector="rs-timeline-event"]
 *       rs-timeline-event[data-id="$eventId"]   <- one message, shadow DOM
 *         div.room-message.regular|compact
 *           div.room-message-body.image-message <- image attachment
 *             div.room-message-image > rs-image > img
 *
 * Both lists are virtualised: only ~20 rows exist at a time, so everything is
 * keyed on the server-side ids (room="!…", data-id="$…") which stay stable as
 * nodes mount and unmount.
 */
(() => {
  if (window.__RCIP && !window.__RCIP_RELOAD) return;

  /** querySelectorAll that descends through open shadow roots. */
  const deep = (sel, root = document) => {
    const out = [];
    const visit = (n) => {
      try { out.push(...n.querySelectorAll(sel)); } catch (e) { /* bad selector */ }
      if (n.shadowRoot) visit(n.shadowRoot);
      for (const el of n.querySelectorAll('*')) if (el.shadowRoot) visit(el.shadowRoot);
    };
    visit(root);
    return Array.from(new Set(out));
  };

  const cssEscape = (s) => String(s).replace(/["\\]/g, '\\$&');

  const box = (el) => {
    const r = el.getBoundingClientRect();
    return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
  };
  const shown = (el) => el && el.isConnected && box(el).w > 0 && box(el).h > 0;
  const txt = (el) => (el ? (el.textContent || '') : '').replace(/\s+/g, ' ').trim();

  /* ------------------------------------------------------------------ rooms */

  const sidebarScroller = () => deep('rs-rooms-nav rs-virtual-scroll')[0] || deep('rs-virtual-scroll')[0];

  /** Conversations currently mounted in the sidebar.
   *
   * The row's anchor carries aria-label="Direct chat with <username>" (or the
   * group equivalent), which is the only trustworthy name source: the visible
   * row text starts with a date on some rows, and its message preview can
   * mention *other* usernames, which must never drive the protected list.
   */
  const visibleRooms = () => deep('rs-rooms-nav-room').map((r) => {
    const root = r.shadowRoot || r;
    const a = root.querySelector('a[aria-label]')
      || deep('a[aria-label]', r)[0]
      || deep('[aria-label*="chat with" i]', r)[0];
    const aria = a ? a.getAttribute('aria-label') : '';
    const direct = /^\s*direct chat with\s+(\S+)/i.exec(aria || '');
    return {
      room: r.getAttribute('room'),
      user: direct ? direct[1] : '',
      // participant names only - never the message preview
      label: aria || '',
      preview: txt(root).slice(0, 100),   // logging/diagnostics only
      isGroup: !!aria && !direct,
      selected: r.hasAttribute('selected'),
    };
  }).filter((r) => r.room);

  const scrollSidebar = (px) => {
    const s = sidebarScroller();
    if (!s) return null;
    const before = s.scrollTop;
    s.scrollTop = Math.min(s.scrollHeight, s.scrollTop + (px == null ? s.clientHeight * 0.8 : px));
    return {
      before: Math.round(before), after: Math.round(s.scrollTop),
      scrollHeight: Math.round(s.scrollHeight), clientHeight: Math.round(s.clientHeight),
      atBottom: s.scrollTop + s.clientHeight >= s.scrollHeight - 4,
      moved: s.scrollTop > before,
    };
  };

  /** Bring the last mounted conversation row into view so more mount below it.
   *  Element-based: no pixel arithmetic, no assumptions about row height. */
  const advanceSidebar = () => {
    const rows = deep('rs-rooms-nav-room');
    if (!rows.length) return null;
    const last = rows[rows.length - 1];
    last.scrollIntoView({ block: 'end' });
    return { rows: rows.length, lastRoom: last.getAttribute('room') };
  };

  /** Open a conversation by clicking its row, which keeps the SPA (and the
   *  sidebar's scroll position) intact - unlike navigating to its URL. */
  const openRoomByClick = (id) => {
    const row = deep(`rs-rooms-nav-room[room="${cssEscape(id)}"]`)[0];
    if (!row) return false;
    row.scrollIntoView({ block: 'nearest' });
    const a = (row.shadowRoot || row).querySelector('a') || deep('a', row)[0];
    (a || row).click();
    return true;
  };

  const scrollSidebarTop = () => {
    const s = sidebarScroller();
    if (!s) return null;
    s.scrollTop = 0;
    return { scrollTop: 0, scrollHeight: Math.round(s.scrollHeight) };
  };

  const sidebarState = () => {
    const s = sidebarScroller();
    if (!s) return null;
    return { scrollTop: Math.round(s.scrollTop), scrollHeight: Math.round(s.scrollHeight),
             clientHeight: Math.round(s.clientHeight),
             atBottom: s.scrollTop + s.clientHeight >= s.scrollHeight - 4 };
  };

  /** The signed-in account's own display name. */
  const currentUserName = () => {
    const cu = deep('rs-current-user')[0];
    return cu ? (cu.getAttribute('display-name') || '').trim() : '';
  };

  /** Usernames that have actually posted in the open conversation, minus you.
   *
   * More trustworthy than any header: it is read from the messages themselves,
   * and it lists every participant of a group chat rather than just a title.
   */
  const roomParticipants = () => {
    const me = currentUserName().toLowerCase();
    const names = new Set();
    for (const e of deep('rs-timeline-event')) {
      for (const n of deep('.user-name, [class*="user-name"]', e)) {
        const t = txt(n).replace(/^u\//i, '').trim();
        if (t && t.length <= 24 && t.toLowerCase() !== me) names.add(t);
      }
    }
    return { me: currentUserName(), others: Array.from(names).slice(0, 30) };
  };

  const roomHeader = () => {
    const h = deep('rs-room-header')[0] || deep('rs-room main header')[0];
    return h ? txt(h).slice(0, 200) : '';
  };

  const currentRoomId = () => {
    const t = deep('rs-timeline')[0] || deep('rs-room')[0];
    return t ? t.getAttribute('room') : null;
  };

  /* --------------------------------------------------------------- timeline */

  /* rs-virtual-scroll-dynamic is itself the scrolling element - its wrapper
   * div reports scrollHeight == clientHeight, which silently defeats any
   * attempt to page back through history. Pick the visible one that actually
   * holds messages, since previously-visited rooms can stay mounted. */
  const timelineScroller = () => {
    const cands = deep('rs-virtual-scroll-dynamic').filter((el) => box(el).h > 0);
    const withEvents = cands.filter((el) => deep('rs-timeline-event', el).length > 0);
    const pick = (withEvents[0] || cands[0] || null);
    if (pick) return pick;
    return deep('rs-timeline div.overflow-y-auto').filter((el) => el.scrollHeight > el.clientHeight + 20)[0] || null;
  };

  const timelineState = () => {
    const s = timelineScroller();
    if (!s) return null;
    const evs = deep('rs-timeline-event');
    return {
      scrollTop: Math.round(s.scrollTop), scrollHeight: Math.round(s.scrollHeight),
      clientHeight: Math.round(s.clientHeight),
      atTop: s.scrollTop <= 2,
      atBottom: s.scrollTop + s.clientHeight >= s.scrollHeight - 4,
      eventCount: evs.length,
      firstId: evs.length ? evs[0].getAttribute('data-id') : null,
      lastId: evs.length ? evs[evs.length - 1].getAttribute('data-id') : null,
    };
  };

  /** Page towards older messages by scrolling the oldest mounted message to
   *  the top; Reddit then loads whatever came before it. */
  const advanceTimelineUp = () => {
    const evs = deep('rs-timeline-event');
    if (!evs.length) return { events: 0, firstId: null };
    evs[0].scrollIntoView({ block: 'start' });
    return { events: evs.length, firstId: evs[0].getAttribute('data-id') };
  };

  /** The same, downwards. */
  const advanceTimelineDown = () => {
    const evs = deep('rs-timeline-event');
    if (!evs.length) return { events: 0, lastId: null };
    const last = evs[evs.length - 1];
    last.scrollIntoView({ block: 'end' });
    return { events: evs.length, lastId: last.getAttribute('data-id') };
  };

  const scrollTimeline = (px) => {
    const s = timelineScroller();
    if (!s) return null;
    const before = s.scrollTop;
    s.scrollTop = Math.max(0, Math.min(s.scrollHeight, s.scrollTop + (px == null ? -s.clientHeight * 0.8 : px)));
    const st = timelineState();
    return Object.assign({ before: Math.round(before), moved: Math.round(s.scrollTop) !== Math.round(before) }, st);
  };

  const AVATAR_RE = /avatar|snoovatar|emote|emoji|award|styles\.redditmedia\.com\/t5_/i;

  /** Mounted messages that carry an image/video attachment. */
  const imageEvents = () => deep('rs-timeline-event').map((e) => {
    const bodies = deep('.room-message-body', e);
    const isImage = bodies.some((b) => /image-message|video-message|gif-message/.test(b.className))
      || deep('rs-image, .room-message-image, video', e).length > 0;
    if (!isImage) return null;
    const urls = deep('rs-image, img, video', e)
      .map((n) => n.getAttribute('src') || n.currentSrc || '')
      .filter((u) => u && !AVATAR_RE.test(u));
    return {
      id: e.getAttribute('data-id'),
      urls: Array.from(new Set(urls)).slice(0, 4),
      kind: (bodies[0] ? bodies[0].className : '').replace('room-message-body', '').trim(),
      box: box(e),
    };
  }).filter(Boolean).filter((e) => e.id);

  /** Aria-labels on the hover toolbar of a given message. */
  const menuLabelsFor = (id) => {
    const e = deep(`rs-timeline-event[data-id="${cssEscape(id)}"]`)[0];
    if (!e) return null;
    const out = [];
    for (const m of deep('rs-timeline-event-menu')) {
      if (!shown(m)) continue;
      for (const b of deep('button,[role="button"]', m)) {
        if (!shown(b)) continue;
        out.push({ label: b.getAttribute('aria-label'), box: box(b) });
      }
    }
    return out;
  };

  const eventBox = (id) => {
    const e = deep(`rs-timeline-event[data-id="${cssEscape(id)}"]`)[0];
    return e ? box(e) : null;
  };

  const eventExists = (id) => deep(`rs-timeline-event[data-id="${cssEscape(id)}"]`).length > 0;

  const scrollEventIntoView = (id) => {
    const e = deep(`rs-timeline-event[data-id="${cssEscape(id)}"]`)[0];
    if (!e) return null;
    e.scrollIntoView({ block: 'center' });
    return box(e);
  };

  /** Visible confirmation-dialog buttons, anywhere in the document. */
  const dialogButtons = () => deep('button,[role="button"]')
    .filter((b) => shown(b) && txt(b).length <= 30)
    .map((b) => ({ text: txt(b), label: b.getAttribute('aria-label'), box: box(b) }))
    .filter((b) => /^(yes,?\s*delete|delete|confirm|remove|cancel)$/i.test(b.text));

  const dialogOpen = () => deep('[role="dialog"],[role="alertdialog"],dialog,.dialog-panel')
    .some((d) => shown(d));

  /* --------------------------------------------------------------- describe */

  const describe = () => ({
    url: location.href,
    currentRoom: currentRoomId(),
    counts: {
      roomsNav: deep('rs-rooms-nav-room').length,
      timelineEvents: deep('rs-timeline-event').length,
      imageEvents: imageEvents().length,
      hoverMenus: deep('rs-timeline-event-menu').length,
    },
    sidebar: sidebarState(),
    timeline: timelineState(),
    customTags: Array.from(new Set(deep('*').map((e) => e.tagName.toLowerCase())
      .filter((t) => t.startsWith('rs-')))).slice(0, 40),
  });

  window.__RCIP = {
    deepCount: (sel) => deep(sel).length,
    visibleRooms, scrollSidebar, scrollSidebarTop, advanceSidebar, openRoomByClick,
    sidebarState, roomHeader, currentRoomId,
    currentUserName, roomParticipants,
    timelineState, scrollTimeline, advanceTimelineUp, advanceTimelineDown, imageEvents,
    menuLabelsFor, eventBox, eventExists, scrollEventIntoView,
    dialogButtons, dialogOpen, describe,
  };
})();
