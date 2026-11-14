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
    metar_lock: { name: 'METAR 氣象鎖定', short: 'METAR 鎖定', color: '#2ef2a0' },
    binance_lock: { name: '幣安收盤鎖定', short: '幣安鎖定', color: '#f59e0b' },
    crypto_short: { name: '5m/15m/1h 短週期套利', short: '短週期加密', color: '#06b6d4' },
    micro_arb: { name: '5分鐘微利', short: '5分鐘微利', color: '#f97316' },
    twin_arb: { name: '雙生跨盤套利', short: '雙生跨盤', color: '#f472b6' },
    maker_convert: { name: '零費掛單轉換', short: '掛單轉換', color: '#38bdf8' },
    maker_metar: { name: '零費 METAR 保底', short: 'METAR 搶單', color: '#34d399' },
    maker_binance: { name: '零費幣安保底', short: '幣安搶單', color: '#fbbf24' },
    maker_crypto_short: { name: '零費 5m/15m/1h 保底', short: '短週期搶單', color: '#22d3ee' },
    maker_crypto_pair: { name: '零費 5m/15m/1h 跨盤', short: '短週期跨盤', color: '#67e8f9' },
    maker_micro_merge: { name: '零費 5分鐘同盤合成', short: '同盤合成', color: '#fb923c' },
    maker_micro_corridor: { name: '零費 5分鐘走廊', short: '走廊掛單', color: '#fdba74' },
    maker_ladder: { name: '零費掛單階梯', short: '掛單階梯', color: '#fb7185' },
    maker_yes_basket: { name: '零費掛單籃子', short: '掛單籃子', color: '#a78bfa' },
    maker_twin: { name: '零費掛單雙生', short: '掛單雙生', color: '#f9a8d4' },
    negrisk_yes_basket: { name: 'YES 籃子', short: 'YES 籃子', color: '#8b6cff' },
    ladder: { name: '階梯套利', short: '階梯', color: '#ffc857' },
  };
  const ORDER = ['negrisk_convert', 'micro_arb', 'crypto_short', 'metar_lock', 'binance_lock', 'twin_arb', 'negrisk_yes_basket', 'ladder'];
  const strat = k => STRATS[k] || { name: k || '?', short: k || '?', color: '#5aa9ff' };

  const SKINS = {
    spatial: {
      key: '1', short: '量子玻璃', name: '量子空間玻璃', sub: 'visionOS 光學磨砂・雙層極光帶',
      bg: '#04060f', scheme: 'dark', main: '#2ef2a0', real: '#19e6d4', step: false,
      swatchBg: 'linear-gradient(135deg,#04060f,#0b1d2e,#1c1238)',
    },
    obsidian: {
      key: '2', short: '黑曜鎏金', name: '黑曜石鎏金私銀', sub: '24K 香檳金細線・瑞士私銀終端',
      bg: '#08080a', scheme: 'dark', main: '#e5b869', real: '#10b981', step: false,
      swatchBg: 'linear-gradient(135deg,#08080a,#1f1a12,#2c2314)',
    },
    cyber: {
      key: '3', short: '超頻脈衝', name: '超頻脈衝矩陣', sub: '電光青激光階梯・戰術聲納 HUD',
      bg: '#020611', scheme: 'dark', main: '#00ff9d', real: '#00f0ff', step: true,
      swatchBg: 'linear-gradient(135deg,#020611,#032136,#042e27)',
    },
    titanium: {
      key: '4', short: '極晝鈦銀', name: '極晝鈦銀瑞士白', sub: '陶瓷純白高對比・戶外強光清晰',
      bg: '#f3f5fa', scheme: 'light', main: '#2563eb', real: '#059669', step: false,
      swatchBg: 'linear-gradient(135deg,#ffffff,#e8eef9,#dbeafe)',
    },
    orbital: {
      key: '5', short: '星際極光', name: '星際極光方舟', sub: '流體紫青極光絲帶・全息 Bento',
      bg: '#070514', scheme: 'dark', main: '#c084fc', real: '#38bdf8', step: false,
      swatchBg: 'linear-gradient(135deg,#09051c,#2e1065,#0c4a6e)',
    },
  };
  const SKIN_ORDER = ['spatial', 'obsidian', 'cyber', 'titanium', 'orbital'];

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
    [/^skip:liquid reserve protected$/, '保留流動預備金（只供閃電策略）'],
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
    [/^instant flip$/, '閃電止盈平倉（0.99+）'],
    [/^partial flip -> holding remainder$/, '部分閃電止盈・餘量待結算'],
    [/^converted \+ surplus sold$/, '已轉換 pUSD ＋ 免費 YES 殘值變現'],
    [/^converted$/, '已即時轉換為 pUSD'],
    [/^maker filled -> converted \(co-fill 0-fee!\) \+ surplus sold$/, '雙腿零費自對沖 → 轉換 pUSD ＋ 殘值變現'],
    [/^maker filled -> converted \(co-fill 0-fee!\)$/, '雙腿零費自對沖 → 已轉換 pUSD'],
    [/^maker filled -> converted \+ surplus sold$/, '零費掛單成交 → 轉換 pUSD ＋ 殘值變現'],
    [/^maker filled -> converted$/, '零費掛單成交 → 已轉換 pUSD'],
    [/^maker lock filled -> 0\.995 flip$/, '零對沖保底成交 → 自動掛 0.995 止盈'],
    [/^maker filled -> holding to resolution$/, '零費掛單成交 → 對沖持有'],
    [/^partial hedge -> converted subset$/, '部分對沖 → 轉換已成交子集'],
    [/^maker hedge missed -> unwind$/, '對沖未滿 → 已平倉保護'],
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

  function stepPath(pts) {
    if (!pts.length) return '';
    let d = `M${r1(pts[0][0])},${r1(pts[0][1])}`;
    for (let i = 1; i < pts.length; i++) {
      d += `L${r1(pts[i][0])},${r1(pts[i - 1][1])}L${r1(pts[i][0])},${r1(pts[i][1])}`;
    }
    return d;
  }

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

      // Optional bottom volume/activity pulse bars (Concept 5 & Hero chart)
      if (data.bars && data.bars.length === ts.length) {
        let maxB = 0;
        for (let i = i0; i < ts.length; i++) if (finite(data.bars[i]) && data.bars[i] > maxB) maxB = data.bars[i];
        if (maxB > 0) {
          const bw = Math.max(1.5, Math.min(6, iw / Math.max(24, ts.length - i0) * 0.65));
          const bCol = data.barColor || CSSV('--accent-2') || '#8b6cff';
          for (let i = i0; i < ts.length; i++) {
            const bv = data.bars[i];
            if (!finite(bv) || bv <= 0) continue;
            const bh = Math.max(2, (bv / maxB) * ih * 0.22);
            plot.append(svg('rect', {
              x: r1(x(ts[i]) - bw / 2), y: r1(pt + ih - bh), width: r1(bw), height: r1(bh), rx: 1,
              fill: bCol, 'fill-opacity': 0.22,
            }));
          }
        }
      }

      // Optional vertical laser execution beams + markers (Concepts 1 & 3)
      if (data.markers && data.markers.length) {
        for (const m of data.markers) {
          if (!finite(m.ts) || m.ts < tStart || m.ts > tEnd) continue;
          const mx = r1(x(m.ts));
          const mc = m.color || CSSV('--pos') || '#2ef2a0';
          plot.append(svg('line', {
            x1: mx, x2: mx, y1: pt + 4, y2: pt + ih,
            stroke: mc, 'stroke-opacity': 0.28, 'stroke-width': 1.2, 'stroke-dasharray': '2 3',
          }));
          plot.append(svg('circle', { cx: mx, cy: pt + 6, r: 2.8, fill: mc }));
        }
      }

      const pathFn = opts.step ? stepPath : monotonePath;
      data.series.forEach((s, si) => {
        const runs = [];
        let run = [], prev = -1;
        for (let i = i0; i < ts.length; i++) {
          const v = s.values[i];
          if (finite(v)) {
            if (run.length && ts[i] - ts[prev] > opts.gap) { runs.push(run); run = []; }
            run.push([x(ts[i]), y(v)]);
            prev = i;
          } else if (run.length) { runs.push(run); run = []; }
        }
        if (run.length) runs.push(run);
        if (s.area) {
          const gid = `${this.uid}-g${si}`;
          const lg = svg('linearGradient', { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 });
          lg.append(svg('stop', { offset: 0, 'stop-color': s.color, 'stop-opacity': 0.34 }), svg('stop', { offset: 1, 'stop-color': s.color, 'stop-opacity': 0 }));
          defs.append(lg);
          for (const r of runs) if (r.length > 1) {
            plot.append(svg('path', { d: pathFn(r) + `L${r1(r[r.length - 1][0])},${r1(base)}L${r1(r[0][0])},${r1(base)}Z`, fill: `url(#${gid})` }));
          }
        }
        for (const r of runs) {
          const attrs = { d: pathFn(r), fill: 'none', stroke: s.color, 'stroke-width': s.width || 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' };
          if (s.dash) attrs['stroke-dasharray'] = s.dash;
          if (s.glow) attrs.style = `filter: drop-shadow(0 0 6px ${hexA(s.color, 0.55)})`;
          plot.append(svg('path', attrs));
        }
        const last = runs.length ? runs[runs.length - 1] : null;
        if (s.glow && last) {
          const [lx, ly] = last[last.length - 1];
          plot.append(svg('circle', { class: 'pulse-dot', cx: r1(lx), cy: r1(ly), r: 3.5, fill: 'none', stroke: s.color, 'stroke-width': 1.6 }));
          plot.append(svg('circle', { cx: r1(lx), cy: r1(ly), r: 3.8, fill: s.color, style: `filter: drop-shadow(0 0 7px ${s.color})` }));
        }
      });
      root.append(plot);

      this.cross = svg('line', { class: 'cross', x1: 0, x2: 0, y1: pt, y2: pt + ih, visibility: 'hidden' });
      const dotBg = CSSV('--bg') || '#04060f';
      this.dots = data.series.map(s => svg('circle', { r: 4.2, fill: dotBg, stroke: s.color, 'stroke-width': 2.2, visibility: 'hidden' }));
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
        rows.push(html`<div class="row"><span class="sw" style="background:${s.color}"></span>${s.label}<span style="margin-left:auto;color:var(--text);font-family:var(--mono)">${(s.fmt || this.opts.yFmt)(val)}</span></div>`);
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

      const isLight = (SKINS[S.skin] && SKINS[S.skin].scheme === 'light');
      const sweepHex = (SKINS[S.skin] && SKINS[S.skin].main) || '#2ef2a0';
      const ringLine = isLight ? 'rgba(15,23,42,0.11)' : 'rgba(255,255,255,0.09)';
      const borderLine = isLight ? 'rgba(15,23,42,0.16)' : 'rgba(255,255,255,0.12)';
      const labelFill = isLight ? 'rgba(71,85,105,0.78)' : 'rgba(138,146,180,0.6)';
      const centerFill = isLight ? 'rgba(15,23,42,0.88)' : 'rgba(232,235,247,0.92)';

      let g = ctx.createRadialGradient(cx, cy, 0, cx, cy, R);
      g.addColorStop(0, hexA(sweepHex, isLight ? 0.12 : 0.09));
      g.addColorStop(0.35, hexA('#19e6d4', isLight ? 0.05 : 0.03));
      g.addColorStop(1, hexA('#8b6cff', isLight ? 0.05 : 0.035));
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, TAU);
      ctx.fill();

      this.sectors.forEach((name, si) => {
        const a0 = -Math.PI / 2 + si * span, a1 = a0 + span, col = strat(name).color;
        ctx.fillStyle = hexA(col, isLight ? 0.06 : 0.035);
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.arc(cx, cy, R, a0 + gap, a1 - gap);
        ctx.closePath();
        ctx.fill();
        ctx.strokeStyle = ringLine;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(a0) * r0, cy + Math.sin(a0) * r0);
        ctx.lineTo(cx + Math.cos(a0) * R, cy + Math.sin(a0) * R);
        ctx.stroke();
        ctx.strokeStyle = hexA(col, 0.7);
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
        ctx.strokeStyle = ringLine;
        ctx.beginPath();
        ctx.arc(cx, cy, rr, 0, TAU);
        ctx.stroke();
        ctx.fillStyle = labelFill;
        ctx.fillText(label, cx + 4, cy - rr - 3);
      }
      ctx.setLineDash([]);
      ctx.strokeStyle = borderLine;
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, TAU);
      ctx.stroke();

      const W = 1.0;
      if (ctx.createConicGradient) {
        const cg = ctx.createConicGradient(sweep - W, cx, cy);
        const f = W / TAU;
        cg.addColorStop(0, hexA(sweepHex, 0));
        cg.addColorStop(f * 0.999, hexA(sweepHex, isLight ? 0.24 : 0.2));
        cg.addColorStop(f, hexA(sweepHex, 0));
        cg.addColorStop(1, hexA(sweepHex, 0));
        ctx.fillStyle = cg;
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.arc(cx, cy, R, sweep - W, sweep);
        ctx.closePath();
        ctx.fill();
      }
      ctx.save();
      ctx.strokeStyle = hexA(sweepHex, 0.85);
      ctx.lineWidth = 1.5;
      ctx.shadowColor = sweepHex;
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
      g.addColorStop(0, hexA(sweepHex, 0.45));
      g.addColorStop(0.55, hexA(sweepHex, 0.13));
      g.addColorStop(1, hexA(sweepHex, 0));
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(cx, cy, r0 * 2, 0, TAU);
      ctx.fill();
      ctx.strokeStyle = hexA(sweepHex, 0.45 + 0.3 * pulse);
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
      ctx.fillStyle = centerFill;
      ctx.fillText('套利', cx, cy);
      if (this.loading) {
        ctx.font = `500 12px ${FONT}`;
        ctx.fillStyle = labelFill;
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

  // ------------------------------------------------------------------ 3D Orbital Oracle Globe

  const AIRPORT_NODES = [
    [40.64, -73.78, 'KJFK'], [33.94, -118.4, 'KLAX'], [41.97, -87.9, 'KORD'], [25.79, -80.29, 'KMIA'],
    [47.45, -122.31, 'KSEA'], [39.86, -104.67, 'KDEN'], [30.19, -97.67, 'KAUS'], [29.99, -95.34, 'KIAH'],
    [51.5, 0.05, 'EGLC'], [48.86, 2.35, 'LFPG'], [52.36, 13.5, 'EDDB'], [60.32, 24.96, 'EFHK'],
    [40.47, -3.56, 'LEMD'], [41.8, 12.24, 'LIRF'], [32.01, 34.89, 'LLBG'], [25.25, 55.36, 'OMDB'],
    [22.31, 113.91, 'VHHH'], [35.55, 139.78, 'RJTT'], [37.46, 126.44, 'RKSI'], [1.36, 103.99, 'WSSS'],
    [25.08, 121.23, 'RCTP'], [-33.95, 151.18, 'YSSY'], [-23.43, -46.47, 'SBGR'], [-36.99, 174.79, 'NZAA'],
  ];

  class OrbitalGlobe {
    constructor(canvas) {
      this.c = canvas;
      if (!canvas) return;
      this.ctx = canvas.getContext('2d');
      this.rot = 0.6;
      this.drag = null;
      this.hotIcaos = new Set();
      this.resize();
      new ResizeObserver(() => this.resize()).observe(canvas);
      canvas.addEventListener('pointerdown', e => { this.drag = [e.clientX, this.rot]; canvas.setPointerCapture(e.pointerId); });
      canvas.addEventListener('pointermove', e => { if (this.drag) this.rot = this.drag[1] + (e.clientX - this.drag[0]) * 0.012; });
      canvas.addEventListener('pointerup', () => { this.drag = null; });
      requestAnimationFrame(t => this.frame(t));
    }
    resize() {
      if (!this.c) return;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const w = this.c.clientWidth || 186, h = this.c.clientHeight || 186;
      this.w = w; this.h = h;
      this.c.width = Math.round(w * dpr);
      this.c.height = Math.round(h * dpr);
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    frame(tNow) {
      if (!this.c) return;
      requestAnimationFrame(t => this.frame(t));
      if (document.hidden || !this.w) return;
      if (!this.drag && !REDUCED) this.rot += 0.0045;
      const ctx = this.ctx, cx = this.w / 2, cy = this.h / 2, R = Math.min(cx, cy) - 12;
      const sk = SKINS[S.skin] || SKINS.spatial;
      const isLight = sk.scheme === 'light';
      const col = sk.main, col2 = sk.real;
      ctx.clearRect(0, 0, this.w, this.h);

      const grd = ctx.createRadialGradient(cx - R * 0.3, cy - R * 0.3, R * 0.1, cx, cy, R);
      grd.addColorStop(0, hexA(col2, isLight ? 0.18 : 0.16));
      grd.addColorStop(1, hexA(col, isLight ? 0.04 : 0.02));
      ctx.fillStyle = grd;
      ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.fill();

      ctx.strokeStyle = isLight ? 'rgba(15,23,42,0.14)' : 'rgba(255,255,255,0.13)';
      ctx.lineWidth = 1;
      for (const lat of [-45, -15, 15, 45]) {
        const rad = (lat * Math.PI) / 180;
        const yy = cy - Math.sin(rad) * R * 0.92;
        const rx = Math.cos(rad) * R;
        ctx.beginPath(); ctx.ellipse(cx, yy, rx, rx * 0.18, 0, 0, Math.PI * 2); ctx.stroke();
      }
      for (let m = 0; m < 6; m++) {
        const a = this.rot + (m * Math.PI) / 6;
        ctx.beginPath(); ctx.ellipse(cx, cy, Math.abs(Math.sin(a)) * R, R, 0, 0, Math.PI * 2); ctx.stroke();
      }
      ctx.strokeStyle = hexA(col, 0.55);
      ctx.lineWidth = 1.4;
      ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.stroke();

      // Orbiting satellite ring
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate(-0.34);
      ctx.strokeStyle = hexA(col2, 0.45);
      ctx.lineWidth = 1.1;
      ctx.beginPath(); ctx.ellipse(0, 0, R + 8, (R + 8) * 0.3, 0, 0, Math.PI * 2); ctx.stroke();
      const satA = (tNow / 950) % (Math.PI * 2);
      ctx.fillStyle = col2;
      ctx.beginPath(); ctx.arc(Math.cos(satA) * (R + 8), Math.sin(satA) * (R + 8) * 0.3, 2.8, 0, Math.PI * 2); ctx.fill();
      ctx.restore();

      const tilt = 0.28;
      for (const [lat, lon, icao] of AIRPORT_NODES) {
        const phi = (lat * Math.PI) / 180;
        const lam = (lon * Math.PI) / 180 + this.rot;
        const x3 = Math.cos(phi) * Math.sin(lam);
        const y3 = Math.sin(phi) * Math.cos(tilt) - Math.cos(phi) * Math.cos(lam) * Math.sin(tilt);
        const z3 = Math.sin(phi) * Math.sin(tilt) + Math.cos(phi) * Math.cos(lam) * Math.cos(tilt);
        if (z3 < -0.05) continue;
        const px = cx + x3 * R, py = cy - y3 * R;
        const hot = this.hotIcaos.has(icao);
        ctx.fillStyle = hot ? col : col2;
        ctx.globalAlpha = 0.35 + 0.65 * Math.max(0, z3);
        ctx.beginPath(); ctx.arc(px, py, hot ? 3.2 : 2.1, 0, Math.PI * 2); ctx.fill();
        if (hot && z3 > 0.25) {
          ctx.font = `700 8.5px ${MONO}`;
          ctx.fillStyle = isLight ? '#0f172a' : '#ffffff';
          ctx.fillText(icao, px + 5, py + 3);
        }
      }
      ctx.globalAlpha = 1;
    }
  }

  // ------------------------------------------------------------------ state & skin manager

  const S = {
    meta: {}, tick: null, series: null, ranking: null, opps: [], execs: [], range: 86400,
    nearTab: ORDER[0], logFilter: 'all', sigs: {}, mode: null, phase: null, connected: false,
    skin: 'spatial', chosenSkin: 'spatial', autoDaylight: false, mobileView: 'overview',
  };
  const FEED_MAX = 60;
  const LOG_MAX = 500;
  let chartsQueued = false;

  const pnlChart = new LineChart($('#pnl-chart'), {
    yFmt: v => usd(v), minSpan: 0.5, range: S.range, empty: '累積數據中…（每 10 秒取樣）',
  });
  const tpChart = new LineChart($('#tp-chart'), {
    pad: [8, 12, 22, 44], yFmt: v => (finite(v) ? (v >= 1000 ? (v / 1000).toFixed(1) + 'k' : nf0.format(v)) : '—'),
    minSpan: 10, nonneg: true, empty: '累積數據中…',
  });
  const radar = new Radar($('#radar'), $('#radar-tip'));
  const globe = new OrbitalGlobe($('#oracle-globe'));

  function renderSkinCards() {
    const box = $('#skin-cards');
    if (!box) return;
    setHTML(box, SKIN_ORDER.map(id => {
      const sk = SKINS[id];
      const pathD = sk.step ? 'M2,28 L24,28 L24,18 L52,18 L52,11 L82,11 L82,4 L108,4' : 'M2,28 C25,26 35,16 56,15 C78,14 88,5 108,4';
      return html`<button type="button" class="skin-card${S.chosenSkin === id ? ' on' : ''}" data-pick-skin="${id}">
        <div class="skin-swatch" style="background:${sk.swatchBg}">
          <span class="skin-key-num">${sk.key}</span>
          <svg viewBox="0 0 110 32" preserveAspectRatio="none">
            <path d="${pathD}" fill="none" stroke="${sk.main}" stroke-width="2.4" stroke-linecap="round"/>
            <path d="M2,30 C32,28 65,22 108,16" fill="none" stroke="${sk.real}" stroke-width="1.5" stroke-dasharray="3 3"/>
          </svg>
        </div>
        <div>
          <b>${sk.key}. ${sk.name}</b>
          <small>${sk.sub}</small>
        </div>
      </button>`;
    }));
  }

  function effectiveSkinId(chosen) {
    if (S.autoDaylight) {
      const hr = new Date().getHours();
      if (hr >= 7 && hr < 18) return 'titanium';
    }
    return SKINS[chosen] ? chosen : 'spatial';
  }

  function applySkin(chosen, persistServer = false) {
    if (!SKINS[chosen]) chosen = 'spatial';
    S.chosenSkin = chosen;
    const active = effectiveSkinId(chosen);
    S.skin = active;
    const sk = SKINS[active];
    document.documentElement.setAttribute('data-skin', active);
    document.documentElement.style.colorScheme = sk.scheme;
    const mt = $('#meta-theme-color');
    if (mt) mt.setAttribute('content', sk.bg);
    setText($('#cur-skin-short'), sk.short);
    pnlChart.opts.step = !!sk.step;
    setHTML($('#pnl-legend'), html`<span><i style="background:${sk.main}"></i>累計利潤（含已鎖定）</span><span><i style="background:${sk.real}"></i>已實現淨利</span>`);
    setHTML($('#tp-legend'), html`<span><i style="background:${sk.real}"></i>WebSocket 訊息／秒</span><span><i style="background:${sk.main}"></i>策略評估／秒</span>`);
    renderSkinCards();
    drawCharts();
    try { localStorage.setItem('polyarb_skin', chosen); } catch (_) {}
    if (persistServer) {
      post('/api/control', { action: 'set_skin', skin: chosen }).catch(() => {});
    }
  }

  try {
    S.autoDaylight = localStorage.getItem('polyarb_auto_day') === '1';
    const ad = $('#auto-daylight');
    if (ad) ad.checked = S.autoDaylight;
    applySkin(localStorage.getItem('polyarb_skin') || 'spatial', false);
  } catch (_) {
    applySkin('spatial', false);
  }

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
    ['metar', 'METAR 氣象源'], ['maker', '零費掛單對沖'], ['cpu', 'CPU'], ['rss', '記憶體'],
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

  function renderMiniSpark(id, values, color) {
    const el = $(id);
    if (!el) return;
    const clean = (values || []).filter(finite).slice(-32);
    if (clean.length < 2) { el.innerHTML = ''; return; }
    let lo = Math.min(...clean), hi = Math.max(...clean);
    if (hi - lo < 1e-6) { lo -= 0.5; hi += 0.5; }
    const pts = clean.map((v, i) => [(i / (clean.length - 1)) * 86 + 1, 20 - ((v - lo) / (hi - lo)) * 18]);
    el.innerHTML = `<path d="${monotonePath(pts)}" fill="none" stroke="${color}" stroke-width="1.8" stroke-linecap="round"/>`;
  }

  function drawCharts() {
    if (chartsQueued) return;
    chartsQueued = true;
    requestAnimationFrame(() => {
      chartsQueued = false;
      const s = S.series;
      if (!s || !s.t) return;
      const sk = SKINS[S.skin] || SKINS.spatial;
      const total = s.t.map((_, i) => (finite(s.pnl[i]) ? s.pnl[i] + (finite(s.locked[i]) ? s.locked[i] : 0) : null));
      const markers = (S.execs || [])
        .filter(x => x.sent && finite(x.ts))
        .slice(-24)
        .map(x => ({ ts: x.ts, color: x.complete ? sk.main : '#ffc857' }));

      const validTotal = total.filter(finite);
      if (validTotal.length >= 2) {
        const hi = Math.max(...validTotal), lo = Math.min(...validTotal);
        setText($('#pnl-hi-lo'), `區間高 ${usd(hi, true)} · 低 ${usd(lo, true)}`);
      }

      pnlChart.range = S.range;
      pnlChart.set({
        t: s.t,
        bars: s.opps || null,
        barColor: sk.real,
        markers,
        series: [
          { label: '累計利潤', color: sk.main, values: total, area: true, glow: true, width: 2.5 },
          { label: '已實現淨利', color: sk.real, values: s.pnl, dash: '4 4', width: 1.7 },
        ],
      });
      renderMiniSpark('#spark-realized', s.pnl, sk.real);
      renderMiniSpark('#spark-today', s.today, sk.main);
      renderMiniSpark('#spark-locked', s.locked, '#ffc857');

      tpChart.range = 3 * 3600;
      tpChart.set({
        t: s.t,
        series: [
          { label: 'WS 訊息／秒', color: sk.real, values: s.ws, area: true },
          { label: '評估／秒', color: sk.main, values: s.evals },
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
    const topPill = $('#top-pnl-pill');
    if (topPill) setText(topPill, usd(total, true));
    tween($('#pnl-realized'), k.pnl_total, v => usd(v, true));
    tween($('#pnl-today'), k.pnl_today, v => usd(v, true));
    tween($('#pnl-locked'), k.locked_profit, v => usd(v, true));
    tween($('#pnl-roi'), k.bankroll ? total / k.bankroll : null, v => pct(v, 2));
    tween($('#exec-count'), k.executions, num, 600);
    $('#pnl-realized').className = k.pnl_total < -0.005 ? 'neg' : '';
    $('#pnl-today').className = k.pnl_today < -0.005 ? 'neg' : '';
    setText($('#pnl-hint'), (t.mode === 'live' ? '實盤' : '紙盤模擬') + '・已實現 + 已鎖定（結算後入賬）');
    document.title = `${usd(total, true)} · POLYARB ${t.mode === 'live' ? '實盤' : '紙盤'}`;

    // Capital Allocation Segmented Bar (Concept 4 & Concept 2 pool breakdown)
    const avail = Math.max(0, k.available || 0);
    const resv = Math.max(0, k.reserved_usd || 0);
    const hold = Math.max(0, k.hold_exposure || 0);
    const sumCap = Math.max(1e-6, avail + resv + hold);
    const sk = SKINS[S.skin] || SKINS.spatial;
    setHTML($('#cap-bar'), html`
      <i style="width:${((avail / sumCap) * 100).toFixed(1)}%;background:${sk.main}"></i>
      <i style="width:${((resv / sumCap) * 100).toFixed(1)}%;background:${sk.real}"></i>
      <i style="width:${((hold / sumCap) * 100).toFixed(1)}%;background:#ffc857"></i>`);
    setHTML($('#cap-legend'), html`
      <span><em style="background:${sk.main}"></em>立即可用 <b>${usd(avail)}</b></span>
      <span><em style="background:${sk.real}"></em>池化掛單預留 <b>${usd(resv)}</b></span>
      <span><em style="background:#ffc857"></em>持倉鎖定 <b>${usd(hold)}</b></span>`);
  }

  function renderMakerMatrix(t) {
    const box = $('#maker-matrix');
    if (!box) return;
    const mk = (t.engine && t.engine.maker) || {};
    const quotes = mk.quotes || [];
    const maxSlots = Math.max(6, quotes.length);
    const sig = JSON.stringify([mk.enabled, mk.open_quotes, mk.reserved_usd, mk.rebates_est_usd, quotes]);
    if (S.sigs.makerMatrix === sig) return;
    S.sigs.makerMatrix = sig;

    const pill = $('#maker-pill');
    if (pill) {
      setText(pill, mk.enabled ? `${quotes.length} 掛單中 · 預留 ${usd(mk.reserved_usd || 0)}` : '已關閉');
      pill.className = 'pill ' + (quotes.length ? 'ok' : '');
    }
    const cancelBtn = $('#btn-cancel-quotes');
    if (cancelBtn) cancelBtn.disabled = !quotes.length;

    const slots = [];
    for (let i = 0; i < Math.min(maxSlots, 6); i++) {
      const q = quotes[i];
      const tag = `ARB-0${i + 1}`;
      if (q) {
        const ttlPct = Math.max(6, Math.min(100, 100 - ((q.age_s || 0) / 300) * 100));
        slots.push(html`<div class="m-slot active">
          <div class="m-slot-top"><span class="m-slot-id">${tag}</span>${chip(q.strategy)}</div>
          <div class="m-slot-title" title="${q.title}">${q.title}</div>
          <div class="m-slot-quote" title="${q.lead}">BUY ${q.lead} @ ${Number(q.price).toFixed(3)} × ${q.shares}</div>
          <div class="m-slot-foot">
            <span>預期 <b>+${usd(q.profit)}</b> (+${q.edge_pct}%)</span>
            <span>${Math.round(q.age_s || 0)}s</span>
          </div>
          <div class="m-ttl-bar"><i style="width:${ttlPct.toFixed(0)}%"></i></div>
        </div>`);
      } else {
        slots.push(html`<div class="m-slot idle">
          <span class="m-slot-id">${tag} · STANDBY</span>
          <small class="muted">${mk.enabled ? '毫秒級掃描最優掛單價差中…' : '零費掛單已關閉'}</small>
        </div>`);
      }
    }
    setHTML(box, slots);
  }

  function renderOracles(t) {
    const box = $('#oracle-cards');
    if (!box) return;
    const mt = (t.engine && t.engine.metar) || {};
    const bn = (t.engine && t.engine.binance) || {};
    const rt = (t.engine && t.engine.rtds) || {};
    const sig = JSON.stringify([mt, bn, rt]);
    if (S.sigs.oracles === sig) return;
    S.sigs.oracles = sig;

    const samples = mt.sample || [];
    if (globe) {
      globe.hotIcaos = new Set(samples.map(s => s.icao));
    }
    const latest = bn.latest || {};
    const coinEntries = Object.entries(latest).slice(0, 6);
    const clTwap = Object.entries(rt.twap || {}).slice(0, 4);

    setHTML($('#oracle-pill'), html`${num(mt.stations || 0)} 機場 · 幣安 · Chainlink 5m/15m`);
    setHTML(box, html`
      <div class="ora-box">
        <div class="ora-head">🌤️ NOAA AWC METAR 航空氣象源 <span>${num(mt.polls || 0)} 輪 · ${num(mt.days_tracked || 0)} 站日</span></div>
        ${samples.length
          ? html`<div class="ora-grid">${samples.slice(0, 6).map(s => html`<div><b>${s.icao}</b> 高 ${s.hi_c}°C / 低 ${s.lo_c}°C${s.fin ? ' ✓' : ''}</div>`)}</div>`
          : html`<div class="muted" style="font-size:11.5px">正在同步全球 48 個國際機場例行 METAR 實測報文…</div>`}
      </div>
      <div class="ora-box">
        <div class="ora-head">⚡ Chainlink 60s-TWAP (5m/15m) ＆ 幣安 1h 收盤源 <span>RTDS ${num(rt.messages || 0)} 訊 · K線 ${num(bn.hourly_closes || 0)} 根</span></div>
        ${clTwap.length
          ? html`<div class="coin-tags">${clTwap.map(([sym, px]) => html`<span class="coin-tag">${sym}/USD <b>$${Number(px).toLocaleString('en-US', { maximumFractionDigits: px >= 100 ? 2 : 4 })}</b></span>`)}</div>`
          : coinEntries.length
            ? html`<div class="coin-tags">${coinEntries.map(([sym, px]) => html`<span class="coin-tag">${sym.replace('USDT', '')} <b>$${Number(px).toLocaleString('en-US', { maximumFractionDigits: px >= 100 ? 1 : 4 })}</b></span>`)}</div>`
            : html`<div class="muted" style="font-size:11.5px">實時串流 Chainlink 60s TWAP 與幣安已收盤 1h K 線…</div>`}
      </div>
    `);
  }

  function renderKpis(t) {
    const k = t.kpi, e = t.engine, r = t.risk;
    tween(kv('available'), k.available, v => usd(v));
    setText(ks('available'), finite(k.wallet_balance) ? `鏈上 ${usd(k.wallet_balance)}・預留 ${usd(k.reserved_usd || 0)}` : `本金 ${usd(k.bankroll)}`);
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
    const mt = e.metar || {}, mk = e.maker || {}, lc = e.lifecycle || {};
    setText(ev('metar'), mt.stations ? `${num(mt.stations)} 機場站` : '關閉');
    setText(es_('metar'), mt.polls ? `已追蹤 ${num(mt.days_tracked)} 日・輪詢 ${num(mt.polls)}` : '待啟動');
    setText(ev('maker'), mk.enabled ? `${num(mk.open_quotes || 0)} 掛單中` : '關閉');
    setText(es_('maker'), `成交 ${num(mk.filled || 0)}・閃電平倉 ${num(lc.flips_completed || 0)}・贖回 ${num(lc.redemptions || 0)}`);
    setText(ev('cpu'), finite(e.cpu_pct) ? `${e.cpu_pct.toFixed(1)}%` : '—');
    setText(es_('cpu'), finite(e.eval_rate) ? `評估 ${nf0.format(e.eval_rate)}/秒` : '佔單一核心');
    setText(ev('rss'), finite(e.rss_mb) ? `${nf0.format(e.rss_mb)} MB` : '—');
    const tg = e.tg;
    setText(es_('rss'), tg && tg.running ? `TG @${tg.username || 'bot'} 連線中・指令 ${num(tg.handled || 0)}` : `連線 ${num(e.clients)}・執行中 ${num(e.inflight)}`);
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
      <td class="num" title="${h.resolves_at || ''}">${h.exit_price ? `掛賣 @${h.exit_price.toFixed(3)}` : until(h.resolves_at)}</td></tr>`));
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
    const mbtn = $('#btn-switch-mode');
    if (mbtn) {
      setText(mbtn, t.mode === 'live' ? '切換回紙盤 PAPER（免重啟）' : '熱切換至實盤 LIVE（免重啟）');
      mbtn.className = 'btn small ' + (t.mode === 'live' ? 'warn' : 'danger');
    }
  }

  function renderSettings() {
    const st = S.settings;
    const tabsEl = $('#settings-tabs'), gridEl = $('#settings-grid');
    if (!st || !st.categories || !tabsEl || !gridEl) return;
    const cats = st.categories;
    if (!S.setCat || !cats.some(c => c.id === S.setCat)) S.setCat = cats[0].id;
    const sig = JSON.stringify([S.setCat, st]);
    if (S.sigs.settings === sig) return;
    S.sigs.settings = sig;

    setHTML(tabsEl, cats.map(c => {
      const ov = (c.items || []).filter(i => i.overridden).length;
      const shortTitle = c.title.replace(/^[^\s]+\s*/, '');
      return html`<button type="button" data-sc="${c.id}" class="${c.id === S.setCat ? 'on' : ''}">${shortTitle}${ov ? ` (${ov})` : ''}</button>`;
    }));

    const cur = cats.find(c => c.id === S.setCat) || cats[0];
    setHTML(gridEl, (cur.items || []).map(it => {
      const displayVal = it.unit === 'pct' ? +(it.value * 100).toFixed(4) : it.value;
      const controls = it.kind === 'bool'
        ? html`<button type="button" class="${it.value ? 'on' : ''}" data-skey="${it.key}" data-sval="${it.value ? '0' : '1'}" data-skind="bool">${it.value ? '🟢 已開啟（點擊關閉）' : '⚪ 已關閉（點擊開啟）'}</button>`
        : html`${(it.presets || []).map(p => {
            const pDisp = it.unit === 'pct' ? +(p * 100).toFixed(3) + '%' : it.unit === 'usd' ? '$' + p : p;
            const active = Math.abs(Number(it.value) - Number(p)) < 1e-9;
            return html`<button type="button" class="${active ? 'on' : ''}" data-skey="${it.key}" data-sval="${p}" data-skind="${it.kind}">${pDisp}</button>`;
          })}<input type="number" step="any" value="${displayVal}" data-sinput="${it.key}" data-sunit="${it.unit}" data-skind="${it.kind}"><button type="button" data-ssave="${it.key}">套用</button>`;
      return html`<div class="set-item${it.overridden ? ' ov' : ''}">
        <div class="set-top"><b>${it.label}${it.overridden ? ' ✏️' : ''}</b><span class="val">${it.formatted}</span></div>
        <div class="set-desc">${it.desc}</div>
        <div class="set-ctrl">${controls}</div>
      </div>`;
    }));
    const resetBtn = $('#btn-reset-settings');
    if (resetBtn) {
      resetBtn.disabled = !st.overrides_count;
      setText(resetBtn, st.overrides_count ? `還原全部自訂設定（${st.overrides_count} 項）` : '目前使用預設設定');
    }
  }

  // ------------------------------------------------------------------ event handlers

  function onState(s) {
    S.meta = s.meta || {};
    S.series = s.series || null;
    S.ranking = s.ranking || null;
    S.settings = s.settings || S.settings || null;
    if (S.settings && S.settings.preferred_skin && SKINS[S.settings.preferred_skin] && S.settings.preferred_skin !== S.chosenSkin) {
      applySkin(S.settings.preferred_skin, false);
    }
    if (S.meta.version) setText($('#ver-pill'), 'v' + S.meta.version);
    S.opps = s.opportunities || [];
    S.execs = s.executions || [];
    renderFeed($('#opp-feed'), S.opps, oppItem, EMPTY_OPPS);
    renderFeed($('#exec-feed'), S.execs, execItem, EMPTY_EXECS);
    appendLogs(s.logs || [], true);
    onTick(s);
    renderSettings();
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
    renderMakerMatrix(t);
    renderOracles(t);
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
    on('flip_closed', d => toast(`閃電止盈成交：${d.title}・${usd(d.pnl, true)}`, 'good'));
    on('redeemed', d => toast(`已自動鏈上贖回：${d.title}・收回 ${usd(d.proceeds)}`, 'good'));
    on('hold_reconciled', d => toast(`已同步外部平倉持倉：${d.title}`));
    on('setting', d => {
      if (d.label) toast(`設定已同步（${d.source === 'telegram' ? 'Telegram' : '網頁'}）：${d.label} = ${d.formatted}`, 'good');
    });
    on('mode_switch', d => toast(`模式已熱切換至：${d.mode === 'live' ? '實盤 LIVE' : '紙盤 PAPER'}`, 'good'));
    on('skin', d => {
      if (d && d.skin && SKINS[d.skin]) {
        applySkin(d.skin, false);
        if (d.source === 'telegram') toast(`外觀已由 Telegram 同步為：${d.label || d.skin}`, 'good');
      }
    });
    on('early_harvest', d => toast(`中間帶雙重派彩收割：${d.title}・淨利 ${usd(d.pnl, true)}`, 'good'));
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

  // 5-Skin Studio Drawer + Keyboard 1..5 + Auto-Daylight + Mobile Dock
  const skinDrawer = $('#skin-drawer');
  if ($('#btn-skin') && skinDrawer) {
    $('#btn-skin').addEventListener('click', () => {
      renderSkinCards();
      skinDrawer.classList.toggle('hidden');
    });
    $('#skin-close').addEventListener('click', () => skinDrawer.classList.add('hidden'));
    skinDrawer.addEventListener('click', e => {
      if (e.target === skinDrawer) skinDrawer.classList.add('hidden');
      const card = e.target.closest('[data-pick-skin]');
      if (card) {
        const picked = card.dataset.pickSkin;
        applySkin(picked, true);
        toast(`已套用主題：${SKINS[picked].name}`, 'good');
      }
    });
  }
  const autoDayEl = $('#auto-daylight');
  if (autoDayEl) {
    autoDayEl.addEventListener('change', () => {
      S.autoDaylight = autoDayEl.checked;
      try { localStorage.setItem('polyarb_auto_day', S.autoDaylight ? '1' : '0'); } catch (_) {}
      applySkin(S.chosenSkin, false);
      toast(S.autoDaylight ? '已開啟日間自動極晝模式' : '已關閉日間自動極晝模式');
    });
  }
  document.addEventListener('keydown', e => {
    if (e.target && ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return;
    if (!$('#modal').classList.contains('hidden')) return;
    if (e.key >= '1' && e.key <= '5' && !e.metaKey && !e.ctrlKey && !e.altKey) {
      const id = SKIN_ORDER[Number(e.key) - 1];
      if (id) {
        applySkin(id, true);
        toast(`快捷鍵 [${e.key}] 已切換：${SKINS[id].name}`, 'good');
      }
    } else if (e.key === 'Escape' && skinDrawer && !skinDrawer.classList.contains('hidden')) {
      skinDrawer.classList.add('hidden');
    }
  });

  const mobileDock = $('#mobile-dock');
  if (mobileDock) {
    mobileDock.addEventListener('click', e => {
      const b = e.target.closest('button[data-mv]');
      if (!b) return;
      const mv = b.dataset.mv;
      S.mobileView = mv;
      document.documentElement.setAttribute('data-view', mv);
      $$('#mobile-dock button').forEach(x => x.classList.toggle('on', x === b));
      window.scrollTo({ top: 0, behavior: REDUCED ? 'auto' : 'smooth' });
      setTimeout(() => { drawCharts(); radar.resize(); if (globe) globe.resize(); }, 40);
    });
  }

  if ($('#btn-cancel-quotes')) {
    $('#btn-cancel-quotes').addEventListener('click', async () => {
      try {
        const res = await control('cancel_quotes');
        toast(`已撤銷 ${res.canceled || 0} 張零費掛單`, 'good');
      } catch (err) { toast('撤單失敗：' + err.message, 'bad'); }
    });
  }
  if ($('#btn-sync-holds')) {
    $('#btn-sync-holds').addEventListener('click', async () => {
      try {
        await control('trigger_lifecycle');
        toast('已完成鏈上贖回、對帳與中間帶收割檢查', 'good');
      } catch (err) { toast('檢查失敗：' + err.message, 'bad'); }
    });
  }

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

  $('#settings-tabs').addEventListener('click', e => {
    const b = e.target.closest('button[data-sc]');
    if (!b) return;
    S.setCat = b.dataset.sc;
    renderSettings();
  });

  $('#settings-grid').addEventListener('click', async e => {
    const presetBtn = e.target.closest('button[data-skey]');
    if (presetBtn) {
      const key = presetBtn.dataset.skey;
      const kind = presetBtn.dataset.skind;
      const raw = presetBtn.dataset.sval;
      const value = kind === 'bool' ? raw === '1' : kind === 'int' ? parseInt(raw, 10) : parseFloat(raw);
      try {
        const res = await post('/api/settings', { key, value });
        S.settings = res.settings;
        renderSettings();
        toast(`已更新：${res.label} = ${res.formatted}`, 'good');
      } catch (err) { toast('設定失敗：' + err.message, 'bad'); }
      return;
    }
    const saveBtn = e.target.closest('button[data-ssave]');
    if (saveBtn) {
      const key = saveBtn.dataset.ssave;
      const inp = $(`#settings-grid input[data-sinput="${key}"]`);
      if (!inp) return;
      let numVal = parseFloat(inp.value);
      if (!Number.isFinite(numVal)) { toast('請輸入有效數字', 'bad'); return; }
      if (inp.dataset.sunit === 'pct') numVal /= 100.0;
      if (inp.dataset.skind === 'int') numVal = Math.round(numVal);
      try {
        const res = await post('/api/settings', { key, value: numVal });
        S.settings = res.settings;
        renderSettings();
        toast(`已更新：${res.label} = ${res.formatted}`, 'good');
      } catch (err) { toast('設定失敗：' + err.message, 'bad'); }
    }
  });

  $('#btn-reset-settings').addEventListener('click', async () => {
    const ok = await modal({ title: '還原全部自訂設定？', text: '所有在網頁或 Telegram 修改過的參數將恢復為系統預設值。', okText: '確認還原' });
    if (ok === null) return;
    try {
      const res = await post('/api/settings', { action: 'reset' });
      S.settings = res.settings;
      renderSettings();
      toast(`已還原 ${res.removed} 項設定`, 'good');
    } catch (err) { toast('還原失敗：' + err.message, 'bad'); }
  });

  $('#btn-switch-mode').addEventListener('click', async () => {
    const isLive = S.tick && S.tick.mode === 'live';
    if (isLive) {
      const ok = await modal({ title: '切換回紙盤模式？', text: '將立即停止實盤落單並切換至紙盤模擬帳本，無需重啟容器。', okText: '切換至紙盤' });
      if (ok === null) return;
      try {
        const res = await control('switch_mode', { mode: 'paper' });
        toast(res.message || '已切換至紙盤', 'good');
      } catch (err) { toast('切換失敗：' + err.message, 'bad'); }
    } else {
      const v = await modal({ title: '熱切換至實盤 LIVE', text: '請確認實盤安全條件已全部通過。切換後會先保持「暫停」讓你核對餘額，輸入 LIVE 確認：', expect: 'LIVE', okText: '切換至實盤' });
      if (v === null) return;
      try {
        const res = await control('switch_mode', { mode: 'live', confirm: v, pause_first: true });
        toast(res.message || '已切換至實盤（保持暫停）', 'good');
      } catch (err) { toast('切換失敗：' + err.message, 'bad'); }
    }
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
