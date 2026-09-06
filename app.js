/* 跑得快 · 两人对战 - 前端逻辑 */
'use strict';

const $ = (id) => document.getElementById(id);
const SUITS = ['♠', '♥', '♣', '♦'];            // 红: s===1 || s===3
const RANK_NAME = { 3:'3',4:'4',5:'5',6:'6',7:'7',8:'8',9:'9',10:'10',11:'J',12:'Q',13:'K',14:'A',15:'2' };
const BASE = 1;          // 底分：1分/张
const BOMB_SCORE = 10;   // 每颗炸弹收取分数

/* ================= 状态 ================= */
const S = {
  screen: 'lobby',
  mode: 'ai',                                  // 'ai' | 'hotseat'
  opts: { sanzhang: false, nobomb: true, red10: false, four3: false },
  rounds: 10, roundNo: 0,
  total: [0, 0], history: [],
  names: ['我', '电脑'], avatars: ['🙂', '🤖'],
  hands: [[], []], turn: 0,
  last: null,                                  // {combo, cards, by}
  shown: [null, null],                         // 各家最近动作（出牌/不出），用于展示
  bombs: [], playsMade: [0, 0], red10Holder: null,
  lastWinner: null, phase: 'idle',             // 'idle'|'playing'|'roundEnd'|'matchEnd'
  awaiting: null,                              // 热座：等待确认看牌的座位
  selected: new Set(), hints: [], hintIdx: -1,
  bridgeMode: false, roundLeader: 0,           // AI 机器人桥接状态 / 本局先手
  aiTimer: null,
};

/* ================= 基础工具 ================= */
function toast(msg, ms = 2200) {
  const t = $('toast');
  t.textContent = msg; t.classList.remove('hidden');
  clearTimeout(t._tm); t._tm = setTimeout(() => t.classList.add('hidden'), ms);
}
function randInt(n) {   // crypto 均匀随机 [0,n)
  const u = new Uint32Array(1), lim = Math.floor(0x100000000 / n) * n;
  let x; do { crypto.getRandomValues(u); x = u[0]; } while (x >= lim);
  return x % n;
}
function shuffle(arr) {  // Fisher-Yates
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) { const j = randInt(i + 1); [a[i], a[j]] = [a[j], a[i]]; }
  return a;
}
function buildDeck() {
  const d = []; let i = 0;
  for (let r = 3; r <= 13; r++) for (let s = 0; s < 4; s++) d.push({ i: i++, r, s }); // 3~K 各4张
  for (const s of [1, 2, 3]) d.push({ i: i++, r: 14, s });                            // A：黑桃A拿走，剩3张
  d.push({ i: i++, r: 15, s: 0 });                                                    // 黑桃2，全场最大单张
  return d;                                                                           // 共48张
}
function sortHand(h) { h.sort((a, b) => a.r - b.r || a.s - b.s); return h; }
function removeCards(hand, cards) { const ids = new Set(cards.map(c => c.i)); return hand.filter(c => !ids.has(c.i)); }
function cardText(c) { return SUITS[c.s] + RANK_NAME[c.r]; }
function isRed(c) { return c.s === 1 || c.s === 3; }
function cardHTML(c, mini) {
  return `<div class="card ${mini ? 'mini' : ''} ${isRed(c) ? 'red' : ''}" data-id="${c.i}">` +
    `<span class="cr">${RANK_NAME[c.r]}<span class="suit-sm">${SUITS[c.s]}</span></span>` +
    `<span class="cs">${SUITS[c.s]}</span></div>`;
}
function cardsHTML(cards, mini = true) { return cards.map(c => cardHTML(c, mini)).join(''); }

/* ================= 牌型分析 ================= */
function rankCounts(cards) {
  const m = new Map();
  for (const c of cards) { let a = m.get(c.r); if (!a) { a = []; m.set(c.r, a); } a.push(c); }
  return m;
}
function isConsecutiveRun(ranks) {
  for (let i = 1; i < ranks.length; i++) if (ranks[i] !== ranks[i - 1] + 1) return false;
  return true;
}
/* 纯形状分析：t: single/pair/triple/t1/t2/straight/pairseq/plane/planeBare/bomb/quad3 */
function analyzeShape(cards) {
  const n = cards.length; if (!n) return null;
  const m = rankCounts(cards);
  const ranks = [...m.keys()].sort((a, b) => a - b);
  const cnt = r => m.get(r).length;
  if (n === 1) return { t: 'single', key: ranks[0], len: 1 };
  if (n === 2) return ranks.length === 1 ? { t: 'pair', key: ranks[0], len: 2 } : null;
  if (n === 3) return ranks.length === 1 ? { t: 'triple', key: ranks[0], len: 3 } : null;
  if (n === 4) {
    if (ranks.length === 1) return { t: 'bomb', key: ranks[0], len: 4 };  // 4张同点=炸弹；2只1张、A只3张，炸弹只可能是3~K（KKKK最大）
    const ts = ranks.filter(r => cnt(r) === 3);
    if (ts.length === 1) {
      const t = ts[0], others = ranks.filter(r => r !== t);
      if (others.length === 1 && cnt(others[0]) === 1) return { t: 't1', key: t, len: 4 };
    }
    // 两连对（连对 2 对起）
    if (ranks.length === 2 && ranks.every(r => cnt(r) === 2) && isConsecutiveRun(ranks))
      return { t: 'pairseq', key: ranks[1], len: 4 };
    return null;
  }
  if (n === 5) {
    const ts = ranks.filter(r => cnt(r) === 3);
    if (ts.length === 1) {
      const t = ts[0], rest = ranks.filter(r => r !== t);
      if (rest.length === 1 && cnt(rest[0]) === 2) return { t: 't2', key: t, len: 5 };
      if (rest.length === 2 && cnt(rest[0]) === 1 && cnt(rest[1]) === 1) return { t: 't2', key: t, len: 5 };
    }
    if (ranks.length === 5 && ranks[4] <= 14 && isConsecutiveRun(ranks)) return { t: 'straight', key: ranks[4], len: 5 }; // 2不能进顺子
    return null;
  }
  // n >= 6
  if (ranks.length === n && n <= 12 && ranks[n - 1] <= 14 && isConsecutiveRun(ranks)) return { t: 'straight', key: ranks[n - 1], len: n }; // 2不能进顺子
  if (n % 2 === 0 && ranks.length === n / 2 && ranks.length >= 2 &&
      ranks.every(r => cnt(r) === 2) && isConsecutiveRun(ranks)) return { t: 'pairseq', key: ranks[ranks.length - 1], len: n };
  // 飞机带翅膀：k 副连三张 + 2k 张带牌（5k 张）；取 key 最大的解释
  for (let k = Math.floor(n / 5); k >= 2; k--) {
    if (n !== 5 * k) continue;
    for (let i = ranks.length - k; i >= 0; i--) {
      const seg = ranks.slice(i, i + k);
      if (seg.every(r => cnt(r) >= 3) && isConsecutiveRun(seg)) return { t: 'plane', key: seg[k - 1], len: n, k };
    }
  }
  // 纯飞机（最后一手）：恰好 k 副连三张
  if (n % 3 === 0) {
    const k = n / 3;
    if (k >= 2 && ranks.length === k && ranks.every(r => cnt(r) === 3) && isConsecutiveRun(ranks))
      return { t: 'planeBare', key: ranks[k - 1], len: n, k };
  }
  // 四带三（选项）
  if (n === 7) {
    const qs = ranks.filter(r => cnt(r) === 4);
    if (qs.length === 1) return { t: 'quad3', key: qs[0], len: 7 };
  }
  return null;
}
/* 同牌型比较（含炸弹、三张家族交叉规则） */
function canBeat(c, last, opts) {
  if (!last) return true;
  if (c.t === 'bomb') return last.t !== 'bomb' || c.key > last.key;
  if (last.t === 'bomb') return false;
  const fam = t => t === 'triple' || t === 't1' || t === 't2';
  if (fam(c.t) && fam(last.t)) {
    if (c.t !== last.t && opts.sanzhang && last.t === 't2' && (c.t === 'triple' || c.t === 't1')) return false;
    return c.key > last.key;
  }
  if (c.t === last.t && c.len === last.len) return c.key > last.key;
  return false;
}
/* 上下文合法性：纯三张/三带一/纯飞机只能最后一手；四带三需选项 */
function comboContextOK(combo, handLen, opts) {
  if ((combo.t === 'triple' || combo.t === 't1' || combo.t === 'planeBare') && combo.len !== handLen) return false;
  if (combo.t === 'quad3' && !opts.four3) return false;
  return true;
}
/* 炸弹不可拆：手牌中某点数满4张（≤K）而打法只用了一部分 */
function breaksBomb(hand, cards, opts) {
  if (!opts.nobomb) return false;
  const hm = rankCounts(hand), cm = rankCounts(cards);
  for (const [r, a] of cm) {
    const total = hm.get(r) ? hm.get(r).length : 0;
    if (total === 4 && a.length < 4) return true;
  }
  return false;
}
/* 防放水：对方报单时，出单张必须出手中最大的单张 */
function violatesBaodan(hand, combo, oppCount) {
  if (oppCount !== 1 || combo.t !== 'single') return false;
  let mx = 0; for (const c of hand) if (c.r > mx) mx = c.r;
  return combo.key !== mx;
}

/* ================= 带牌挑选 ================= */
/* 从除 excludeRanks 外的牌里挑 count 张；prefer: 'pairs' 优先成对 / 'singles' 优先散单 */
function pickWings(m, excludeRanks, count, prefer) {
  const singles = [], pairs = [];
  for (const [r, a] of m) {
    if (excludeRanks.has(r)) continue;
    for (const c of a) singles.push(c);
    if (a.length >= 2) pairs.push({ r, cards: [a[0], a[1]], exact: a.length === 2 });
  }
  const take = [];
  if (prefer === 'pairs') {
    pairs.sort((x, y) => (x.exact === y.exact ? x.r - y.r : (x.exact ? -1 : 1)));
    for (const p of pairs) { if (take.length + 2 <= count) take.push(p.cards[0], p.cards[1]); if (take.length === count) break; }
  }
  if (take.length < count) {
    singles.sort((a, b) => a.r - b.r || a.s - b.s);
    const taken = new Set(take.map(c => c.i));
    for (const c of singles) {
      if (take.length >= count) break;
      if (taken.has(c.i)) continue;
      take.push(c); taken.add(c.i);
    }
  }
  return take.length === count ? take : null;
}

/* ================= 候选生成 ================= */
function genLeads(hand, opts) {
  const res = [], m = rankCounts(hand);
  const ranks = [...m.keys()].sort((a, b) => a - b);
  const cnt = r => (m.get(r) || []).length;
  const add = cards => { const c = analyzeShape(cards); if (c) res.push({ cards, combo: c }); };
  for (const r of ranks) {
    add([m.get(r)[0]]);
    if (cnt(r) >= 2) add(m.get(r).slice(0, 2));
  }
  // 顺子
  for (let len = 5; len <= 12; len++) for (let s = 3; s + len - 1 <= 14; s++) {
    let ok = true; const cs = [];
    for (let r = s; r <= s + len - 1; r++) { if (cnt(r) < 1) { ok = false; break; } cs.push(m.get(r)[0]); }
    if (ok) add(cs);
  }
  // 连对
  for (let p = 2; p <= 12; p++) for (let s = 3; s + p - 1 <= 14; s++) {
    let ok = true; const cs = [];
    for (let r = s; r <= s + p - 1; r++) { if (cnt(r) < 2) { ok = false; break; } cs.push(m.get(r)[0], m.get(r)[1]); }
    if (ok) add(cs);
  }
  for (const r of ranks) {
    if (cnt(r) < 3) continue;
    const tri = m.get(r).slice(0, 3);
    add(tri);                                   // 纯三张（仅剩3张时合法，contextOK 过滤）
    const w1 = pickWings(m, new Set([r]), 1, 'singles');
    if (w1) add(tri.concat(w1));                // 三带一（仅剩4张时合法）
    const wA = pickWings(m, new Set([r]), 2, 'pairs');
    if (wA) add(tri.concat(wA));
    const wB = pickWings(m, new Set([r]), 2, 'singles');
    if (wB) add(tri.concat(wB));
    if (cnt(r) === 4) add(m.get(r).slice());                     // 炸弹（2/A凑不满4张，实际为3~K，KKKK最大）
    if (cnt(r) === 4 && opts.four3) {
      const w3 = pickWings(m, new Set([r]), 3, 'singles');
      if (w3) add(m.get(r).slice().concat(w3));                  // 四带三
    }
  }
  // 飞机
  for (let k = 2; k <= 6; k++) for (let s = 3; s + k - 1 <= 14; s++) {
    let ok = true; const seg = [];
    for (let r = s; r <= s + k - 1; r++) { if (cnt(r) < 3) { ok = false; break; } seg.push(r); }
    if (!ok) continue;
    const segCards = []; for (const r of seg) segCards.push(...m.get(r).slice(0, 3));
    add(segCards);                              // 纯飞机（最后一手）
    const segSet = new Set(seg);
    const wA = pickWings(m, segSet, 2 * k, 'pairs');
    if (wA) add(segCards.concat(wA));
    const wB = pickWings(m, segSet, 2 * k, 'singles');
    if (wB) add(segCards.concat(wB));
  }
  return res;
}
function genBeats(last, hand, opts) {
  const res = [], m = rankCounts(hand);
  const ranks = [...m.keys()].sort((a, b) => a - b);
  const cnt = r => (m.get(r) || []).length;
  const add = cards => { const c = analyzeShape(cards); if (c) res.push({ cards, combo: c }); };
  const t = last.t, key = last.key;
  if (t === 'single') {
    for (const r of ranks) if (r > key) add([m.get(r)[0]]);
  } else if (t === 'pair') {
    for (const r of ranks) if (r > key && cnt(r) >= 2) add(m.get(r).slice(0, 2));
  } else if (t === 'straight') {
    for (let top = key + 1; top <= 14; top++) {
      const s = top - last.len + 1; if (s < 3) continue;
      let ok = true; const cs = [];
      for (let r = s; r <= top; r++) { if (cnt(r) < 1) { ok = false; break; } cs.push(m.get(r)[0]); }
      if (ok) add(cs);
    }
  } else if (t === 'pairseq') {
    const p = last.len / 2;
    for (let top = key + 1; top <= 14; top++) {
      const s = top - p + 1; if (s < 3) continue;
      let ok = true; const cs = [];
      for (let r = s; r <= top; r++) { if (cnt(r) < 2) { ok = false; break; } cs.push(m.get(r)[0], m.get(r)[1]); }
      if (ok) add(cs);
    }
  } else if (t === 't2') {
    for (const r of ranks) if (r > key && cnt(r) >= 3) {
      const tri = m.get(r).slice(0, 3);
      const wA = pickWings(m, new Set([r]), 2, 'pairs'); if (wA) add(tri.concat(wA));
      const wB = pickWings(m, new Set([r]), 2, 'singles'); if (wB) add(tri.concat(wB));
      // 「三张不可接」关闭时：更大的裸三张/三带一也可管三带二（仅最后一手，contextOK 过滤）
      add(tri);
      const w1 = pickWings(m, new Set([r]), 1, 'singles'); if (w1) add(tri.concat(w1));
    }
  } else if (t === 't1' || t === 'triple') {
    for (const r of ranks) if (r > key && cnt(r) >= 3) {
      const tri = m.get(r).slice(0, 3);
      add(tri);                                                            // 更大的纯三张
      const w1 = pickWings(m, new Set([r]), 1, 'singles'); if (w1) add(tri.concat(w1));
      const wA = pickWings(m, new Set([r]), 2, 'pairs'); if (wA) add(tri.concat(wA));
      const wB = pickWings(m, new Set([r]), 2, 'singles'); if (wB) add(tri.concat(wB));
    }
  } else if (t === 'plane' || t === 'planeBare') {
    const k = last.k;
    for (let top = key + 1; top <= 14; top++) {          // 窗口 = [top-k+1, top]，top 最高到 A
      const s = top - k + 1; if (s < 3) continue;
      let ok = true; const seg = [];
      for (let r = s; r <= top; r++) { if (cnt(r) < 3) { ok = false; break; } seg.push(r); }
      if (!ok) continue;
      const segCards = []; for (const r of seg) segCards.push(...m.get(r).slice(0, 3));
      if (t === 'planeBare') { add(segCards); continue; }
      const segSet = new Set(seg);
      const wA = pickWings(m, segSet, 2 * k, 'pairs'); if (wA) add(segCards.concat(wA));
      const wB = pickWings(m, segSet, 2 * k, 'singles'); if (wB) add(segCards.concat(wB));
    }
  } else if (t === 'bomb') {
    for (const r of ranks) if (r > key && cnt(r) === 4) add(m.get(r).slice());
  } else if (t === 'quad3') {
    for (const r of ranks) if (r > key && cnt(r) === 4) {
      const w = pickWings(m, new Set([r]), 3, 'singles'); if (w) add(m.get(r).slice().concat(w));
    }
  }
  if (t !== 'bomb') {
    for (const r of ranks) if (cnt(r) === 4) add(m.get(r).slice());              // 炸弹压一切
  }
  return res.filter(x => canBeat(x.combo, last, opts));
}
function legalPlays(hand, ctx, opts) {
  const raw = ctx.last ? genBeats(ctx.last, hand, opts) : genLeads(hand, opts);
  const handLen = hand.length, out = [], seen = new Set();
  for (const cand of raw) {
    const { cards, combo } = cand;
    if (!comboContextOK(combo, handLen, opts)) continue;
    if (breaksBomb(hand, cards, opts)) continue;
    if (violatesBaodan(hand, combo, ctx.oppCount)) continue;
    const k = cards.map(c => c.i).sort((a, b) => a - b).join(',');
    if (seen.has(k)) continue; seen.add(k);
    out.push(cand);
  }
  return out;
}

/* ================= AI ================= */
/* 贪心估算剩余手牌还需要几手出完（越小越好） */
function estimatePlays(cards) {
  let cs = cards.slice(); if (!cs.length) return 0;
  let plays = 0, guard = 0;
  while (cs.length && guard++ < 60) {
    const m = rankCounts(cs);
    const ranks = [...m.keys()].sort((a, b) => a - b);
    const cnt = r => m.get(r).length;
    let found = null;
    for (const r of ranks) if (cnt(r) === 4) { found = m.get(r).slice(); break; }
    if (!found) {
      outer: for (let k = Math.floor(ranks.length / 2); k >= 2; k--) {
        for (let i = 0; i + k <= ranks.length; i++) {
          const seg = ranks.slice(i, i + k);
          if (seg.every(r => cnt(r) >= 3) && isConsecutiveRun(seg)) {
            found = []; for (const r of seg) found.push(...m.get(r).slice(0, 3));
            break outer;
          }
        }
      }
    }
    if (!found) {
      const r14 = ranks.filter(r => r <= 14);              // 2不能进顺子
      for (let len = Math.min(12, r14.length); len >= 5; len--) {
        for (let i = 0; i + len <= r14.length; i++) {
          const seg = r14.slice(i, i + len);
          if (isConsecutiveRun(seg)) { found = seg.map(r => m.get(r)[0]); break; }
        }
        if (found) break;
      }
    }
    if (!found) {
      for (let p = Math.min(11, ranks.length); p >= 2; p--) {
        for (let i = 0; i + p <= ranks.length; i++) {
          const seg = ranks.slice(i, i + p);
          if (seg.every(r => cnt(r) >= 2) && isConsecutiveRun(seg)) {
            found = []; for (const r of seg) found.push(m.get(r)[0], m.get(r)[1]);
            break;
          }
        }
        if (found) break;
      }
    }
    if (!found) {
      const tr = ranks.find(r => cnt(r) >= 3);
      if (tr != null) {
        found = m.get(tr).slice(0, 3);
        const rest = cs.filter(c => c.r !== tr).sort((a, b) => a.r - b.r);
        if (cs.length === 4 && rest.length === 1) found.push(rest[0]);
        else if (rest.length >= 2) {
          const singles = rest.filter(c => cnt(c.r) === 1);
          if (singles.length >= 2) found.push(singles[0], singles[1]);
          else found.push(rest[0], rest[1]);
        }
      }
    }
    if (!found) { const pr = ranks.find(r => cnt(r) >= 2); if (pr != null) found = m.get(pr).slice(0, 2); }
    if (!found) found = [cs[0]];
    const ids = new Set(found.map(c => c.i));
    cs = cs.filter(c => !ids.has(c.i));
    plays++;
  }
  return plays;
}
function scoreCandidate(cand, hand, ctx, opts) {
  const { cards, combo: c } = cand;
  const rest = removeCards(hand, cards);
  if (!rest.length) return 1e9;
  let s = 0;
  s -= estimatePlays(rest) * 12;
  s += cards.length * 2.2;
  s -= c.key * 1.1;
  const hm = rankCounts(hand), cm = rankCounts(cards);
  for (const [r, a] of cm) {
    const total = hm.get(r).length;
    if (total === 4 && a.length < 4) s -= c.t === 'bomb' ? 0 : 30;               // 拆炸弹
    else if (a.length === 1 && c.t === 'single') s -= total === 4 ? 30 : (total === 3 ? 9 : 5);
    else if (a.length === 2 && total === 3) s -= 8;                              // 拆三张为对
  }
  if (c.t === 'bomb') s -= 26 - (ctx.oppCount <= 2 ? 40 : 0);
  if (ctx.oppCount === 1 && c.t === 'single') s -= 26;                           // 对方报单少送单
  if (!ctx.last && cards.length >= 5) s += 3;
  s += Math.random() * 1.6;
  return s;
}
function aiCandidates(seat) {
  const hand = S.hands[seat];
  const ctx = { last: S.last ? S.last.combo : null, oppCount: S.hands[1 - seat].length };
  return legalPlays(hand, ctx, S.opts)
    .map(p => ({ p, score: scoreCandidate(p, hand, ctx, S.opts) }))
    .sort((a, b) => b.score - a.score);
}

/* ================= AI 机器人桥接（pdk_ai 深度模型, ai_bridge.py :8766） ================= */
const AI_BRIDGE = 'http://127.0.0.1:8766';
const bridge = { sid: null, ready: false, mode: '', actions: [], actPending: false, initHands: null, syncing: false };

function toBotCard(c) { return (c.r === 14 && c.s === 3) ? 44 : ((c.r - 3) << 2) | c.s; }
function botRankToMy(idx) { return idx === 12 ? 15 : idx + 3; }   // bot点数0=3..11=A,12=2
function rankSig(cards) { return cards.map(c => c.r).sort((a, b) => a - b).join(','); }
function botRankSig(ids) { return ids.map(id => botRankToMy(id >> 2)).sort((a, b) => a - b).join(','); }
function matchLegalByRank(botIds, legal) {
  const sig = botRankSig(botIds);
  return legal.find(p => rankSig(p.cards) === sig) || null;   // 花色不影响规则，按点数匹配
}
function fromBotIds(ids, pool) {
  const map = new Map(pool.map(c => [toBotCard(c), c]));
  return ids.map(id => map.get(id)).filter(Boolean);
}
function legalKey(cs) { return cs.map(c => c.i).sort((a, b) => a - b).join(','); }

async function bridgeApi(path, body, timeoutMs = 15000) {
  const ctl = new AbortController();
  const tm = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(AI_BRIDGE + path, {
      method: body !== undefined ? 'POST' : 'GET',
      headers: { 'Content-Type': 'application/json' },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: ctl.signal,
    });
    return await r.json();
  } finally { clearTimeout(tm); }
}
async function bridgeHealth() {
  try { const r = await bridgeApi('/health', undefined, 3000); return !!r.ok; } catch { return false; }
}
function bridgeOpts() {
  return { sanzhang: S.opts.sanzhang, nobomb: S.opts.nobomb, red10: S.opts.red10, four3: S.opts.four3 };
}
async function bridgeNewRound() {
  bridge.sid = null; bridge.ready = false; bridge.actions = []; bridge.actPending = false;
  if (S.mode !== 'ai' || !(await bridgeHealth())) { setBridgeMode(false); return; }
  const hands0 = S.hands[0].map(toBotCard), hands1 = S.hands[1].map(toBotCard);
  try {
    const r = await bridgeApi('/init', {
      hands: [hands0, hands1],
      leader: S.roundLeader, opts: bridgeOpts(),
    }, 8000);
    if (r.sid) {
      bridge.sid = r.sid; bridge.mode = r.mode || 'hybrid'; bridge.ready = true;
      bridge.initHands = [hands0, hands1];               // 重连重放用初始发牌
      setBridgeMode(true);
      // init 期间可能已有落子（AI先手竞态）：带着完整历史重放一次
      if (bridge.actions.length) await bridgeResync();
    }
    else setBridgeMode(false);
  } catch { setBridgeMode(false); }
  if (S.screen === 'game') render();
}
async function bridgeResync() {
  // 影子失同步：用本局初始发牌 + 完整动作历史重建会话（并发保护）
  if (bridge.syncing || !bridge.initHands) return;
  bridge.syncing = true;
  bridge.ready = false;
  try {
    const r = await bridgeApi('/init', {
      hands: bridge.initHands, leader: S.roundLeader, opts: bridgeOpts(),
      actions: bridge.actions,
    }, 12000);
    bridge.sid = r.sid || null; bridge.ready = !!r.sid;
  } catch { bridge.ready = false; }
  finally { bridge.syncing = false; }
}
async function bridgeMirror(seat, myCards) {
  // 历史无条件记录（重放数据源）；/act 已在桥内落子的只记录不重发
  const wasActApplied = bridge.actPending;
  bridge.actPending = false;
  bridge.actions.push({ seat, cards: myCards.map(toBotCard) });
  if (wasActApplied) return;
  if (!bridge.sid || !bridge.ready) {
    if (S.mode === 'ai' && bridge.initHands && !bridge.syncing) bridgeResync();  // 无会话时尝试重建
    return;
  }
  try {
    const r = await bridgeApi('/action', { sid: bridge.sid, seat, cards: bridge.actions.at(-1).cards });
    if (!r.ok) throw new Error(r.error || 'mirror failed');
  } catch { if (!bridge.syncing) bridgeResync(); }
}
async function bridgeSuggestForTurn() {
  if (!bridge.ready) return null;
  try {
    const r = await bridgeApi('/suggest', { sid: bridge.sid }, 20000);
    if (!r.cards) return null;
    const legal = legalPlays(S.hands[S.turn], { last: S.last ? S.last.combo : null, oppCount: S.hands[1 - S.turn].length }, S.opts);
    return matchLegalByRank(r.cards, legal) || null;     // 内核建议未命中我方合法集 -> 用内置
  } catch { return null; }
}
function setBridgeMode(on) {
  if (S.bridgeMode !== on) { S.bridgeMode = on; if (S.screen === 'game') render(); }
}
/* 注册回调放在 init() 中（externalAI 以 let 声明在本段之后，加载期赋值会触发 TDZ） */

/* ================= 可插拔 AI 模型接口 =================
 * 预留的机器人模型接入点（后续接外部模型/接口时使用）：
 *
 *   pdkRegisterAI(async (ctx) => cardsArrayOrNull)
 *
 * ctx = {
 *   seat: 1,                    // AI 固定坐 1 号位
 *   hand: [...],                // AI 当前手牌（副本）
 *   oppCount: n,                // 对手剩余张数
 *   last: combo|null,           // 桌面上一手牌型（null = 自由出牌）
 *   legal: [{cards, combo}],    // 全部合法候选（legalPlays 的结果）
 *   opts: {...},                // 本局规则开关
 * }
 * 返回值：
 *   - 牌数组：必须与 legal 中某个候选完全一致（按牌 id 集合匹配），否则回退内置 AI；
 *   - null / []：表示「不出」（自由出牌时无效，会回退内置 AI）；
 *   - 同步或 Promise 均可；抛异常也会回退内置贪心 AI，保证牌局不中断。
 */
let externalAI = null;                                   // (ctx) => cards|null，由 pdkRegisterAI 注册
function pdkRegisterAI(fn) { externalAI = fn; }

async function aiMove() {
  if (S.phase !== 'playing' || S.turn !== 1 || S.mode !== 'ai') return;
  const seat = 1;
  const hand = S.hands[seat];
  const ctx = {
    seat,
    hand: hand.slice(),
    oppCount: S.hands[1 - seat].length,
    last: S.last ? S.last.combo : null,
    legal: legalPlays(hand, { last: S.last ? S.last.combo : null, oppCount: S.hands[1 - seat].length }, S.opts),
    opts: S.opts,
  };
  let cards = null;
  if (typeof externalAI === 'function') {
    try {
      const ret = await externalAI(ctx);
      if (ret == null || (Array.isArray(ret) && ret.length === 0)) {
        if (ctx.last) { applyPass(seat); return; }       // 模型选择不出（跟牌时合法）
      } else if (Array.isArray(ret)) {
        const key = ids => ids.map(c => c.i).sort((a, b) => a - b).join(',');
        const want = key(ret);
        const hit = ctx.legal.find(p => key(p.cards) === want);
        if (hit) cards = hit.cards;                      // 只接受合法候选，防止模型作弊/越权出牌
      }
    } catch (e) {
      console.error('[PdkAI] 模型调用失败，回退内置 AI：', e);
    }
  }
  if (!cards) {                                          // 内置贪心 AI 兜底
    const cands = aiCandidates(seat);
    if (!cands.length) { applyPass(seat); return; }
    cards = cands[0].p.cards;
  }
  applyPlay(seat, cards);                                // 镜像统一在 applyPlay/applyPass 里做
}
/* ================= 流程 ================= */
function startMatch() {
  S.mode = chosenMode;
  S.opts = {
    sanzhang: $('opt-sanzhang').checked,
    nobomb: $('opt-nobomb').checked,
    red10: $('opt-red10').checked,
    four3: $('opt-four3').checked,
  };
  S.rounds = +$('sel-rounds').value;
  S.total = [0, 0]; S.history = []; S.roundNo = 0; S.lastWinner = null;
  if (S.mode === 'ai') { S.names = ['我', '电脑']; S.avatars = ['🙂', '🤖']; }
  else { S.names = ['玩家一', '玩家二']; S.avatars = ['🧑', '👦']; }
  bridgeHealth().then(ok => {
    if (S.mode === 'ai') toast(ok ? '🤖 已接入 AI 机器人（深度模型驱动电脑并优化提示）' : 'AI 机器人未连接，电脑使用内置 AI', 3000);
  });
  showScreen('game');
  nextRound();
}
function nextRound() {
  if (S.roundNo >= S.rounds) return showMatchEnd();
  startRound();
}
function startRound() {
  clearTimeout(S.aiTimer);
  const deck = shuffle(buildDeck());
  S.hands = [sortHand(deck.slice(0, 24)), sortHand(deck.slice(24, 48))];
  S.roundNo++;
  S.last = null; S.shown = [null, null];
  S.bombs = []; S.playsMade = [0, 0];
  S.selected = new Set(); S.hints = []; S.hintIdx = -1;
  S.phase = 'playing';
  // 红桃十持有者
  S.red10Holder = null;
  if (S.opts.red10) {
    for (let seat = 0; seat < 2; seat++)
      if (S.hands[seat].some(c => c.r === 10 && c.s === 1)) S.red10Holder = seat;
  }
  // 先出权
  let leader;
  if (S.roundNo === 1) {
    leader = S.hands[0].some(c => c.r === 3 && c.s === 0) ? 0 : 1;
    toast(S.names[leader] + '持有黑桃3，先出牌', 2600);
  } else leader = S.lastWinner;
  S.turn = leader;
  S.roundLeader = leader;
  if (S.mode === 'ai') bridgeNewRound();           // 同步影子牌局给 AI 机器人
  hideModal();
  beginTurn();
}
function beginTurn() {
  if (S.phase !== 'playing') return;
  if (S.mode === 'hotseat') {
    S.awaiting = S.turn;
    S.selected = new Set(); S.hints = []; S.hintIdx = -1;
    render();
    showHandover();
    return;
  }
  if (S.turn === 1) {                                    // AI
    render();
    S.aiTimer = setTimeout(aiMove, 750 + randInt(600));
  } else render();
}
function confirmHandover() {
  S.awaiting = null;
  $('handover').classList.add('hidden');
  render();
}
function showHandover() {
  const seat = S.awaiting;
  if (seat == null) return;
  $('ho-icon').textContent = S.avatars[seat];
  $('ho-title').textContent = '轮到 ' + S.names[seat];
  $('ho-desc').innerHTML = hasHintRight(seat)
    ? '请确认对方看不到屏幕后再继续。<b>你有 AI 出牌提示</b>（点「💡提示」获取建议）。'
    : '请确认对方看不到屏幕后再继续。<b>你没有 AI 提示</b>，凭实力出牌！';
  $('handover').classList.remove('hidden');
}
function applyPlay(seat, cards) {
  if (S.phase !== 'playing' || S.turn !== seat) return;
  const hand = S.hands[seat];
  const combo = analyzeShape(cards);
  if (!combo) return;
  hand.splice(0, hand.length, ...removeCards(hand, cards));
  S.playsMade[seat]++;
  const prev = S.last;
  S.shown[seat] = { cards: cards.slice(), combo, pass: false };
  S.last = { combo, cards: cards.slice(), by: seat };
  let msg = '';
  if (combo.t === 'bomb') {
    if (prev && prev.combo.t === 'bomb') {               // 用更大的炸弹压掉上家炸弹：被压的不计分
      for (let i = S.bombs.length - 1; i >= 0; i--) {
        if (!S.bombs[i].beaten) { S.bombs[i].beaten = true; break; }
      }
    }
    S.bombs.push({ by: seat, beaten: false });
    msg = '💣 炸弹！结算时收 ' + BOMB_SCORE + ' 分';
  }
  S.selected = new Set(); S.hints = []; S.hintIdx = -1;
  if (S.mode === 'ai') bridgeMirror(seat, cards);             // 镜像出牌（历史必记）
  if (hand.length === 1) toast('⚠ ' + S.names[seat] + ' 报单！只剩1张', 1800);
  if (msg) toast(msg, 1600);
  if (hand.length === 0) { S.lastWinner = seat; endRound(seat); return; }
  S.turn = 1 - seat;
  beginTurn();
}
function applyPass(seat) {
  if (S.phase !== 'playing' || S.turn !== seat) return;
  S.shown[seat] = { pass: true };
  S.last = null;                                          // 对方获得自由出牌权
  S.selected = new Set(); S.hints = []; S.hintIdx = -1;
  if (S.mode === 'ai') bridgeMirror(seat, []);                // 镜像过牌
  S.turn = 1 - seat;
  beginTurn();
}

/* ================= 结算 ================= */
function settle(winner) {
  const loser = 1 - winner;
  const rem = S.hands[loser].length;
  const shut = S.playsMade[loser] === 0;
  let base = rem === 1 ? 0 : rem;
  if (shut) base *= 2;
  const surv = S.bombs.filter(b => !b.beaten);
  const bw = surv.filter(b => b.by === winner).length;
  const bl = surv.filter(b => b.by === loser).length;
  let dW = base + BOMB_SCORE * (bw - bl);
  let dL = -dW, redTxt = '';
  if (S.opts.red10 && S.red10Holder != null) {
    if (S.red10Holder === winner) { dW *= 2; dL = -dW; redTxt = S.names[winner] + '持有红桃十，翻倍'; }
    else { dL *= 2; dW = -dL; redTxt = S.names[loser] + '持有红桃十，翻倍'; }
  }
  S.total[winner] += dW; S.total[loser] += dL;
  return { winner, loser, rem, shut, base, bw, bl, dW, dL, redTxt };
}
function endRound(winner) {
  S.phase = 'roundEnd';
  clearTimeout(S.aiTimer);
  const r = settle(winner);
  const d0 = r.winner === 0 ? r.dW : r.dL;
  const d1 = r.winner === 0 ? r.dL : r.dW;
  S.history.push({ no: S.roundNo, winner: r.winner, rem: r.rem, shut: r.shut, base: r.base, bw: r.bw, bl: r.bl, redTxt: r.redTxt, d0, d1 });
  render();
  showRoundModal(r);
}
function showRoundModal(r) {
  const w = r.winner;
  $('m-title').textContent = '🏆 ' + S.names[w] + ' 获胜';
  const lines = [];
  lines.push(`<div class="res-line"><span class="k">底分</span><span class="v">${S.names[r.loser]}剩 ${r.rem} 张 × ${BASE}分` +
    (r.rem === 1 ? '（仅剩1张，不计分）' : '') + (r.shut ? '，<b>关门！失分×2</b>' : '') + ` = <b class="num">${r.base}</b> 分</span></div>`);
  if (r.bw + r.bl > 0) {
    lines.push(`<div class="res-line"><span class="k">炸弹</span><span class="v">${S.names[r.winner]} ${r.bw} 颗、${S.names[r.loser]} ${r.bl} 颗（每颗 ${BOMB_SCORE} 分，被压掉的不计）</span></div>`);
  }
  if (r.redTxt) lines.push(`<div class="res-line"><span class="k">红桃十</span><span class="v">${r.redTxt}（输赢×2）</span></div>`);
  lines.push(`<div class="res-line hl"><span class="k">本局得分</span><span class="v">${S.names[w]} <b class="num pos">+${r.dW}</b> ｜ ${S.names[r.loser]} <b class="num neg">${r.dL}</b></span></div>`);
  lines.push(`<div class="res-line"><span class="k">累计</span><span class="v">${S.names[0]} <b>${S.total[0]}</b> : <b>${S.total[1]}</b> ${S.names[1]}（第 ${S.roundNo}/${S.rounds} 局）</span></div>`);
  $('m-list').innerHTML = lines.join('');
  const last = S.roundNo >= S.rounds;
  $('btn-next').classList.remove('hidden');          // 最后一局也要能点进总成绩页
  $('btn-next').textContent = last ? '查看总成绩 →' : '下一局 →';
  $('btn-rematch').classList.add('hidden');
  $('btn-back-lobby').classList.add('hidden');
  $('modal').classList.remove('hidden');
}
function showMatchEnd() {
  S.phase = 'matchEnd';
  const w = S.total[0] > S.total[1] ? 0 : S.total[1] > S.total[0] ? 1 : -1;
  $('m-title').textContent = w < 0 ? '🤝 平局' : `🎉 ${S.names[w]} 最终获胜`;
  let html = `<div class="big-score">总比分　${S.names[0]} <b>${S.total[0]}</b> : <b>${S.total[1]}</b> ${S.names[1]}</div>`;
  html += '<div class="score-table-wrap"><table class="score-table"><tr><th>局</th><th>胜者</th><th>底分</th><th>炸弹</th><th>红十</th><th>' + S.names[0] + '</th><th>' + S.names[1] + '</th></tr>';
  for (const h of S.history) {
    html += `<tr><td>${h.no}</td><td>${S.names[h.winner]}</td><td>${h.base}${h.shut ? '(关门×2)' : ''}${h.rem === 1 ? '(剩1不计)' : ''}</td>` +
      `<td>${h.bw || 0}/${h.bl || 0}</td><td>${h.redTxt ? '×2' : '-'}</td>` +
      `<td class="${h.d0 >= 0 ? 'pos' : 'neg'}">${h.d0 > 0 ? '+' : ''}${h.d0}</td><td class="${h.d1 >= 0 ? 'pos' : 'neg'}">${h.d1 > 0 ? '+' : ''}${h.d1}</td></tr>`;
  }
  html += `<tr class="total"><td colspan="5">累计</td><td>${S.total[0]}</td><td>${S.total[1]}</td></tr></table></div>`;
  $('m-list').innerHTML = html;
  $('btn-next').classList.add('hidden');
  $('btn-rematch').classList.remove('hidden');
  $('btn-back-lobby').classList.remove('hidden');
  $('modal').classList.remove('hidden');
}
function showScoreboard() {
  let html;
  if (!S.history.length) html = '<div class="hint-modal-desc">还没有完成的局。</div>';
  else {
    html = '<div class="score-table-wrap"><table class="score-table"><tr><th>局</th><th>胜者</th><th>底分</th><th>炸弹</th><th>红十</th><th>' + S.names[0] + '</th><th>' + S.names[1] + '</th></tr>';
    for (const h of S.history) {
      html += `<tr><td>${h.no}</td><td>${S.names[h.winner]}</td><td>${h.base}${h.shut ? '(关门×2)' : ''}${h.rem === 1 ? '(剩1不计)' : ''}</td>` +
        `<td>${h.bw || 0}/${h.bl || 0}</td><td>${h.redTxt ? '×2' : '-'}</td>` +
        `<td class="${h.d0 >= 0 ? 'pos' : 'neg'}">${h.d0 > 0 ? '+' : ''}${h.d0}</td><td class="${h.d1 >= 0 ? 'pos' : 'neg'}">${h.d1 > 0 ? '+' : ''}${h.d1}</td></tr>`;
    }
    html += `<tr class="total"><td colspan="5">累计</td><td>${S.total[0]}</td><td>${S.total[1]}</td></tr></table></div>`;
    html += `<div class="big-score">当前　${S.names[0]} <b>${S.total[0]}</b> : <b>${S.total[1]}</b> ${S.names[1]}</div>`;
  }
  $('score-table').innerHTML = html;
  $('score-modal').classList.remove('hidden');
}

/* ================= 渲染 ================= */
function showScreen(name) {
  S.screen = name;
  $('lobby').classList.toggle('hidden', name !== 'lobby');
  $('game').classList.toggle('hidden', name !== 'game');
}
function hideModal() { $('modal').classList.add('hidden'); }
function myTurnHuman() {
  if (S.phase !== 'playing' || S.awaiting != null) return -1;
  if (S.mode === 'ai') return S.turn === 0 ? 0 : -1;
  return S.turn;
}
function hasHintRight(seat) {
  if (S.mode === 'ai') return seat === 0;
  return seat === 0;                                     // 热座：玩家一有提示
}
function comboName(c) {
  const R = RANK_NAME[c.key];
  switch (c.t) {
    case 'single': return '单张 ' + R;
    case 'pair': return '对子 ' + R + R;
    case 'triple': return '三张 ' + R + R + R;
    case 't1': return '三带一（' + R + '）';
    case 't2': return '三带二（' + R + '）';
    case 'straight': return '顺子';
    case 'pairseq': return '连对';
    case 'plane': return '飞机';
    case 'planeBare': return '纯飞机';
    case 'bomb': return '💣 炸弹 ' + R;
    case 'quad3': return '四带三（' + R + '）';
  }
  return '';
}
function render() {
  if (S.screen !== 'game') return;
  const me = S.mode === 'ai' ? 0 : S.turn;               // 热座渲染当前座位视角
  const opp = 1 - me;
  // 顶栏
  $('g-round').textContent = S.roundNo; $('g-rounds').textContent = S.rounds;
  $('g-mode').textContent = S.mode === 'ai' ? '人机对战' : '双人热座';
  $('g-score').innerHTML = `${S.names[0]} <b class="${S.total[0] >= S.total[1] ? 'pos' : 'neg'}">${S.total[0]}</b> : <b class="${S.total[1] >= S.total[0] ? 'pos' : 'neg'}">${S.total[1]}</b> ${S.names[1]}`;
  // 对手座位
  $('opp-avatar').textContent = S.avatars[opp];
  $('opp-name').textContent = S.names[opp];
  $('opp-count').textContent = S.hands[opp].length;
  const oppTags = [];
  if (S.mode === 'ai') oppTags.push('<span class="tag ai">' + (S.bridgeMode ? 'AI·深度模型' : 'AI·内置') + '</span>');
  else oppTags.push('<span class="tag human">真人</span>');
  $('opp-tags').innerHTML = oppTags.join('');
  $('seat-opp').classList.toggle('turn', S.turn === opp && S.phase === 'playing');
  $('opp-bao').classList.toggle('hidden', S.hands[opp].length !== 1);
  $('opp-backs').innerHTML = '<i></i>'.repeat(Math.min(24, S.hands[opp].length));
  // 我方座位
  $('my-avatar').textContent = S.avatars[me];
  $('my-name').textContent = S.names[me];
  $('my-count').textContent = S.hands[me].length;
  const myTags = [];
  if (S.mode === 'ai') myTags.push('<span class="tag human">真人</span>', '<span class="tag hint-on">有AI提示</span>');
  else myTags.push('<span class="tag human">真人</span>', hasHintRight(me) ? '<span class="tag hint-on">有AI提示</span>' : '<span class="tag hint-off">无提示</span>');
  $('my-tags').innerHTML = myTags.join('');
  $('my-seat').classList.toggle('turn', S.turn === me && S.phase === 'playing');
  $('my-bao').classList.toggle('hidden', S.hands[me].length !== 1);
  // 出牌展示
  const so = S.shown[opp], sm = S.shown[me];
  $('opp-play-cards').innerHTML = so && !so.pass ? cardsHTML(so.cards) : '';
  $('opp-play-label').innerHTML = !so ? '' : so.pass
    ? `<span class="pass">${S.names[opp]}：不出</span>`
    : `${S.names[opp]}：${comboName(so.combo)}${so.combo.t === 'bomb' ? '<span class="bomb"> 💣</span>' : ''}`;
  $('my-play-cards').innerHTML = sm && !sm.pass ? cardsHTML(sm.cards) : '';
  $('my-play-label').innerHTML = !sm ? '' : sm.pass
    ? `<span class="pass">${S.names[me]}：不出</span>`
    : `${S.names[me]}：${comboName(sm.combo)}${sm.combo.t === 'bomb' ? '<span class="bomb"> 💣</span>' : ''}`;
  // 中央信息 & 按钮
  renderHandAndActions(me);
}
function renderHandAndActions(me) {
  const human = myTurnHuman();
  const isMyTurn = human === me;
  const hand = (S.mode === 'hotseat' && S.awaiting != null) ? [] : S.hands[me];
  // 中央信息
  const cm = $('center-msg');
  if (S.phase === 'playing') {
    if (isMyTurn) cm.textContent = S.last ? '轮到你了：管上或不出' : '轮到你了：任意牌型先出';
    else if (S.mode === 'ai') cm.textContent = '电脑思考中…';
    else cm.textContent = '等待 ' + S.names[S.turn] + ' 出牌…';
  } else cm.textContent = '';
  cm.classList.toggle('warn', isMyTurn && !!S.last && legalPlays(S.hands[me], { last: S.last.combo, oppCount: S.hands[1 - me].length }, S.opts).length === 0);
  // 手牌
  const handEl = $('hand');
  handEl.innerHTML = hand.map(c => cardHTML(c, false)).join('');
  handEl.querySelectorAll('.card').forEach(el => {
    const id = +el.dataset.id;
    if (S.selected.has(id)) el.classList.add('sel');
    el.addEventListener('click', () => onCardClick(id));
  });
  layoutHand();
  relayoutHand();   // rAF 再测一次，防止媒体查询/字体加载导致的宽度变化
  // 按钮
  let legal = null;
  if (isMyTurn) legal = legalPlays(S.hands[me], { last: S.last ? S.last.combo : null, oppCount: S.hands[1 - me].length }, S.opts);
  const selCards = S.hands[me].filter(c => S.selected.has(c.i));
  let playOK = false;
  if (isMyTurn && selCards.length) {
    const v = validateSelection(me, selCards);
    playOK = v.ok;
  }
  $('btn-play').disabled = !playOK;
  // 不出：跟牌且没有能管上的牌时才允许
  const canPass = isMyTurn && !!S.last && legal.length === 0;
  $('btn-pass').disabled = !canPass;
  // 提示按钮
  const showHintBtn = isMyTurn && hasHintRight(me);
  $('btn-hint').classList.toggle('hidden', !showHintBtn);
  $('btn-hint').disabled = !showHintBtn;
  // 提示条
  const hb = $('hint-bar');
  if (!showHintBtn || !S.hints.length) hb.classList.add('hidden');
  // 转动提示文字
  const tip = $('turn-tip');
  if (!isMyTurn) tip.textContent = '';
  else if (!S.last) tip.textContent = '你先出，可出任意牌型';
  else if (legal.length === 0) tip.textContent = '没有能管上的牌，请点「不出」';
  else tip.textContent = '有牌必打：必须管上';
}
function layoutHand() {
  const handEl = $('hand');
  const cards = [...handEl.querySelectorAll('.card')];
  const n = cards.length;
  if (!n) return;
  const W = handEl.clientWidth - 12;
  const cw = cards[0].getBoundingClientRect().width;
  let step = n > 1 ? Math.min(cw * 0.72, (W - cw) / (n - 1)) : 0;
  if (step < 13) step = Math.max(11, step);
  cards.forEach((el, idx) => {
    el.style.marginLeft = idx === 0 ? '0' : (step - cw) + 'px';
    el.style.zIndex = el.classList.contains('sel') ? 60 : idx + 1;   // 选中的牌浮在最上层
  });
}
// 视口尺寸变化（含移动端旋转）后重测一次，rAF 确保在样式应用后再测量
function relayoutHand() {
  requestAnimationFrame(() => { if (S.screen === 'game') layoutHand(); });
}

/* ================= 出牌校验 ================= */
function comboErrText(cards) {
  return '不是有效牌型（单张/对子/连对/顺子5-12张/三带二/飞机/炸弹）';
}
function validateSelection(seat, cards) {
  if (!cards.length) return { ok: false, err: '请先选牌' };
  const combo = analyzeShape(cards);
  if (!combo) return { ok: false, err: comboErrText(cards) };
  const hand = S.hands[seat];
  if (!comboContextOK(combo, hand.length, S.opts)) {
    if (combo.t === 'quad3') return { ok: false, err: '本局未开启「四带三」选项' };
    return { ok: false, err: '纯三张 / 三带一 / 纯飞机只能在最后一手（正好出完）打出' };
  }
  if (S.last && !canBeat(combo, S.last.combo, S.opts)) return { ok: false, err: '管不上上家的牌（同牌型且更大，或炸弹）' };
  if (breaksBomb(hand, cards, S.opts)) return { ok: false, err: '已开启「炸弹不可拆」，不能拆开炸弹' };
  if (violatesBaodan(hand, combo, S.hands[1 - seat].length)) return { ok: false, err: '对方报单！你出单张必须出手中最大的单张' };
  return { ok: true, combo };
}

/* ================= 交互 ================= */
function onCardClick(id) {
  const me = myTurnHuman();
  if (me < 0) return;
  if (S.selected.has(id)) S.selected.delete(id);
  else S.selected.add(id);
  S.hints = []; S.hintIdx = -1;
  render();
}
let chosenMode = 'ai';
function initLobby() {
  document.querySelectorAll('.mode').forEach(el => {
    el.addEventListener('click', () => {
      document.querySelectorAll('.mode').forEach(x => x.classList.remove('active'));
      el.classList.add('active');
      chosenMode = el.dataset.mode;
    });
  });
  document.querySelector('.mode[data-mode="ai"]').classList.add('active');
  $('btn-start').addEventListener('click', () => { startMatch(); });
}
function humanPlay() {
  const me = myTurnHuman();
  if (me < 0) return;
  const sel = S.hands[me].filter(c => S.selected.has(c.i));
  const v = validateSelection(me, sel);
  if (!v.ok) { toast(v.err); return; }
  applyPlay(me, sel);
}
function humanPass() {
  const me = myTurnHuman();
  if (me < 0) return;
  if (!S.last) { toast('先出牌，不能不出'); return; }
  const legal = legalPlays(S.hands[me], { last: S.last.combo, oppCount: S.hands[1 - me].length }, S.opts);
  if (legal.length) { toast('有牌必打：你能管上，不能不出'); render(); return; }
  applyPass(me);
}
async function showHint() {
  const me = myTurnHuman();
  if (me < 0 || !hasHintRight(me)) return;
  let p = await bridgeSuggestForTurn();                // 优先：AI 机器人深度模型
  let src = '深度模型';
  if (!p) {
    const cands = aiCandidates(me);                    // 降级：内置贪心 AI
    if (!cands.length) { toast('没有能管上的牌，请点「不出」'); return; }
    S.hints = cands.slice(0, 6).map(x => x.p);
    S.hintIdx = (S.hintIdx + 1) % S.hints.length;
    p = S.hints[S.hintIdx];
    src = '内置AI';
  } else {
    S.hints = [p]; S.hintIdx = 0;
  }
  S.selected = new Set(p.cards.map(c => c.i));
  const hb = $('hint-bar');
  hb.classList.remove('hidden');
  $('hint-text').innerHTML = '建议<b>[' + src + ']</b> ' + comboName(p.combo) + '：<span class="hint-cards">' +
    p.cards.map(c => cardText(c)).join(' ') + '</span>' +
    (src === '内置AI' ? '（第 ' + (S.hintIdx + 1) + '/' + S.hints.length + ' 个，再点提示切换）' : '');
  render();
}
function quitToLobby() {
  clearTimeout(S.aiTimer);
  S.phase = 'idle'; S.awaiting = null;
  hideModal();
  $('handover').classList.add('hidden');
  $('hint-bar').classList.add('hidden');
  showScreen('lobby');
}

/* ================= 初始化 ================= */
function init() {
  initLobby();
  // AI 机器人回调：深度模型驱动 AI 座位；返回的牌必须命中我方合法候选，否则降级内置 AI
  pdkRegisterAI(async (ctx) => {
    if (!bridge.ready) return null;
    try {
      const r = await bridgeApi('/act', { sid: bridge.sid });
      if (!r.cards) throw new Error(r.error || 'act failed');
      const hit = matchLegalByRank(r.cards, ctx.legal);
      if (!hit) { bridgeResync(); return null; }
      bridge.actPending = true;              // 桥内已落子：applyPlay 镜像时只记历史不重发
      return hit.cards;
    } catch { bridgeResync(); return null; }
  });
  $('btn-play').addEventListener('click', humanPlay);
  $('btn-pass').addEventListener('click', humanPass);
  $('btn-hint').addEventListener('click', showHint);
  $('btn-adopt').addEventListener('click', humanPlay);
  $('btn-handover-ok').addEventListener('click', confirmHandover);
  $('btn-next').addEventListener('click', () => { hideModal(); nextRound(); });
  $('btn-rematch').addEventListener('click', () => { hideModal(); startMatch(); });
  $('btn-back-lobby').addEventListener('click', quitToLobby);
  $('btn-quit').addEventListener('click', quitToLobby);
  $('btn-rules').addEventListener('click', () => $('rules-modal').classList.remove('hidden'));
  $('btn-rules-close').addEventListener('click', () => $('rules-modal').classList.add('hidden'));
  $('btn-score').addEventListener('click', showScoreboard);
  $('btn-score-close').addEventListener('click', () => $('score-modal').classList.add('hidden'));
  window.addEventListener('resize', relayoutHand);
  window.addEventListener('orientationchange', relayoutHand);
}
init();
