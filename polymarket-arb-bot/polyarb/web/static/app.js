/* POLYARB dashboard. No dependencies, no build step; every server string is escaped before it reaches the DOM. */
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const REDUCED = !!(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches);
  const CSSV = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const FONT = CSSV('--font') || 'sans-serif';
  const MONO = CSSV('--mono') || 'monospace';

  // ------------------------------------------------------------------ vocabulary

  const STRATS = {
    negrisk_convert: { name: '轉換套利', short: '轉換', color: '#19e6d4' },
    negrisk_yes_basket: { name: 'YES 籃子', short: 'YES 籃子', color: '#8b6cff' },
    ladder: { name: '階梯套利', short: '階梯', color: '#ffc857' },
  };
  const ORDER = ['negrisk_convert', 'negrisk_yes_basket', 'ladder'];
  const strat = k => STRATS[k] || { name: k || '?', short: k || '?', color: '#5aa9ff' };

  const PHASES = {
    idle: ['準備中', 'busy'], geo: ['檢查地區', 'busy'], loading: ['載入全市場', 'busy'], sweeping: ['首次掃描', 'busy'],
    running: ['運行中', 'running'], stopping: ['關閉中', 'busy'], stopped: ['已停止', 'down'],
  };

  const READY = {
    mode: ['POLYARB_MODE=live', '交易模式設為實盤'],
    confirm: ['POLYARB_LIVE_CONFIRM=yes', '明確確認啟用實盤'],
    key: ['POLYMARKET_PRIVATE_KEY', '錢包私鑰（只存於伺服器環境變數）'],
    sdk: ['polymarket-client', '官方下單 SDK 已安裝'],
    geo: ['地區檢查', '伺服器所在地區准許 API 下單（日本等地只限制網頁版）'],
    password: ['POLYARB_DASHBOARD_PASSWORD', '已設定固定控制台密碼'],
    volume: ['/data 永久磁碟', '帳本與持倉可跨重啟保存'],
  };

  const WALLETS = {
    DEPOSIT_WALLET: 'Deposit Wallet・免 gas', POLY_PROXY: 'Polymarket 代理錢包・免 gas',
    GNOSIS_SAFE: 'Safe 錢包・免 gas', EOA: 'EOA・需少量 POL 付 gas',
  };

  const BLOCKED = {
    confirm: '未設 POLYARB_LIVE_CONFIRM=yes', key: '未設 POLYMARKET_PRIVATE_KEY', sdk: '未安裝下單 SDK',
    geo: '伺服器所在地區不准 API 下單', password: '未設 POLYARB_DASHBOARD_PASSWORD', volume: '未掛載 /data 永久磁碟',
  };
  const blockedText = b => BLOCKED[b] || (String(b).startsWith('gateway:') ? '連接 Polymarket 失敗（' + String(b).slice(8).trim() + '）' : String(b));
  const killText = r => /^Polymarket refused orders/.test(r) ? 'Polymarket 拒絕了此伺服器的訂單（HTTP 403，通常是地區限制）' : r;
  const geoDetail = (g, fallback) => g.error ? '查詢失敗' : !g.country ? fallback || '?' : `${g.country}・API ${g.api_ok ? '可用' : '受限'}`;

  const SKIPS = [
    [/^skip:cooldown$/, '冷卻中（同一市場暫不重試）'],
    [/^skip:event in flight$/, '同一事件正在執行'],
    [/^skip:hold exposure cap$/, '持倉總額已達上限'],
    [/^skip:event exposure cap$/, '單一事件已達上限'],
    [/^skip:insufficient capital$/, '可用資金不足'],
    [/^skip:trade above max_trade_usd$/, '超出單筆上限'],
    [/^skip:kill file present$/, '已手動暫停'],
    [/^skip:killed/, '熔斷中'],
    [/^skip:daily loss limit$/, '今日虧損已達上限'],
    [/^skip:consecutive failure limit$/, '連續失敗已達上限'],
    [/^skip:shutting down$/, '系統關閉中'],
    [/^skip:geoblocked/, '地區檢查未通過'],
    [/^abort: vanished on recheck$/, '下單前複查：機會已消失'],
    [/^abort: paper liquidity already used$/, '紙盤：同一流動性已用過'],
    [/^abort: /, s => '下單前攔截：' + s.slice(7)],
    [/^skip:/, s => s.slice(5)],
  ];
  const ACTIONS = [
    [/^holding to resolution$/, '持有至結算'],
    [/^converted$/, '已即時轉換為 pUSD'],
    [/^completed after retry$/, '重試後完成'],
    [/^convert filled subset$/, '轉換已成交部分'],
    [/^retry incomplete -> convert subset$/, '重試未完成，轉換已成交部分'],
    [/^no fills$/, '全部未成交（無損失）'],
    [/^legs kept/, '保留已成交腿（平倉會虧損）'],
    [/^convert impossible -> unwind$/, '無法轉換，已平倉'],
    [/^convert failed: NO legs kept$/, '轉換失敗，保留 NO 倉位'],
    [/^unwind/, '已平倉'],
  ];
  const translate = (table, s) => {
    s = String(s || '');
    for (const [re, txt] of table) if (re.test(s)) return typeof txt === 'function' ? txt(s) : txt;
    return s;
  };

  // ------------------------------------------------------------------ safe templating

  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ESC[c]);
  class Safe { constructor(s) { this.s = s; } toString() { return this.s; } }
  const render = v => (v instanceof Safe ? v.s : Array.isArray(v) ? v.map(render).join('') : esc(v));
  const html = (strings, ...vals) => new Safe(strings.reduce((acc, s, i) => acc + s + (i < vals.length ? render(vals[i]) : ''), ''));
  const setHTML = (el, content) => { if (el) el.innerHTML = render(content); };
  const setText = (el, text) => { text = String(text); if (el && el.textContent !== text) el.textContent = text; };

  const ICON = {
    stop: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M8.5 8.5l7 7M15.5 8.5l-7 7"/></svg>',
    bolt: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M13 2 4 14h7l-1 8 9-12h-7z"/></svg>',
    pause: '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1.2"/><rect x="14" y="5" width="4" height="14" rx="1.2"/></svg>',
    key: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M17 6l3 3M14 9l2 2"/></svg>',
    globe: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3.5 3 14.5 0 18M12 3c-3 3.5-3 14.5 0 18"/></svg>',
    load: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 3a9 9 0 1 0 9 9"/></svg>',
    wifi: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M2 8.5a15 15 0 0 1 20 0M5 12a10 10 0 0 1 14 0M8.5 15.5a5 5 0 0 1 7 0"/><circle cx="12" cy="19" r="1"/></svg>',
    info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/></svg>',
  };

  // ------------------------------------------------------------------ formatting

  const nf2 = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const nf0 = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
  const finite = v => typeof v === 'number' && isFinite(v);
  const usd = (v, sign = false) => {
    if (!finite(v)) return '—';
    const s = '$' + nf2.format(Math.abs(v));
    if (v <= -0.005) return '−' + s;
    return sign && v >= 0.005 ? '+' + s : s;
  };
  const num = v => (finite(v) ? nf0.format(v) : '—');
  const pct = (v, d = 1) => (finite(v) ? (v * 100).toFixed(d) + '%' : '—');
  const cents = e => {
    if (!finite(e)) return '—';
    const c = e * 100, a = Math.abs(c);
    const body = (a >= 10 ? a.toFixed(1) : a.toFixed(2)) + '¢';
    return c >= 0.005 ? '+' + body : c <= -0.005 ? '−' + body : body;
  };
  const pad2 = n => String(n).padStart(2, '0');
  const hhmm = ts => { const d = new Date(ts * 1000); return pad2(d.getHours()) + ':' + pad2(d.getMinutes()); };
  const hhmmss = ts => hhmm(ts) + ':' + pad2(new Date(ts * 1000).getSeconds());
  let skew = 0;
  const now = () => Date.now() / 1000 + skew;
  const ago = ts => {
    if (!finite(ts) || ts <= 0) return '—';
    const s = Math.max(0, now() - ts);
    if (s < 5) return '剛剛';
    if (s < 60) return Math.floor(s) + ' 秒前';
    if (s < 3600) return Math.floor(s / 60) + ' 分鐘前';
    if (s < 86400) return Math.floor(s / 3600) + ' 小時前';
    return Math.floor(s / 86400) + ' 天前';
  };
  const dur = s => {
    if (!finite(s)) return '—';
    if (s < 60) return Math.round(s) + ' 秒';
    if (s < 3600) return Math.floor(s / 60) + ' 分 ' + pad2(Math.floor(s % 60)) + ' 秒';
    if (s < 86400) return Math.floor(s / 3600) + ' 時 ' + pad2(Math.floor((s % 3600) / 60)) + ' 分';
    return Math.floor(s / 86400) + ' 天 ' + Math.floor((s % 86400) / 3600) + ' 時';
  };
  const until = iso => {
    const t = Date.parse(iso || '');
    if (!isFinite(t)) return '—';
    const s = t / 1000 - now();
    if (s <= 0) return '待結算';
    return s >= 86400 ? Math.round(s / 86400) + ' 天後' : Math.max(1, Math.round(s / 3600)) + ' 小時後';
  };
  const marketURL = slug => (slug ? 'https://polymarket.com/event/' + encodeURIComponent(slug) : '');
  const link = (text, slug) => (slug ? html`<a href="${marketURL(slug)}" target="_blank" rel="noopener noreferrer">${text}</a>` : html`${text}`);
  const chip = k => { const s = strat(k); return html`<span class="chip" style="--c:${s.color}">${s.short}</span>`; };
  const hexA = (hex, a) => {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  };
  const hash01 = s => {
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
    h ^= h >>> 15; h = Math.imul(h, 2246822507); h ^= h >>> 13;
    return (h >>> 0) / 4294967296;
  };

  // ------------------------------------------------------------------ count-up numbers

  const tweenIds = new WeakMap();
  function tween(el, to, fmt, ms = 900) {
    if (!el) return;
    if (!finite(to)) { el._v = undefined; setText(el, fmt(to)); return; }
    const from = finite(el._v) ? el._v : to;
    el._v = to;
    if (from === to || REDUCED || document.hidden) { setText(el, fmt(to)); return; }
    const id = {};
    tweenIds.set(el, id);
    const t0 = performance.now();
    const step = t => {
      if (tweenIds.get(el) !== id) return;
      const k = Math.min(1, (t - t0) / ms);
      setText(el, fmt(from + (to - from) * (1 - Math.pow(1 - k, 3))));
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  // ------------------------------------------------------------------ SVG line chart

  const SVGNS = 'http://www.w3.org/2000/svg';
  const svg = (tag, attrs = {}) => {
    const e = document.createElementNS(SVGNS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  };
  const r1 = v => Math.round(v * 10) / 10;

  function monotonePath(pts) {
    const n = pts.length;
    if (!n) return '';
    if (n === 1) return `M${r1(pts[0][0])},${r1(pts[0][1])}`;
    const dx = [], m = [], t = new Array(n);
    for (let i = 0; i < n - 1; i++) {
      dx[i] = pts[i + 1][0] - pts[i][0];
      m[i] = dx[i] ? (pts[i + 1][1] - pts[i][1]) / dx[i] : 0;
    }
    t[0] = m[0];
    t[n - 1] = m[n - 2];
    for (let i = 1; i < n - 1; i++) {
      if (m[i - 1] * m[i] <= 0) t[i] = 0;
      else {
        const w1 = 2 * dx[i] + dx[i - 1], w2 = dx[i] + 2 * dx[i - 1];
        t[i] = (w1 + w2) / (w1 / m[i - 1] + w2 / m[i]);
      }
    }
    let d = `M${r1(pts[0][0])},${r1(pts[0][1])}`;
    for (let i = 0; i < n - 1; i++) {
      const h = dx[i] / 3;
      d += `C${r1(pts[i][0] + h)},${r1(pts[i][1] + h * t[i])},${r1(pts[i + 1][0] - h)},${r1(pts[i + 1][1] - h * t[i + 1])},${r1(pts[i + 1][0])},${r1(pts[i + 1][1])}`;
    }
    return d;
  }

  function niceScale(lo, hi, count = 4) {
    const span = hi - lo || 1;
    const raw = span / count, mag = Math.pow(10, Math.floor(Math.log10(raw))), r = raw / mag;
    const step = (r < 1.5 ? 1 : r < 3 ? 2 : r < 7 ? 5 : 10) * mag;
    const a = Math.floor(lo / step + 1e-9) * step, b = Math.ceil(hi / step - 1e-9) * step;
    const ticks = [];
    for (let v = a; v <= b + step / 2; v += step) ticks.push(Math.abs(v) < step / 1e6 ? 0 : v);
    return { lo: a, hi: b === a ? a + step : b, ticks };
  }

  const lowerBound = (arr, x) => {
    let lo = 0, hi = arr.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (arr[mid] < x) lo = mid + 1; else hi = mid; }
    return lo;
  };

  let chartSeq = 0;
  class LineChart {
    constructor(el, opts) {
      this.el = el;
      this.opts = Object.assign({ pad: [10, 12, 22, 52], yFmt: v => String(v), minSpan: 1, nonneg: false, gap: 300, empty: '等待數據…' }, opts);
      this.uid = 'c' + ++chartSeq;
      this.range = opts.range || Infinity;
      this.svg = svg('svg');
      this.tip = document.createElement('div');
      this.tip.className = 'tip';
      el.append(this.svg, this.tip);
      this.data = null;
      this.view = null;
      new ResizeObserver(() => this.draw()).observe(el);
      el.addEventListener('pointermove', e => this.hover(e));
      el.addEventListener('pointerleave', () => this.unhover());
    }

    set(data) { this.data = data; this.draw(); }

    draw() {
      const { el, data, opts } = this;
      const W = el.clientWidth, H = el.clientHeight;
      if (!W || !H) return;
      const root = this.svg;
      root.setAttribute('viewBox', `0 0 ${W} ${H}`);
      root.replaceChildren();
      this.view = null;
      const [pt, pr, pb, pl] = opts.pad;
      const iw = W - pl - pr, ih = H - pt - pb;
      const ts = data && data.t ? data.t : [];
      let first = ts.length;
      for (const s of data ? data.series : []) {
        for (let i = 0; i < first; i++) if (finite(s.values[i])) { first = i; break; }
      }
      if (ts.length - first < 2) {
        const tx = svg('text', { x: W / 2, y: H / 2, 'text-anchor': 'middle', fill: '#565d80', 'font-size': 12 });
        tx.textContent = opts.empty;
        root.append(tx);
        return;
      }
      const tEnd = ts[ts.length - 1];
      const tStart = Math.max(ts[first], tEnd - this.range);
      const i0 = Math.max(0, lowerBound(ts, tStart) - 1);
      let lo = Infinity, hi = -Infinity;
      for (const s of data.series) for (let i = i0; i < ts.length; i++) {
        const v = s.values[i];
        if (finite(v)) { if (v < lo) lo = v; if (v > hi) hi = v; }
      }
      if (!isFinite(lo)) { lo = 0; hi = opts.minSpan; }
      lo = Math.min(lo, 0);
      hi = Math.max(hi, 0);
      if (hi - lo < opts.minSpan) {
        if (opts.nonneg || lo >= 0) hi = lo + opts.minSpan;
        else { const mid = (hi + lo) / 2; lo = mid - opts.minSpan / 2; hi = mid + opts.minSpan / 2; }
      }
      const sc = niceScale(lo, hi);
      lo = sc.lo; hi = sc.hi;
      const x = t => pl + ((t - tStart) / (tEnd - tStart || 1)) * iw;
      const y = v => pt + (1 - (v - lo) / (hi - lo)) * ih;

      const defs = svg('defs');
      const clip = svg('clipPath', { id: this.uid + '-clip' });
      clip.append(svg('rect', { x: pl, y: 0, width: iw, height: H }));
      defs.append(clip);
      root.append(defs);

      const grid = svg('g', { class: 'grid' }), axis = svg('g', { class: 'axis' });
      for (const v of sc.ticks) {
        const yy = r1(y(v));
        grid.append(svg('line', { x1: pl, x2: W - pr, y1: yy, y2: yy }));
        const label = svg('text', { x: pl - 8, y: yy + 3.5, 'text-anchor': 'end' });
        label.textContent = opts.yFmt(v);
        axis.append(label);
      }
      const span = tEnd - tStart;
      const nX = Math.max(2, Math.min(6, Math.floor(iw / 90)));
      const tFmt = span / nX < 60 ? hhmmss : hhmm;
      let prev = '';
      for (let k = 0; k <= nX; k++) {
        const t = tStart + (span * k) / nX;
        const text = tFmt(t);
        if (text === prev) continue;
        prev = text;
        const label = svg('text', { x: r1(x(t)), y: H - 5, 'text-anchor': k === 0 ? 'start' : k === nX ? 'end' : 'middle' });
        label.textContent = text;
        axis.append(label);
      }
      root.append(grid, axis);
      if (lo < 0 && hi > 0) root.append(svg('line', { class: 'zero', x1: pl, x2: W - pr, y1: r1(y(0)), y2: r1(y(0)) }));

      const plot = svg('g', { 'clip-path': `url(#${this.uid}-clip)` });
      const base = y(Math.min(Math.max(0, lo), hi));
      data.series.forEach((s, si) => {
        const runs = [];
        let run = [], prev = -1;
        for (let i = i0; i < ts.length; i++) {
          const v = s.values[i];
          if (finite(v)) {
            // A long silence (process down) is drawn as a break, not interpolated across.
            if (run.length && ts[i] - ts[prev] > opts.gap) { runs.push(run); run = []; }
            run.push([x(ts[i]), y(v)]);
            prev = i;
          } else if (run.length) { runs.push(run); run = []; }
        }
        if (run.length) runs.push(run);
        if (s.area) {
          const gid = `${this.uid}-g${si}`;
          const lg = svg('linearGradient', { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 });
          lg.append(svg('stop', { offset: 0, 'stop-color': s.color, 'stop-opacity': 0.32 }), svg('stop', { offset: 1, 'stop-color': s.color, 'stop-opacity': 0 }));
          defs.append(lg);
          for (const r of runs) if (r.length > 1) {
            plot.append(svg('path', { d: monotonePath(r) + `L${r1(r[r.length - 1][0])},${r1(base)}L${r1(r[0][0])},${r1(base)}Z`, fill: `url(#${gid})` }));
          }
        }
        for (const r of runs) {
          const attrs = { d: monotonePath(r), fill: 'none', stroke: s.color, 'stroke-width': s.width || 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' };
          if (s.dash) attrs['stroke-dasharray'] = s.dash;
          if (s.glow) attrs.style = `filter: drop-shadow(0 0 6px ${hexA(s.color, 0.55)})`;
          plot.append(svg('path', attrs));
        }
        const last = runs.length ? runs[runs.length - 1] : null;
        if (s.glow && last) {
          const [lx, ly] = last[last.length - 1];
          plot.append(svg('circle', { cx: r1(lx), cy: r1(ly), r: 3.5, fill: s.color, style: `filter: drop-shadow(0 0 6px ${s.color})` }));
        }
      });
      root.append(plot);

      this.cross = svg('line', { class: 'cross', x1: 0, x2: 0, y1: pt, y2: pt + ih, visibility: 'hidden' });
      this.dots = data.series.map(s => svg('circle', { r: 4, fill: '#04050c', stroke: s.color, 'stroke-width': 2, visibility: 'hidden' }));
      root.append(this.cross, ...this.dots);
      this.view = { x, y, i0, ts, pl, iw, W, tStart, tEnd };
      if (this.lastPointer) this.hover(this.lastPointer);
    }

    hover(e) {
      this.lastPointer = e;
      const v = this.view;
      if (!v || !this.data) return;
      const box = this.el.getBoundingClientRect();
      const px = e.clientX - box.left;
      if (px < v.pl || px > v.pl + v.iw) { this.unhover(); return; }
      const t = v.tStart + ((px - v.pl) / v.iw) * (v.tEnd - v.tStart);
      let i = lowerBound(v.ts, t);
      if (i >= v.ts.length) i = v.ts.length - 1;
      if (i > v.i0 && Math.abs(v.ts[i - 1] - t) < Math.abs(v.ts[i] - t)) i -= 1;
      const xx = r1(v.x(v.ts[i]));
      this.cross.setAttribute('x1', xx);
      this.cross.setAttribute('x2', xx);
      this.cross.setAttribute('visibility', 'visible');
      const rows = [];
      this.data.series.forEach((s, k) => {
        const val = s.values[i];
        const dot = this.dots[k];
        if (finite(val)) {
          dot.setAttribute('cx', xx);
          dot.setAttribute('cy', r1(v.y(val)));
          dot.setAttribute('visibility', 'visible');
        } else dot.setAttribute('visibility', 'hidden');
        rows.push(html`<div class="row"><span class="sw" style="background:${s.color}"></span>${s.label}<span style="margin-left:auto;color:#e8ebf7">${(s.fmt || this.opts.yFmt)(val)}</span></div>`);
      });
      setHTML(this.tip, html`<b>${hhmmss(v.ts[i])}</b>${rows}`);
      const tw = this.tip.offsetWidth;
      const left = xx + 14 + tw > v.W ? xx - 14 - tw : xx + 14;
      this.tip.style.left = Math.max(0, left) + 'px';
      this.tip.style.top = '6px';
      this.tip.classList.add('on');
    }

    unhover() {
      this.lastPointer = null;
      this.tip.classList.remove('on');
      if (this.cross) this.cross.setAttribute('visibility', 'hidden');
      for (const d of this.dots || []) d.setAttribute('visibility', 'hidden');
    }
  }

  // ------------------------------------------------------------------ radar

  const E0 = 0.002, EMAX = 0.10, LMAX = Math.log(1 + EMAX / E0);
  const RINGS = [[-0.005, '−½¢'], [-0.01, '−1¢'], [-0.02, '−2¢'], [-0.05, '−5¢']];
  const radiusFrac = (edge, r0f) => {
    if (edge >= 0) return r0f * Math.max(0.12, 0.8 - (edge / 0.01) * 0.6);
    return r0f + (1 - r0f) * Math.min(1, Math.log(1 + -edge / E0) / LMAX);
  };
  const sprites = {};
  const glowSprite = color => {
    if (sprites[color]) return sprites[color];
    const c = document.createElement('canvas');
    c.width = c.height = 64;
    const g = c.getContext('2d');
    const grd = g.createRadialGradient(32, 32, 0, 32, 32, 32);
    grd.addColorStop(0, hexA(color, 0.85));
    grd.addColorStop(0.22, hexA(color, 0.4));
    grd.addColorStop(1, hexA(color, 0));
    g.fillStyle = grd;
    g.fillRect(0, 0, 64, 64);
    return (sprites[color] = c);
  };

  class Radar {
    constructor(canvas, tip) {
      this.c = canvas;
      this.ctx = canvas.getContext('2d');
      this.tip = tip;
      this.dots = new Map();
      this.sectors = ORDER.slice();
      this.ripples = [];
      this.pos = [];
      this.mouse = null;
      this.hover = null;
      this.loading = true;
      this.t0 = performance.now();
      this.resize();
      new ResizeObserver(() => this.resize()).observe(canvas.parentElement);
      canvas.addEventListener('pointermove', e => {
        const b = canvas.getBoundingClientRect();
        this.mouse = [e.clientX - b.left, e.clientY - b.top];
      });
      canvas.addEventListener('pointerleave', () => { this.mouse = null; });
      canvas.addEventListener('click', () => {
        if (this.hover && this.hover.slug) window.open(marketURL(this.hover.slug), '_blank', 'noopener');
      });
      requestAnimationFrame(t => this.frame(t));
    }

    resize() {
      const b = this.c.parentElement.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      this.w = b.width;
      this.h = b.height;
      this.c.width = Math.max(1, Math.round(b.width * dpr));
      this.c.height = Math.max(1, Math.round(b.height * dpr));
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    geometry() {
      // Narrow cards have no room beside the circle, so the legend gets a strip underneath it.
      const bottom = this.w < 560 ? 72 : 36;
      const R = Math.max(40, Math.min(this.w - 20, this.h - bottom - 8) / 2 - 8);
      return { cx: this.w / 2, cy: (this.h - bottom) / 2 + 6, R, r0: R * 0.12 };
    }

    setRanking(ranking) {
      const st = (ranking && ranking.strategies) || {};
      const names = ORDER.filter(n => st[n]).concat(Object.keys(st).filter(n => !ORDER.includes(n)));
      if (names.length) this.sectors = names;
      this.loading = !names.length;
      const seen = new Set();
      const t = performance.now();
      names.forEach((name, si) => {
        for (const row of st[name].top || []) {
          seen.add(row.key);
          let d = this.dots.get(row.key);
          if (!d) { d = { key: row.key, r: null, born: t, u: hash01(row.key) }; this.dots.set(row.key, d); }
          Object.assign(d, { strategy: name, si, title: row.title, slug: row.slug, edge: row.edge, legs: row.legs, dying: 0 });
        }
      });
      for (const d of this.dots.values()) if (!seen.has(d.key) && !d.dying) d.dying = t;
    }

    ping(strategy) {
      this.ripples.push({ t: performance.now(), color: strat(strategy).color });
      if (this.ripples.length > 12) this.ripples.shift();
    }

    frame(tNow) {
      requestAnimationFrame(t => this.frame(t));
      if (document.hidden || !this.w) return;
      const ctx = this.ctx;
      const { cx, cy, R, r0 } = this.geometry();
      const t = (tNow - this.t0) / 1000;
      const TAU = Math.PI * 2;
      const sweep = REDUCED ? -Math.PI / 2 : ((t * TAU) / 7 - Math.PI / 2) % TAU;
      const n = this.sectors.length || 1;
      const span = TAU / n, gap = 0.035;
      ctx.clearRect(0, 0, this.w, this.h);

      let g = ctx.createRadialGradient(cx, cy, 0, cx, cy, R);
      g.addColorStop(0, 'rgba(46,242,160,0.09)');
      g.addColorStop(0.3, 'rgba(25,230,212,0.03)');
      g.addColorStop(1, 'rgba(139,108,255,0.035)');
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, TAU);
      ctx.fill();

      this.sectors.forEach((name, si) => {
        const a0 = -Math.PI / 2 + si * span, a1 = a0 + span, col = strat(name).color;
        ctx.fillStyle = hexA(col, 0.035);
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.arc(cx, cy, R, a0 + gap, a1 - gap);
        ctx.closePath();
        ctx.fill();
        ctx.strokeStyle = 'rgba(255,255,255,0.07)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(a0) * r0, cy + Math.sin(a0) * r0);
        ctx.lineTo(cx + Math.cos(a0) * R, cy + Math.sin(a0) * R);
        ctx.stroke();
        ctx.strokeStyle = hexA(col, 0.6);
        ctx.lineWidth = 2.5;
        ctx.lineCap = 'round';
        ctx.beginPath();
        ctx.arc(cx, cy, R + 6, a0 + gap * 1.5, a1 - gap * 1.5);
        ctx.stroke();
      });

      ctx.setLineDash([2, 5]);
      ctx.lineWidth = 1;
      ctx.font = `500 9.5px ${MONO}`;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'alphabetic';
      for (const [e, label] of RINGS) {
        const rr = radiusFrac(e, r0 / R) * R;
        ctx.strokeStyle = 'rgba(255,255,255,0.09)';
        ctx.beginPath();
        ctx.arc(cx, cy, rr, 0, TAU);
        ctx.stroke();
        ctx.fillStyle = 'rgba(138,146,180,0.55)';
        ctx.fillText(label, cx + 4, cy - rr - 3);
      }
      ctx.setLineDash([]);
      ctx.strokeStyle = 'rgba(255,255,255,0.12)';
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, TAU);
      ctx.stroke();

      const W = 1.0;
      if (ctx.createConicGradient) {
        const cg = ctx.createConicGradient(sweep - W, cx, cy);
        const f = W / TAU;
        cg.addColorStop(0, 'rgba(46,242,160,0)');
        cg.addColorStop(f * 0.999, 'rgba(46,242,160,0.2)');
        cg.addColorStop(f, 'rgba(46,242,160,0)');
        cg.addColorStop(1, 'rgba(46,242,160,0)');
        ctx.fillStyle = cg;
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.arc(cx, cy, R, sweep - W, sweep);
        ctx.closePath();
        ctx.fill();
      } else {
        for (let k = 0; k < 14; k++) {
          ctx.fillStyle = `rgba(46,242,160,${0.17 * (1 - k / 14)})`;
          ctx.beginPath();
          ctx.moveTo(cx, cy);
          ctx.arc(cx, cy, R, sweep - (W * (k + 1)) / 14, sweep - (W * k) / 14);
          ctx.closePath();
          ctx.fill();
        }
      }
      ctx.save();
      ctx.strokeStyle = 'rgba(46,242,160,0.8)';
      ctx.lineWidth = 1.5;
      ctx.shadowColor = '#2ef2a0';
      ctx.shadowBlur = 12;
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(cx + Math.cos(sweep) * R, cy + Math.sin(sweep) * R);
      ctx.stroke();
      ctx.restore();

      for (const rp of this.ripples) {
        const k = (tNow - rp.t) / 1800;
        if (k < 0 || k >= 1) continue;
        ctx.strokeStyle = hexA(rp.color, 0.7 * (1 - k));
        ctx.lineWidth = 2.5 * (1 - k) + 0.5;
        ctx.beginPath();
        ctx.arc(cx, cy, r0 + (R - r0) * (1 - Math.pow(1 - k, 3)), 0, TAU);
        ctx.stroke();
      }
      this.ripples = this.ripples.filter(rp => tNow - rp.t < 1800);

      const pulse = 0.5 + 0.5 * Math.sin(t * 2.2);
      g = ctx.createRadialGradient(cx, cy, 0, cx, cy, r0 * (1.7 + 0.25 * pulse));
      g.addColorStop(0, 'rgba(46,242,160,0.5)');
      g.addColorStop(0.55, 'rgba(46,242,160,0.13)');
      g.addColorStop(1, 'rgba(46,242,160,0)');
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(cx, cy, r0 * 2, 0, TAU);
      ctx.fill();
      ctx.strokeStyle = `rgba(46,242,160,${0.45 + 0.3 * pulse})`;
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.arc(cx, cy, r0, 0, TAU);
      ctx.stroke();

      const r0f = r0 / R;
      this.pos = [];
      for (const [key, d] of this.dots) {
        let alpha = Math.min(1, (tNow - d.born) / 700);
        if (d.dying) {
          alpha *= 1 - (tNow - d.dying) / 900;
          if (alpha <= 0) { this.dots.delete(key); continue; }
        }
        const target = radiusFrac(d.edge, r0f) * R;
        d.r = d.r == null ? R : d.r + (target - d.r) * 0.06;
        const a0 = -Math.PI / 2 + d.si * span + gap * 2;
        const ang = a0 + d.u * (span - gap * 4);
        const x = cx + Math.cos(ang) * d.r, y = cy + Math.sin(ang) * d.r;
        let behind = (sweep - ang) % TAU;
        if (behind < 0) behind += TAU;
        const lit = REDUCED ? 0.4 : Math.exp(-behind / 1.2);
        const hot = d.edge >= 0;
        const col = hot ? '#2ef2a0' : strat(d.strategy).color;
        const size = Math.min(6.5, 1.8 + Math.sqrt(d.legs || 2) * 0.75);
        const gs = size * (2.6 + 2.4 * lit);
        ctx.globalAlpha = alpha * (0.35 + 0.65 * lit);
        ctx.drawImage(glowSprite(col), x - gs, y - gs, gs * 2, gs * 2);
        ctx.globalAlpha = alpha * (0.55 + 0.45 * lit);
        ctx.fillStyle = col;
        ctx.beginPath();
        ctx.arc(x, y, size, 0, TAU);
        ctx.fill();
        if (hot) {
          const k = (t * 0.9) % 1;
          ctx.globalAlpha = alpha * (1 - k);
          ctx.strokeStyle = '#2ef2a0';
          ctx.lineWidth = 1.4;
          ctx.beginPath();
          ctx.arc(x, y, size + 3 + 10 * k, 0, TAU);
          ctx.stroke();
        }
        ctx.globalAlpha = 1;
        this.pos.push([x, y, d, size]);
      }

      ctx.font = `700 11px ${FONT}`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillStyle = 'rgba(232,235,247,0.9)';
      ctx.fillText('套利', cx, cy);
      if (this.loading) {
        ctx.font = `500 12px ${FONT}`;
        ctx.fillStyle = 'rgba(138,146,180,0.9)';
        ctx.fillText('正在載入全市場訂單簿…', cx, cy + r0 + 22);
      }
      this.pick();
    }

    pick() {
      let best = null, bd = 13 * 13;
      if (this.mouse) {
        for (const p of this.pos) {
          const dx = p[0] - this.mouse[0], dy = p[1] - this.mouse[1], dd = dx * dx + dy * dy;
          if (dd < bd) { bd = dd; best = p; }
        }
      }
      const d = best ? best[2] : null;
      const cursor = d && d.slug ? 'pointer' : 'crosshair';
      if (this.cursor !== cursor) { this.cursor = cursor; this.c.style.cursor = cursor; }
      if (!d) {
        if (this.hover) this.tip.classList.remove('on');
        this.hover = null;
        return;
      }
      const [x, y, , size] = best;
      const ctx = this.ctx;
      ctx.strokeStyle = 'rgba(255,255,255,0.85)';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(x, y, size + 5, 0, Math.PI * 2);
      ctx.stroke();
      if (this.hover !== d || this.tipEdge !== d.edge) {
        const s = strat(d.strategy);
        setHTML(this.tip, html`<b>${d.title}</b>
          <div class="row"><span class="sw" style="background:${s.color}"></span>${s.name}・${d.legs} 腿</div>
          <div class="row">頂部邊際 <span class="${d.edge >= 0 ? 'pos' : 'neg'}">${cents(d.edge)}</span>／組</div>
          ${d.slug ? html`<div class="row">點擊開啟 Polymarket</div>` : ''}`);
        this.tipEdge = d.edge;
      }
      this.hover = d;
      const tw = this.tip.offsetWidth, th = this.tip.offsetHeight;
      let left = x + 16, top = y - th / 2;
      if (left + tw > this.w) left = x - 16 - tw;
      top = Math.max(0, Math.min(this.h - th, top));
      this.tip.style.left = Math.max(0, left) + 'px';
      this.tip.style.top = top + 'px';
      this.tip.classList.add('on');
    }
  }

  // ------------------------------------------------------------------ state

  const S = {
    meta: {}, tick: null, series: null, ranking: null, opps: [], execs: [], range: 86400,
    nearTab: ORDER[0], logFilter: 'all', sigs: {}, mode: null, phase: null, connected: false,
  };
  const FEED_MAX = 60;
  const LOG_MAX = 500;

  const pnlChart = new LineChart($('#pnl-chart'), {
    yFmt: v => usd(v), minSpan: 1, range: S.range, empty: '累積數據中…（每 10 秒取樣）',
  });
  const tpChart = new LineChart($('#tp-chart'), {
    pad: [8, 12, 22, 44], yFmt: v => (finite(v) ? (v >= 1000 ? (v / 1000).toFixed(1) + 'k' : nf0.format(v)) : '—'),
    minSpan: 10, nonneg: true, empty: '累積數據中…',
  });
  const radar = new Radar($('#radar'), $('#radar-tip'));

  setHTML($('#pnl-legend'), html`<span><i style="background:#2ef2a0"></i>累計利潤（含已鎖定）</span><span><i style="background:#19e6d4"></i>已實現</span>`);
  setHTML($('#tp-legend'), html`<span><i style="background:#19e6d4"></i>WebSocket 訊息／秒</span><span><i style="background:#8b6cff"></i>策略評估／秒</span>`);

  // ------------------------------------------------------------------ static scaffolding

  const KPIS = [
    ['available', '可用資金', '#19e6d4'], ['exposure', '持倉成本', '#8b6cff'], ['rate', '執行成功率', '#2ef2a0'],
    ['opps', '套利機會（去重）', '#ffc857'], ['best', '最接近套利', '#ff4d8d'], ['targets', '監控目標', '#5aa9ff'],
    ['fresh', '訂單簿新鮮度', '#19e6d4'], ['stream', '即時串流', '#8b6cff'],
  ];
  setHTML($('#kpis'), KPIS.map(([k, label, c], i) => html`<div class="kpi" style="--c:${c};animation-delay:${i * 45}ms"><label>${label}</label><div class="v" data-k="${k}">—</div><div class="s" data-s="${k}">&nbsp;</div></div>`));
  const kv = k => $(`#kpis [data-k="${k}"]`);
  const ks = k => $(`#kpis [data-s="${k}"]`);

  const ENGINE = [
    ['uptime', '運行時間'], ['sweep', '全量掃描'], ['refresh', '市場刷新'], ['ws', 'WebSocket'],
    ['cpu', 'CPU'], ['rss', '記憶體'], ['eval', '策略評估'], ['clients', '控制台連線'],
  ];
  setHTML($('#engine-grid'), ENGINE.map(([k, label]) => html`<div class="eg"><label>${label}</label><div class="v" data-k="${k}">—</div><div class="s" data-s="${k}">&nbsp;</div></div>`));
  const ev = k => $(`#engine-grid [data-k="${k}"]`);
  const es_ = k => $(`#engine-grid [data-s="${k}"]`);

  const GAUGES = [['hold', '持倉額度'], ['loss', '今日虧損額度'], ['fail', '連續失敗']];
  const RING = 2 * Math.PI * 42;
  setHTML($('#gauges'), GAUGES.map(([k, label]) => html`<div class="gauge" data-g="${k}">
    <svg viewBox="0 0 100 100"><circle class="track" cx="50" cy="50" r="42" fill="none" stroke-width="8"/>
    <circle class="arc" cx="50" cy="50" r="42" fill="none" stroke="#2ef2a0" stroke-width="8" stroke-linecap="round" stroke-dasharray="${RING.toFixed(2)}" stroke-dashoffset="${RING.toFixed(2)}" transform="rotate(-90 50 50)"/>
    <text class="gv" x="50" y="48" text-anchor="middle" dominant-baseline="central">—</text><text class="gs" x="50" y="68" text-anchor="middle"></text></svg>
    <label>${label}</label></div>`));

  function setGauge(k, frac, value, sub) {
    const root = $(`#gauges [data-g="${k}"]`);
    if (!root) return;
    const f = Math.max(0, Math.min(1, finite(frac) ? frac : 0));
    const color = f < 0.6 ? '#2ef2a0' : f < 0.85 ? '#ffc857' : '#ff5c7a';
    const arc = root.querySelector('.arc');
    arc.setAttribute('stroke-dashoffset', (RING * (1 - f)).toFixed(2));
    arc.setAttribute('stroke', color);
    arc.style.color = color;
    setText(root.querySelector('.gv'), value);
    setText(root.querySelector('.gs'), sub);
  }

  // ------------------------------------------------------------------ feeds

  function oppItem(o, fresh) {
    const legs = o.legs || [];
    const shown = legs.slice(0, 6);
    const settle = o.settlement === 'convert' ? '即時轉換' : finite(o.days) ? `持有約 ${Math.max(1, Math.round(o.days))} 天` : '持有至結算';
    return html`<div class="item${fresh ? ' fresh' : ''}">
      <div class="item-top">${chip(o.strategy)}<span class="item-title" title="${o.title}">${link(o.title, o.slug)}</span><span class="ago" data-ts="${o.ts}">${ago(o.ts)}</span></div>
      <div class="item-meta"><span>利潤 <b class="pos">${usd(o.profit, true)}</b></span><span>邊際 <b>${finite(o.edge_pct) ? o.edge_pct.toFixed(2) + '%' : '—'}</b></span><span>成本 ${usd(o.cost)}</span>${finite(o.apr_pct) ? html`<span>年化 ${o.apr_pct.toFixed(1)}%</span>` : ''}<span>${settle}</span><span>風險 ${o.risk === 'low' ? '低' : '中'}</span></div>
      <div class="item-legs">${shown.map(l => html`<span class="leg ${l.outcome === 'NO' ? 'no' : 'yes'}" title="${l.label}">${l.outcome} ${l.label} @ ${finite(l.avg) ? l.avg.toFixed(3) : '?'}</span>`)}${legs.length > shown.length ? html`<span class="leg">+${legs.length - shown.length} 腿</span>` : ''}</div>
    </div>`;
  }

  function execItem(x, fresh) {
    const legs = x.legs || [];
    const filled = legs.filter(l => l.ok).length;
    const state = x.orphaned ? ['!', '需人手檢查', 'neg'] : x.complete ? ['✓', '完成', 'pos'] : x.sent ? ['◐', '部分成交', 'amber'] : ['·', '未送出', 'muted'];
    return html`<div class="item${fresh ? ' fresh' : ''}">
      <div class="item-top">${chip(x.strategy)}<span class="item-title" title="${x.title}">${link(x.title, x.slug)}</span><span class="ago" data-ts="${x.ts}">${ago(x.ts)}</span></div>
      <div class="item-meta"><span class="${state[2]}"><b>${state[0]} ${state[1]}</b></span><span title="${x.action}">${translate(ACTIONS, x.action)}</span><span>${x.mode === 'live' ? '實盤' : '紙盤'}</span><span>${filled}/${legs.length} 腿成交</span><span>${finite(x.latency_ms) ? nf0.format(x.latency_ms) + ' ms' : ''}</span></div>
      <div class="item-meta"><span>投入 <b>${usd(x.spent)}</b></span>${x.locked_cost > 0 ? html`<span>鎖定 → 保證派彩 <b>${usd(x.expected_payout)}</b></span>` : ''}${x.realized_pnl ? html`<span>已實現 <b class="${x.realized_pnl >= 0 ? 'pos' : 'neg'}">${usd(x.realized_pnl, true)}</b></span>` : ''}<span>預期利潤 <b class="pos">${usd(x.expected_profit, true)}</b></span></div>
    </div>`;
  }

  const EMPTY_OPPS = html`<div class="empty"><b>暫時未有達標的套利機會</b>市場有效率時屬正常；機器人正持續為數千個目標定價，出現即會在此顯示。</div>`;
  const EMPTY_EXECS = html`<div class="empty"><b>尚未有成交</b>機會通過所有風控檢查後，會在此顯示每一腿的成交結果。</div>`;

  function renderFeed(el, items, itemFn, empty) {
    const list = items.slice(-FEED_MAX).reverse();
    setHTML(el, list.length ? list.map(i => itemFn(i, false)) : empty);
  }

  function prependFeed(el, item) {
    const empty = el.querySelector('.empty');
    if (empty) el.innerHTML = '';
    el.insertAdjacentHTML('afterbegin', render(item));
    while (el.children.length > FEED_MAX) el.lastElementChild.remove();
  }

  // ------------------------------------------------------------------ console

  const consoleEl = $('#console');
  function logLine(e) {
    const trade = /^(OPP|EXEC) /.test(e.msg || '');
    const name = String(e.name || '').replace(/^polyarb\./, '');
    return html`<div class="ln ${e.level}${trade ? ' trade' : ''}"><span class="t">${hhmmss(e.ts)}</span><span class="lv">${e.level}</span><span class="nm">${name}</span><span class="${trade ? 'hl' : ''}">${e.msg}</span></div>`;
  }
  function appendLogs(entries, replace) {
    const stick = replace || consoleEl.scrollTop + consoleEl.clientHeight >= consoleEl.scrollHeight - 40;
    if (replace) consoleEl.innerHTML = '';
    consoleEl.insertAdjacentHTML('beforeend', render(entries.map(logLine)));
    while (consoleEl.children.length > LOG_MAX) consoleEl.firstElementChild.remove();
    if (stick) consoleEl.scrollTop = consoleEl.scrollHeight;
  }

  // ------------------------------------------------------------------ charts

  let chartsQueued = false;
  function drawCharts() {
    if (chartsQueued) return;
    chartsQueued = true;
    requestAnimationFrame(() => {
      chartsQueued = false;
      const s = S.series;
      if (!s || !s.t) return;
      const total = s.t.map((_, i) => (finite(s.pnl[i]) ? s.pnl[i] + (finite(s.locked[i]) ? s.locked[i] : 0) : null));
      pnlChart.range = S.range;
      pnlChart.set({
        t: s.t,
        series: [
          { label: '累計利潤', color: '#2ef2a0', values: total, area: true, glow: true, width: 2.4 },
          { label: '已實現', color: '#19e6d4', values: s.pnl, dash: '4 4', width: 1.6 },
        ],
      });
      tpChart.range = 3 * 3600;
      tpChart.set({
        t: s.t,
        series: [
          { label: 'WS 訊息／秒', color: '#19e6d4', values: s.ws, area: true },
          { label: '評估／秒', color: '#8b6cff', values: s.evals },
        ],
      });
    });
  }

  function pushSample(p) {
    const s = S.series;
    if (!s || !s.t) return;
    if (s.t.length && p.t <= s.t[s.t.length - 1]) return;
    for (const k of Object.keys(s)) s[k].push(p[k] ?? null);
    const cutoff = p.t - 86400;
    let drop = 0;
    while (drop < s.t.length && s.t[drop] < cutoff) drop++;
    drop = Math.max(drop, s.t.length - 10000);
    if (drop > 0) for (const k of Object.keys(s)) s[k].splice(0, drop);
  }

  // ------------------------------------------------------------------ ranking views

  function renderRanking() {
    const st = (S.ranking && S.ranking.strategies) || {};
    const names = ORDER.filter(n => st[n]).concat(Object.keys(st).filter(n => !ORDER.includes(n)));
    radar.setRanking(S.ranking);

    let priced = 0, positive = 0, best = null;
    for (const n of names) {
      priced += st[n].priced || 0;
      positive += st[n].positive || 0;
      const top = (st[n].top || [])[0];
      if (top && (!best || top.edge > best.edge)) best = { ...top, strategy: n };
    }
    setText($('#radar-count'), names.length ? num(priced) : '—');
    setText($('#radar-sub'), names.length ? (positive ? `個目標即時定價・${positive} 個正邊際` : '個目標即時定價') : '個目標即時定價');
    setHTML($('#radar-legend'), names.map(n => {
      const s = strat(n), top = (st[n].top || [])[0];
      return html`<div><i style="background:${s.color};color:${s.color}"></i><b class="${top && top.edge >= 0 ? 'pos' : ''}">${top ? cents(top.edge) : '—'}</b>${s.name}</div>`;
    }));
    if (best) {
      setText(kv('best'), cents(best.edge));
      kv('best').className = 'v' + (best.edge >= 0 ? ' pos' : '');
      setText(ks('best'), `${strat(best.strategy).short}・${best.title}`);
    }

    if (names.length && !names.includes(S.nearTab)) S.nearTab = names[0];
    setHTML($('#near-tabs'), (names.length ? names : ORDER).map(n => html`<button type="button" data-k="${n}" class="${n === S.nearTab ? 'on' : ''}">${strat(n).name}</button>`));
    renderNear();

    renderHist(st, names);
  }

  const BIN_LABELS = ['<−10', '−10', '−5', '−3', '−2', '−1', '−½', '0', '+½', '+1'];
  const BIN_TITLES = ['低於 −10¢', '−10 至 −5¢', '−5 至 −3¢', '−3 至 −2¢', '−2 至 −1¢', '−1 至 −0.5¢', '−0.5 至 0¢', '0 至 +0.5¢（可套利）', '+0.5 至 +1¢（可套利）', '+1¢ 以上（可套利）'];

  function renderHist(st, names) {
    const hist = $('#hist');
    if (!names.length) {
      S.sigs.hist = '';
      setHTML(hist, html`<div class="empty"><b>計算中</b>全市場訂單簿載入後約 20 秒出現</div>`);
      return;
    }
    if (S.sigs.hist !== names.join()) {
      S.sigs.hist = names.join();
      setHTML(hist, names.map(n => html`<div class="hist-row" data-h="${n}">
        <h4>${chip(n)}<span class="meta"></span></h4>
        <div class="bars">${BIN_TITLES.map((title, i) => html`<div class="bar" style="height:4%;--bc:${i >= 7 ? '#2ef2a0' : strat(n).color}" data-title="${title}"><span></span></div>`)}</div>
        <div class="bins">${BIN_LABELS.map(b => html`<span>${b}</span>`)}</div>
      </div>`));
      void hist.offsetHeight;
    }
    for (const n of names) {
      const row = $(`#hist [data-h="${n}"]`), s = st[n], counts = s.hist || [];
      const max = Math.max(1, ...counts);
      setHTML(row.querySelector('.meta'), html`${num(s.priced)} 已定價・<b class="${s.positive ? 'pos' : ''}">${num(s.positive)}</b> 正邊際`);
      row.querySelectorAll('.bar').forEach((bar, i) => {
        const c = counts[i] || 0;
        bar.style.height = Math.max(4, Math.sqrt(c / max) * 100).toFixed(1) + '%';
        bar.title = `${bar.dataset.title}：${num(c)} 個目標`;
        setText(bar.firstElementChild, c ? num(c) : '');
      });
    }
    setText($('#hist-hint'), S.ranking.at ? `單位 ¢／組・${hhmmss(S.ranking.at)} 更新` : '每 20 秒重算');
  }

  function renderNear() {
    const st = (S.ranking && S.ranking.strategies) || {};
    const rows = ((st[S.nearTab] || {}).top || []).slice(0, 30);
    const body = $('#near-table tbody');
    if (!rows.length) {
      setHTML(body, html`<tr><td colspan="4"><div class="empty">${S.ranking && S.ranking.at ? '此策略暫無可定價目標' : '全市場載入中…'}</div></td></tr>`);
      return;
    }
    setHTML(body, rows.map((r, i) => {
      const w = Math.max(3, Math.min(100, ((r.edge + 0.05) / 0.06) * 100));
      return html`<tr><td class="num muted hide-sm">${i + 1}</td><td class="title" title="${r.title}">${link(r.title, r.slug)}</td><td class="num hide-sm">${r.legs}</td><td class="num"><b class="${r.edge >= 0 ? 'pos' : ''}">${cents(r.edge)}</b><span class="edgebar"><i style="width:${w.toFixed(0)}%"></i></span></td></tr>`;
    }));
  }

  // ------------------------------------------------------------------ tick views

  const banners = new Map();
  function renderBanners(t) {
    const want = [];
    const risk = t.risk || {};
    if (!S.connected && S.tick) want.push(['conn', 'amber', ICON.wifi, '與伺服器的即時連線中斷', '正在自動重新連線；畫面上的數據可能已過時。']);
    if (t.requested_mode === 'live' && t.mode !== 'live') {
      want.push(['live-blocked', 'red', ICON.stop, '已要求實盤，但安全檢查未通過 —— 目前以紙盤運行', '未通過：' + (t.live_blocked || []).map(blockedText).join('、') + '。修正後重新啟動服務即可。']);
    } else if (t.requested_mode && t.requested_mode !== t.mode) {
      want.push(['live-blocked', 'amber', ICON.info, `無法識別 POLYARB_MODE=${t.requested_mode}`, '只接受 paper 或 live；目前以紙盤運行。']);
    }
    if (risk.killed) want.push(['killed', 'red', ICON.bolt, '熔斷已觸發，所有交易已停止', `${killText(risk.kill_reason || '')}。請先檢查持倉，確認無誤後按「重置熔斷」。`]);
    if (risk.paused) want.push(['paused', 'amber', ICON.pause, '交易已暫停', '機器人仍在監控與定價，但不會下單。按「恢復交易」繼續。']);
    if (S.meta.password_generated) want.push(['pw', 'amber', ICON.key, '控制台密碼是臨時隨機產生的', '請在 Zeabur 設定環境變數 POLYARB_DASHBOARD_PASSWORD，否則每次重啟密碼都會改變。']);
    const geo = t.geo || {};
    if (geo.api_ok === false) {
      const where = geo.error ? '未能確認伺服器所在地區' : `伺服器所在地區（${geo.country || '未知'}）不准 API 下單`;
      if (t.mode === 'live') want.push(['geo-live', 'red', ICON.globe, `${where}，實盤暫停下單`, '機器人約每 10 分鐘重新檢查一次，確認准許 API 下單後自動恢復。']);
      else want.push(['geo', 'blue', ICON.globe, where, '紙盤不受影響；轉實盤前需把服務部署到准許 API 下單的地區（例如 Zeabur 東京或香港），詳見右下「轉實盤」步驟。']);
    }
    if (['idle', 'geo', 'loading', 'sweeping'].includes(t.phase)) {
      want.push(['loading', 'blue', ICON.load, '正在載入全市場', '首次啟動需讀取約 4,600 個事件、35,000 個訂單簿，約 30–60 秒。', true]);
    }
    if (t.phase === 'stopped' || t.phase === 'stopping') want.push(['stopped', 'red', ICON.stop, '引擎已停止', '服務正在關閉或重啟中。']);

    const box = $('#banners');
    const ids = new Set(want.map(w => w[0]));
    for (const [id, el] of banners) if (!ids.has(id)) { el.remove(); banners.delete(id); }
    for (const [id, kind, icon, title, text, spin] of want) {
      let el = banners.get(id);
      if (!el) {
        el = document.createElement('div');
        el.className = `banner ${kind}`;
        setHTML(el, html`<span class="b-icon${spin ? ' spin' : ''}">${new Safe(icon)}</span><div class="b-text"><b></b><span></span></div>`);
        box.append(el);
        banners.set(id, el);
      }
      setText(el.querySelector('b'), title);
      setText(el.querySelector('.b-text span'), text);
    }
  }

  function renderBadges(t) {
    if (S.mode !== t.mode) {
      S.mode = t.mode;
      const live = t.mode === 'live';
      const b = $('#mode-badge');
      b.className = 'badge ' + (live ? 'live' : 'paper');
      setHTML(b, live ? html`<i></i><b>實盤</b><small>LIVE</small>` : html`<i></i><b>紙盤</b><small>PAPER</small>`);
    }
    if (S.phase !== t.phase) {
      S.phase = t.phase;
      const [label, cls] = PHASES[t.phase] || [t.phase, 'busy'];
      const b = $('#phase-badge');
      b.className = 'badge phase ' + cls;
      setHTML(b, html`<i></i><b>${label}</b>`);
    }
  }

  function renderHero(t) {
    const k = t.kpi;
    const total = k.pnl_total + k.locked_profit;
    const hero = $('#pnl-total');
    tween(hero, total, v => usd(v, true), 1100);
    hero.classList.toggle('neg', total < -0.005);
    tween($('#pnl-realized'), k.pnl_total, v => usd(v, true));
    tween($('#pnl-today'), k.pnl_today, v => usd(v, true));
    tween($('#pnl-locked'), k.locked_profit, v => usd(v, true));
    tween($('#pnl-roi'), k.bankroll ? total / k.bankroll : null, v => pct(v, 2));
    tween($('#exec-count'), k.executions, num, 600);
    $('#pnl-realized').className = k.pnl_total < -0.005 ? 'neg' : '';
    $('#pnl-today').className = k.pnl_today < -0.005 ? 'neg' : '';
    setText($('#pnl-hint'), (t.mode === 'live' ? '實盤' : '紙盤模擬') + '・已實現 + 已鎖定（結算後入賬）');
    document.title = `${usd(total, true)} · POLYARB ${t.mode === 'live' ? '實盤' : '紙盤'}`;
  }

  function renderKpis(t) {
    const k = t.kpi, e = t.engine, r = t.risk;
    tween(kv('available'), k.available, v => usd(v));
    setText(ks('available'), `本金 ${usd(k.bankroll)}`);
    tween(kv('exposure'), k.hold_exposure, v => usd(v));
    setText(ks('exposure'), `上限 ${usd(r.max_hold_exposure_usd)}・${pct(r.max_hold_exposure_usd ? k.hold_exposure / r.max_hold_exposure_usd : null, 0)}`);
    setText(kv('rate'), k.complete_rate == null ? '—' : pct(k.complete_rate, 1));
    setText(ks('rate'), k.executions ? `${num(k.executions)} 次執行・${num(k.trades)} 筆交易` : '尚未執行');
    tween(kv('opps'), k.unique_opportunities, num, 600);
    setText(ks('opps'), `共觸發 ${num(k.opportunities)} 次`);
    const targets = Object.values(e.targets || {}).reduce((a, b) => a + b, 0);
    tween(kv('targets'), targets, num, 600);
    setText(ks('targets'), `${num(e.events)} 事件・${num(e.tokens)} 訂單簿`);
    setText(kv('fresh'), e.books ? pct(e.fresh / e.books, 1) : '—');
    setText(ks('fresh'), `${num(e.fresh)} / ${num(e.books)}`);
    setText(kv('stream'), finite(e.ws_rate) ? `${nf0.format(e.ws_rate)}/秒` : e.ws_running ? '連線中' : '—');
    setText(ks('stream'), `${num(e.ws_tokens)} 個熱門簿・重連 ${num(e.ws_reconnects)}`);
  }

  function renderEngine(t) {
    const e = t.engine;
    setText(ev('uptime'), dur(e.uptime_s));
    setText(es_('uptime'), S.meta.started_at ? `啟動於 ${hhmm(S.meta.started_at)}` : '');
    setText(ev('sweep'), e.last_sweep_at ? `${e.last_sweep_s.toFixed(1)} 秒` : '—');
    setText(es_('sweep'), `每 ${e.sweep_every_s} 秒・第 ${num(e.sweeps)} 次`);
    setText(ev('refresh'), ago(e.last_refresh_at));
    setText(es_('refresh'), `每 ${e.refresh_minutes} 分鐘重建`);
    setHTML(ev('ws'), html`<span class="dot ${e.ws_running ? 'ok' : 'bad'}"></span>${e.ws_running ? '已連線' : '未連線'}`);
    setText(es_('ws'), `${num(e.ws_tokens)} 簿・重連 ${num(e.ws_reconnects)}`);
    setText(ev('cpu'), finite(e.cpu_pct) ? `${e.cpu_pct.toFixed(1)}%` : '—');
    setText(es_('cpu'), '佔單一核心');
    setText(ev('rss'), finite(e.rss_mb) ? `${nf0.format(e.rss_mb)} MB` : '—');
    setText(es_('rss'), 'RSS 常駐記憶體');
    setText(ev('eval'), finite(e.eval_rate) ? `${nf0.format(e.eval_rate)}/秒` : '—');
    setText(es_('eval'), `錯誤 ${num(e.eval_errors)}`);
    setText(ev('clients'), `${num(e.clients)} 個`);
    setText(es_('clients'), `執行中 ${num(e.inflight)}`);
    setText($('#engine-hint'), `更新於 ${hhmmss(t.now)}`);
    const sec = Math.max(2.5, Math.min(8, 8 - Math.log10((e.ws_rate || 0) + 1) * 1.8));
    const streak = (Math.round(sec * 2) / 2).toFixed(1) + 's';
    if (S.streak !== streak) { S.streak = streak; document.documentElement.style.setProperty('--streak', streak); }
  }

  function renderRisk(t) {
    const r = t.risk, k = t.kpi;
    const pill = $('#kill-pill');
    const [txt, cls] = r.killed ? ['熔斷', 'bad'] : r.paused ? ['已暫停', 'warn'] : ['正常運作', 'ok'];
    setText(pill, txt);
    pill.className = 'pill ' + cls;
    const holdFrac = r.max_hold_exposure_usd ? k.hold_exposure / r.max_hold_exposure_usd : 0;
    setGauge('hold', holdFrac, pct(holdFrac, 0), `$${nf0.format(k.hold_exposure)}/${nf0.format(r.max_hold_exposure_usd)}`);
    const loss = Math.max(0, -k.pnl_today);
    const lossFrac = r.max_daily_loss_usd ? loss / r.max_daily_loss_usd : 0;
    setGauge('loss', lossFrac, pct(lossFrac, 0), `$${nf0.format(loss)}/${nf0.format(r.max_daily_loss_usd)}`);
    setGauge('fail', r.max_consecutive_failures ? r.consecutive_failures / r.max_consecutive_failures : 0, `${r.consecutive_failures}`, `上限 ${r.max_consecutive_failures} 次`);
    const limits = [['單筆上限', usd(r.max_trade_usd)], ['單一事件上限', usd(r.max_event_exposure_usd)], ['可用資金', usd(k.available)], ['本金', usd(k.bankroll)]];
    const lsig = JSON.stringify(limits);
    if (S.sigs.limits !== lsig) {
      S.sigs.limits = lsig;
      setHTML($('#limits'), limits.map(([a, b]) => html`<div><span>${a}</span><b>${b}</b></div>`));
    }
    const skips = r.skips || [];
    const ssig = JSON.stringify(skips);
    if (S.sigs.skips !== ssig) {
      S.sigs.skips = ssig;
      const max = Math.max(1, ...skips.map(s => s[1]));
      setHTML($('#skips'), skips.length
        ? html`<h4>未下單原因（累計）</h4>${skips.map(([name, c]) => html`<div class="skip" title="${name}"><span class="sk-name">${translate(SKIPS, name)}</span><span class="sk-bar"><i style="width:${((c / max) * 100).toFixed(0)}%"></i></span><b>${num(c)}</b></div>`)}`
        : html`<h4>未下單原因</h4><div class="muted" style="font-size:12.5px">暫無 —— 所有機會均已處理</div>`);
    }
    for (const id of ['#btn-pause', '#btn-pause-2']) {
      const b = $(id);
      b.disabled = false;
      setText(b, r.paused ? '恢復交易' : '暫停交易');
      b.className = 'btn small ' + (r.paused ? 'go' : 'warn');
    }
    $('#btn-reset').disabled = !r.killed;
  }

  function renderHolds(t) {
    const holds = t.holds || [];
    const sig = JSON.stringify(holds);
    if (S.sigs.holds === sig) return;
    S.sigs.holds = sig;
    const locked = holds.reduce((a, h) => a + (h.profit || 0), 0);
    setText($('#hold-count'), holds.length ? `${holds.length} 筆・鎖定 ${usd(locked, true)}` : '0');
    const body = $('#holds-table tbody');
    if (!holds.length) {
      setHTML(body, html`<tr><td colspan="6"><div class="empty"><b>暫無持倉</b>YES 籃子與階梯套利會持有至市場結算，屆時保證派彩自動入賬。</div></td></tr>`);
      return;
    }
    setHTML(body, holds.map(h => html`<tr>
      <td class="title" title="${h.title}">${link(h.title, h.slug)}</td><td class="hide-sm">${chip(h.strategy)}</td>
      <td class="num">${usd(h.cost)}</td><td class="num hide-sm">${usd(h.payout)}</td><td class="num pos">${usd(h.profit, true)}</td>
      <td class="num" title="${h.resolves_at || ''}">${until(h.resolves_at)}</td></tr>`));
  }

  function renderReadiness(t) {
    const items = t.readiness || [];
    const sig = JSON.stringify([items, t.mode, t.requested_mode, t.wallet, t.geo]);
    if (S.sigs.ready === sig) return;
    S.sigs.ready = sig;
    const crit = items.filter(i => i.critical), ok = crit.filter(i => i.ok).length;
    const pill = $('#ready-pill');
    let label, cls;
    if (t.mode === 'live') [label, cls] = ['實盤運行中', 'ok'];
    else if (t.requested_mode === 'live') [label, cls] = ['未能啟動實盤', 'bad'];
    else if (crit.length && ok === crit.length) [label, cls] = ['可隨時轉實盤', 'ok'];
    else [label, cls] = [`${ok}/${crit.length} 項就緒`, 'warn'];
    setText(pill, label);
    pill.className = 'pill ' + cls;
    setHTML($('#checklist'), items.map(i => {
      const [code, desc] = READY[i.id] || [i.id, ''];
      const cls2 = i.ok ? 'ok' : i.critical ? 'no' : 'info';
      const det = i.id === 'mode' ? (i.detail === 'live' ? 'live' : '目前 ' + (i.detail || 'paper')) : i.id === 'geo' ? geoDetail(t.geo || {}, i.detail) : i.ok ? '' : '未完成';
      return html`<li class="${cls2}"><span class="ic">${i.ok ? '✓' : i.critical ? '✕' : 'i'}</span><div><code>${code}</code><div class="desc">${desc}</div></div><span class="det">${det}</span></li>`;
    }));
    const w = t.wallet;
    setText($('#wallet-note'), t.mode === 'live' && w
      ? `交易錢包（入金地址）${w.funder || '?'}${w.type ? `〔${WALLETS[w.type] || w.type}〕` : ''}・簽名 ${w.signer || '?'}${finite(w.balance) ? `・餘額 ${usd(w.balance)} pUSD` : ''}`
      : '紙盤：每筆成交都按當刻真實訂單簿深度模擬，不會動用任何資金。實盤與紙盤帳本分開保存。');
  }

  // ------------------------------------------------------------------ event handlers

  function onState(s) {
    S.meta = s.meta || {};
    S.series = s.series || null;
    S.ranking = s.ranking || null;
    S.opps = s.opportunities || [];
    S.execs = s.executions || [];
    renderFeed($('#opp-feed'), S.opps, oppItem, EMPTY_OPPS);
    renderFeed($('#exec-feed'), S.execs, execItem, EMPTY_EXECS);
    appendLogs(s.logs || [], true);
    onTick(s);
    renderRanking();
    drawCharts();
    setText($('#foot-left'), `POLYARB v${S.meta.version || '?'}・數據目錄 ${S.meta.data_dir || '?'}`);
  }

  function onTick(t) {
    S.tick = t;
    skew = t.now - Date.now() / 1000;
    renderBadges(t);
    renderHero(t);
    renderKpis(t);
    renderEngine(t);
    renderRisk(t);
    renderHolds(t);
    renderReadiness(t);
    renderBanners(t);
    setText($('#opp-count'), `${num(t.kpi.unique_opportunities)} 個`);
    setText($('#exec-pill'), `${num(t.kpi.executions)} 次`);
  }

  function onOpportunity(o) {
    S.opps.push(o);
    if (S.opps.length > 200) S.opps.shift();
    prependFeed($('#opp-feed'), oppItem(o, true));
    radar.ping(o.strategy);
  }

  function onExecution(x) {
    S.execs.push(x);
    if (S.execs.length > 200) S.execs.shift();
    prependFeed($('#exec-feed'), execItem(x, true));
    const hero = $('#pnl-total');
    hero.classList.remove('flash');
    void hero.offsetWidth;
    hero.classList.add('flash');
    radar.ping(x.strategy);
    if (x.orphaned) toast(`需人手檢查：${x.title}`, 'bad');
    else toast(`${x.mode === 'live' ? '實盤' : '紙盤'}成交：${x.title}・預期利潤 ${usd(x.expected_profit, true)}`, x.complete ? 'good' : '');
  }

  function onRelease(h) {
    toast(`持倉已結算：${h.title || h.key}・${usd(h.pnl, true)}`, 'good');
  }

  // ------------------------------------------------------------------ live stream

  let source = null, lastMessage = 0;
  function setConn(ok) {
    S.connected = ok;
    $('#conn').className = 'conn ' + (ok ? 'ok' : 'bad');
    if (S.tick) renderBanners(S.tick);
  }
  let blinkTimer;
  function blink() {
    const c = $('#conn');
    c.classList.add('blink');
    clearTimeout(blinkTimer);
    blinkTimer = setTimeout(() => c.classList.remove('blink'), 180);
  }

  async function checkAuth() {
    try {
      const r = await fetch('/api/state', { credentials: 'same-origin', cache: 'no-store' });
      if (r.status === 401) location.replace('/login');
    } catch (_) { /* server unreachable; EventSource keeps retrying */ }
  }

  function connect() {
    if (source) source.close();
    lastMessage = Date.now();
    source = new EventSource('/api/stream');
    source.onopen = () => setConn(true);
    source.onerror = () => {
      setConn(false);
      if (source.readyState === EventSource.CLOSED) setTimeout(connect, 3000);
      checkAuth();
    };
    const on = (name, fn) => source.addEventListener(name, e => {
      lastMessage = Date.now();
      if (!S.connected) setConn(true);
      blink();
      let data;
      try { data = JSON.parse(e.data); } catch (_) { return; }
      try { fn(data); } catch (err) { console.error('handler', name, err); }
    });
    on('state', onState);
    on('tick', onTick);
    on('sample', p => { pushSample(p); drawCharts(); });
    on('ranking', r => { S.ranking = r; renderRanking(); });
    on('opportunity', onOpportunity);
    on('execution', onExecution);
    on('log', e => appendLogs([e], false));
    on('hold_released', onRelease);
    on('universe', u => toast(`市場已刷新：${num(u.events)} 個事件・${num(u.tokens)} 個訂單簿`));
  }

  setInterval(() => { if (Date.now() - lastMessage > 20000) connect(); }, 5000);

  // ------------------------------------------------------------------ controls

  let toastTimer;
  function toast(msg, kind) {
    const el = $('#toast');
    el.textContent = msg;
    el.className = 'toast on' + (kind ? ' ' + kind : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('on'), 3600);
  }

  async function post(url, body) {
    const r = await fetch(url, {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}),
    });
    if (r.status === 401) { location.replace('/login'); throw new Error('登入已過期'); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || 'HTTP ' + r.status);
    return data;
  }

  function modal({ title, text, expect, okText }) {
    return new Promise(resolve => {
      const m = $('#modal'), input = $('#modal-input'), ok = $('#modal-ok'), cancel = $('#modal-cancel');
      setText($('#modal-title'), title);
      setText($('#modal-text'), text);
      input.value = '';
      input.placeholder = expect || '';
      input.style.display = expect ? '' : 'none';
      setText(ok, okText || '確認');
      ok.disabled = !!expect;
      m.classList.remove('hidden');
      setTimeout(() => (expect ? input : ok).focus(), 30);
      const onKey = e => {
        if (e.key === 'Escape') done(null);
        else if (e.key === 'Enter' && !ok.disabled) done(input.value.trim());
      };
      const done = v => {
        m.classList.add('hidden');
        input.oninput = ok.onclick = cancel.onclick = m.onclick = null;
        document.removeEventListener('keydown', onKey);
        resolve(v);
      };
      input.oninput = () => { ok.disabled = !!expect && input.value.trim() !== expect; };
      ok.onclick = () => done(input.value.trim());
      cancel.onclick = () => done(null);
      m.onclick = e => { if (e.target === m) done(null); };
      document.addEventListener('keydown', onKey);
    });
  }

  async function control(action, extra) {
    const res = await post('/api/control', { action, ...(extra || {}) });
    if (S.tick && res.risk) { S.tick.risk = res.risk; renderRisk(S.tick); renderBanners(S.tick); }
    return res;
  }

  async function togglePause() {
    const paused = S.tick && S.tick.risk && S.tick.risk.paused;
    if (!paused) {
      const ok = await modal({ title: '暫停交易？', text: '機器人會繼續監控與定價，但不會再下任何新單。進行中的執行會先完成。', okText: '暫停' });
      if (ok === null) return;
    }
    try {
      await control(paused ? 'resume' : 'pause');
      toast(paused ? '已恢復交易' : '已暫停交易', paused ? 'good' : '');
    } catch (err) { toast('操作失敗：' + err.message, 'bad'); }
  }

  $('#btn-pause').addEventListener('click', togglePause);
  $('#btn-pause-2').addEventListener('click', togglePause);

  $('#btn-reset').addEventListener('click', async () => {
    const reason = (S.tick && S.tick.risk && S.tick.risk.kill_reason) || '';
    const v = await modal({ title: '重置熔斷', text: `熔斷原因：${killText(reason)}。請先在 Polymarket 確認所有持倉正常，然後輸入 RESET 確認。`, expect: 'RESET', okText: '重置' });
    if (v === null) return;
    try { await control('reset_kill', { confirm: v }); toast('熔斷已重置，恢復自動交易', 'good'); } catch (err) { toast('重置失敗：' + err.message, 'bad'); }
  });

  $('#btn-logout').addEventListener('click', async () => {
    try { await post('/api/logout'); } catch (_) { /* leave anyway */ }
    location.replace('/login');
  });

  $('#pnl-range').addEventListener('click', e => {
    const b = e.target.closest('button[data-r]');
    if (!b) return;
    S.range = +b.dataset.r;
    $$('#pnl-range button').forEach(x => x.classList.toggle('on', x === b));
    drawCharts();
  });

  $('#near-tabs').addEventListener('click', e => {
    const b = e.target.closest('button[data-k]');
    if (!b) return;
    S.nearTab = b.dataset.k;
    $$('#near-tabs button').forEach(x => x.classList.toggle('on', x === b));
    renderNear();
  });

  $('#log-filter').addEventListener('click', e => {
    const b = e.target.closest('button[data-f]');
    if (!b) return;
    S.logFilter = b.dataset.f;
    $$('#log-filter button').forEach(x => x.classList.toggle('on', x === b));
    consoleEl.classList.toggle('f-trade', S.logFilter === 'trade');
    consoleEl.classList.toggle('f-warn', S.logFilter === 'warn');
    consoleEl.scrollTop = consoleEl.scrollHeight;
  });

  document.addEventListener('click', async e => {
    const b = e.target.closest('[data-copy]');
    if (!b) return;
    const text = b.dataset.copy;
    try {
      await navigator.clipboard.writeText(text);
    } catch (_) {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.append(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
    }
    toast('已複製：' + text);
  });

  // ------------------------------------------------------------------ clocks

  setInterval(() => {
    const d = new Date();
    setText($('#clock'), `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`);
  }, 1000);
  setInterval(() => {
    for (const el of $$('.ago[data-ts]')) setText(el, ago(+el.dataset.ts));
    if (S.tick) setText(ev('refresh'), ago(S.tick.engine.last_refresh_at));
  }, 5000);

  connect();
})();
