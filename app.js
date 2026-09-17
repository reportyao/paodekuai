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
  deckSpec: 16,                                // 牌副规格：16（经典，深度AI）| 15（新玩法，简易AI）
  opts: { sanzhang: false, nobomb: true, red10: false, four3: false },
  rounds: 10, roundNo: 0,
  total: [0, 0], history: [],
  names: ['我', '电脑'], avatars: ['🙂', '🤖'],
  hands: [[], []], kitty: [], turn: 0,
  initialHands: [[], []], roundMoves: [],
  last: null,                                  // {combo, cards, by}
  shown: [null, null],                         // 各家最近动作（出牌/不出），用于展示
  bombs: [], playsMade: [0, 0], red10Holder: null,
  lastWinner: null, phase: 'idle',             // 'idle'|'playing'|'roundEnd'|'matchEnd'
  awaiting: null,                              // 热座：等待确认看牌的座位
  selected: new Set(), hints: [], hintIdx: -1,
  bridgeMode: false, roundLeader: 0,           // AI 机器人桥接状态 / 本局先手
  aiBlocked: false, aiErrorMsg: '',             // 不降级：AI 异常时暂停本局
  revealOpp: false,                            // 测试：实时明牌对手手牌
  replay: null, replayArchive: [],
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
/* ===== 15张玩法（牌副规格置顶选择）=====
 * 牌副 45 张 = 48张副再去掉 ♦A(id46)、♣A(id45) 和 ♦K(id43)
 *（等价于"去三个2、三个A、一个K"：三个2只留黑桃2、A剩1张、K剩3张）
 * 每局随机弃 15 张底牌，两家各 15 张；其余规则与 16 张完全一致。
 */
const DECK15_SKIP = new Set([43, 45, 46]);
function buildDeck15() { return buildDeck().filter(c => !DECK15_SKIP.has(c.i)); }
function deckSize() { return S.deckSpec === 15 ? 15 : 16; }
function isDeck15() { return S.deckSpec === 15; }
function sortHand(h) { h.sort((a, b) => b.r - a.r || a.s - b.s); return h; }
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

/* ================= AI 机器人桥接（生产版 pdk-ai 双模式） =================
 * 桥地址：网页由 server.py 托管时走同源 /ai 反代（本地与线上一致）；
 * file:// 直开时回退直连本机桥。 */
const AI_BRIDGE = location.protocol.startsWith('http')
  ? location.origin + '/ai'
  : 'http://127.0.0.1:8766';
const bridge = { sid: null, ready: false, mode: '', actions: [], actPending: false, initHands: null, initKitty: null, syncing: false, initing: false, waiters: [], no: null, file: null, lastErr: '', _syncTask: null };

function toBotCard(c) {
  if (c.r === 14) return 44 + ([1, 2, 3].indexOf(c.s)); // pdk_ai的三张A槽位: 44,45,46
  if (c.r === 15) return 48;                                  // 唯一的黑桃2 (pdk_ai 槽位 48)
  return ((c.r - 3) << 2) | c.s;
}
function botRankToMy(idx) { return idx === 12 ? 15 : idx + 3; }   // bot点数0=3..11=A,12=2
function rankSig(cards) { return cards.map(c => c.r).sort((a, b) => a - b).join(','); }
function botRankSig(ids) { return ids.map(id => botRankToMy(id >> 2)).sort((a, b) => a - b).join(','); }
function matchLegalByRank(botIds, legal) {
  const sig = botRankSig(botIds);
  return legal.find(p => rankSig(p.cards) === sig) || null;   // 精确诊断用；出牌路径已改为精确牌映射
}
function fromBotIds(ids, pool) {
  const byRank = new Map();
  for (const c of pool) { const a = byRank.get(c.r) || []; a.push(c); byRank.set(c.r, a); }
  const used = new Map();
  return ids.map(id => {
    const r = botRankToMy(id >> 2), arr = byRank.get(r) || [];
    const n = used.get(r) || 0; used.set(r, n + 1); return arr[n] || null;
  }).filter(Boolean);
}
function legalKey(cs) { return cs.map(c => c.i).sort((a, b) => a - b).join(','); }

/* 对桥的写操作全部串行：跨海链路下镜像/决策可能乱序到达，串行可根除 400 竞态 */
let _bridgeChain = Promise.resolve();
function bridgeEnqueue(fn) {
  const run = _bridgeChain.then(fn, fn);
  _bridgeChain = run.then(() => {}, () => {});
  return run;
}

/* 每次 AI 调用生成 rid（请求 id）：响应原样回显，服务端审计日志按它可检索。
   出问题时弹窗会显示最近一次 rid，把 rid 报给服务方即可精确定位那一次调用。 */
function newRid() {
  return 'web-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
}
async function bridgeApi(path, body, timeoutMs = 15000) {
  const ctl = new AbortController();
  const tm = setTimeout(() => ctl.abort(), timeoutMs);
  const rid = newRid();
  bridge.lastRid = rid;
  try {
    const r = await fetch(AI_BRIDGE + path, {
      method: body !== undefined ? 'POST' : 'GET',
      headers: { 'Content-Type': 'application/json', 'X-Request-Id': rid },
      body: body !== undefined ? JSON.stringify(body === null ? {} : body) : undefined,
      signal: ctl.signal,
    });
    bridge.lastStatus = r.status;
    const txt = await r.text();
    if (!txt) {                                     // 空响应（链路抖动/服务重启时会这样）
      if (!r.ok) {
        const err = new Error('AI 服务返回空响应（HTTP ' + r.status + '），请重试');
        err.status = r.status; err.transport = true; err.rid = rid;
        throw err;
      }
      return {};
    }
    let j = null;
    try { j = JSON.parse(txt); }
    catch (_) {                                     // 非 JSON：不要把原始解析错误抛给用户
      const err = new Error('AI 服务响应异常（HTTP ' + r.status + '，链路抖动/服务重启时会出现），请重试');
      err.status = r.status; err.transport = true; err.rid = rid;
      throw err;
    }
    if (j && j.rid) bridge.lastRid = j.rid;         // 服务端回显的 rid（与本地一致即链路对得上）
    return j;
  } catch (e) {
    bridge.lastRid = (e && e.rid) || rid;           // 失败也保留，便于报障
    if (e && e.name === 'AbortError') {
      e.transport = true; e.timedOut = true;
      e.message = 'AI 正在算牌（超过 ' + Math.round(timeoutMs / 1000) + 's 未回），稍后再点一次';
    } else if (e instanceof TypeError) {
      e.transport = true;                           // fetch 网络层错误
      e.message = 'AI 服务连不上（网络中断或被重置），请重试';
    }
    throw e;
  } finally { clearTimeout(tm); }
}

/* 链路自检：POST /api/selftest 金丝雀（记牌→穷举→数值→顶牌 真实决策路径断言）+ 组件探针 */
const SELFTEST_CN = {
  decide_schema: '领出决策正常',
  a0519_top: '顶牌链路（记牌→穷举→数值计分）',
  must_beat: '有牌必压链路',
  error_contract: '错误契约（400 + 类型 + rid）',
  belief_fields: '新字段契约（推理链/牌型地图/归属/世界推演/张数分布）',
  reasoning_chain: '推理链（台账 L1 / 过牌硬推理 L2 / 行为层 B1-B7）',
  candgen_gate: '合理枚举闸门（三带合理带法 + 牌型家族额度）',
};
const SELFTEST_COMP_CN = {
  c_core: 'C 求解核心', fallback_net: '生产网络', prod_agent: '生产智能体',
  reasoning: '推理引擎', patternmap: '牌型地图', candgen: '合理枚举闸门',
};
async function runSelftest() {
  const box = $('selftest-box');
  const btn = $('btn-selftest');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ 自检中…（约 4 秒）'; }
  if (box) box.innerHTML = '<div class="st-line dim">正在跑链路自检（真实决策路径断言）…</div>';
  try {
    const [st, hd] = await Promise.all([
      bridgeApi('/api/selftest', {}, 60000).catch(e => ({ error: String(e) })),
      bridgeApi('/health', undefined, 10000).catch(() => null),
    ]);
    const comp = (hd && hd.components) || {};
    const bits = [];
    const compBits = Object.entries(comp).map(([k, v]) =>
      `${v.ok ? '✅' : '❌'} ${SELFTEST_COMP_CN[k] || k}` +
      (v.rows ? `(${v.rows} 类)` : v.kept != null ? `(${v.kept}/${v.of})` : ''));
    bits.push(`<div class="st-line">组件：${compBits.join('　') || '<span class="dim">不可用</span>'}</div>`);
    if (hd && hd.pdkCommit && hd.pdkCommit.hash)
      bits.push(`<div class="st-line dim">内核版本 ${esc(hd.pdkCommit.hash)} ｜ 引擎 ${((hd.productionConfig || {}).engine || '?')} ｜ 残局穷举 ≤${((hd.productionConfig || {}).exactWorldsTotal) || 0} 张</div>`);
    if (st && st.cases) {
      bits.push(...st.cases.map(c => {
        const extra = c.check ? ' — ' + esc(String(c.check)) : '';
        return `<div class="st-line">${c.ok ? '✅' : '❌'} ${esc(SELFTEST_CN[c.name] || c.name)} ` +
          `<span class="dim">${c.ms}ms${extra}${c.ok ? '' : ' — ' + esc(String(c.err || '').slice(0, 90))}</span></div>`;
      }));
      bits.push(`<div class="st-line ${st.ok ? 'st-ok' : 'st-bad'}">${st.ok ? '🎉 链路自检全部通过：AI 各组件都在真实运作' : '⚠️ 有链路异常：请把上面的失败项与 rid 报给服务方'}</div>`);
    } else {
      bits.push('<div class="st-line st-bad">❌ 自检接口不可达（AI 服务未连接？）</div>');
    }
    bits.push(`<div class="st-line dim">本次 rid：${esc((st && st.rid) || bridge.lastRid || '-')}</div>`);
    if (box) box.innerHTML = bits.join('');
    return st;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🩺 链路自检'; }
  }
}
async function bridgeHealth() {
  try { const r = await bridgeApi('/health', undefined, 8000); return !!r.ok; } catch { return false; }   // 跨海链路：3s 太短会误判为未连接
}
function bridgeOpts() {
  return { sanzhang: S.opts.sanzhang, nobomb: S.opts.nobomb, red10: S.opts.red10, four3: S.opts.four3 };
}
function prodMode() {
  const el = $('sel-aimode');
  return el && el.value === 'dual' ? 'dual' : 'hybrid';
}
async function bridgeNewRound() {
  bridge.sid = null; bridge.ready = false; bridge.actions = []; bridge.actPending = false;
  bridge.initing = true; bridge.waiters = [];
  bridge.no = null; bridge.file = null;
  const flush = () => { bridge.initing = false; const ws = bridge.waiters.splice(0); ws.forEach(f => f()); };
  try {
    if (S.mode !== 'ai') { setBridgeMode(false); return; }
    // 先固化“本局初始发牌”（无论 init 成功与否）——后续任何时刻都能据此重建影子局
    bridge.initHands = [S.hands[0].map(toBotCard), S.hands[1].map(toBotCard)];
    bridge.initKitty = S.kitty.map(toBotCard);
    const doInit = () => bridgeApi('/init', {
      hands: bridge.initHands, kitty: bridge.initKitty,
      leader: S.roundLeader, opts: bridgeOpts(), mode: prodMode(),
    }, 20000);                                            // 跨海网络放宽到 20s
    // 失败带退避重试（服务重启/网络抖动时不至于整局降级为内置 AI）
    let r = {};
    for (let attempt = 0; attempt < 3 && !r.sid; attempt++) {
      try { r = await doInit(); } catch (e) { r = {}; }
      if (!r.sid && attempt < 2) await new Promise(res => setTimeout(res, 800 * (attempt + 1)));
    }
    if (r.sid) {
      bridge.sid = r.sid; bridge.mode = r.mode || prodMode(); bridge.ready = true;
      bridge.no = r.no || null; bridge.file = r.file || null;   // 本局编号（复盘当局用）
      S.currentNo = r.no || null;
      setBridgeMode(true);
    } else {
      setBridgeMode(false);
      toast('AI 服务未就绪：已暂停对局，不降级（点弹窗里的「重试连接」）', 3000);
      aiError(bridge.lastErr || 'AI 服务未连接（生产模型不可用）');
      scheduleBridgeRecover();
    }
  } catch { setBridgeMode(false); }
  finally { flush(); }                                     // init 完成/失败后才放行 AI 行动，消除先手竞态
  if (S.screen === 'game') render();
}

/* 后台自动重连：桥掉线（含服务重启窗口）时，通过重放本局动作重建影子局，
   避免整局退化为内置 AI，也保证本局仍有编号可复盘。 */
let bridgeRecoverTimer = null;
function scheduleBridgeRecover() {
  if (bridgeRecoverTimer) return;
  bridgeRecoverTimer = setInterval(async () => {
    if (S.mode !== 'ai' || S.phase !== 'playing' || S.screen !== 'game') return;
    if (bridge.ready || bridge.initing || bridge.syncing || !bridge.initHands) return;
    await bridgeResyncQueued();
    if (bridge.ready) {
      clearInterval(bridgeRecoverTimer); bridgeRecoverTimer = null;
      if (S.aiBlocked) {                       // 自动恢复：撤下暂停并继续对局（无需用户操作）
        S.aiBlocked = false; S.aiErrorMsg = '';
        hideModal();
        toast('AI 服务已自动恢复，继续对局（' + (bridge.no ? '本局 ' + bridge.no : '') + '）', 2400);
        if (S.screen === 'game') { render(); beginTurn(); }
      } else if (S.screen === 'game') {
        render();
      }
    }
  }, 6000);
}
async function bridgeResync() {
  // 影子失同步：用本局初始发牌 + 完整动作历史重建会话
  if (!bridge.initHands) return false;
  if (bridge.syncing) return bridge.ready;
  bridge.syncing = true;
  bridge.ready = false;
  try {
    const r = await bridgeApi('/init', {
      hands: bridge.initHands, kitty: bridge.initKitty || [],
      leader: S.roundLeader, opts: bridgeOpts(),
      mode: bridge.mode === 'dual' ? 'dual' : prodMode(),   // 重连保持本局模式
      no: bridge.no, file: bridge.file, sid: bridge.sid,     // 复用同编号/文件/会话

      actions: bridge.actions,
    }, 12000);
    bridge.sid = r.sid || null; bridge.ready = !!r.sid;
    if (r && r.error) bridge.lastErr = r.error;
    if (bridge.ready) {
      bridge.no = r.no || bridge.no; bridge.file = r.file || bridge.file;
      bridge.mode = r.mode || bridge.mode;
      S.currentNo = bridge.no;                              // 重连后编号不变
      setBridgeMode(true);                                  // 标签切回“AI·胜率/净分优先”
      if (bridgeRecoverTimer) { clearInterval(bridgeRecoverTimer); bridgeRecoverTimer = null; }
    }
  } catch (e) { bridge.ready = false; bridge.lastErr = String(e); }
  finally { bridge.syncing = false; }
  return bridge.ready;
}
/* 排队版重建：不与镜像/决策并发；并发调用复用同一任务 */
function bridgeResyncQueued() {
  if (bridge.syncing && bridge._syncTask) return bridge._syncTask;
  bridge._syncTask = bridgeEnqueue(() => bridgeResync());
  return bridge._syncTask;
}
function bridgeMirror(seat, myCards) {
  // 历史无条件记录（重放数据源）；/act 已在桥内落子的只记录不重发
  const wasActApplied = bridge.actPending;
  bridge.actPending = false;
  bridge.actions.push({ seat, cards: myCards.map(toBotCard) });
  const ids = bridge.actions.at(-1).cards;
  const fin = (async () => {
    if (wasActApplied) return true;
    if (!bridge.sid || !bridge.ready) {
      if (S.mode === 'ai' && bridge.initHands) bridgeResyncQueued();   // 无会话 -> 排队重建
      return false;
    }
    // 排队发送：保证“先镜像、后决策”的顺序
    return bridgeEnqueue(async () => {
      try {
        const r = await bridgeApi('/action', { sid: bridge.sid, seat, cards: ids });
        if (r.ok || r.duplicate) return true;
        throw new Error(r.error || 'mirror failed');
      } catch (e) {
        bridge.lastErr = String((e && e.message) || e);
        if (!bridge.syncing) bridgeResyncQueued();
        return false;
      }
    });
  })();
  bridge.lastMirror = fin;          // /suggest 前需等待镜像同步完成
  return fin;
}
/* 桥返回的具体牌 id（与网页手牌同一牌库语义，toBotCard 双射可逆）精确映射回网页手牌。
   返回 null 表示有 id 不在当前手牌里（影子失步信号）。 */
function cardsFromBridge(ids, seat) {
  const map = new Map(S.hands[seat].map(c => [toBotCard(c), c]));
  const out = [];
  for (const id of ids) {
    const c = map.get(id);
    if (!c) return null;
    out.push(c);
  }
  return out;
}

async function bridgeSuggestForTurn() {
  if (!bridge.ready) return { error: bridge.lastErr || 'AI 服务未连接（不提供内置建议）' };
  try {
    const r = await bridgeApi('/suggest', { sid: bridge.sid }, 45000);   // 残局 PIMC 偶发较慢，放宽到45s
    if (r && r.error) return { error: r.error };
    if (r && r.reason) return { error: r.reason };
    if (!r || !Array.isArray(r.cards)) return { error: 'AI 未返回建议（不降级）' };
    if (r.cards.length === 0) {
      // 深度建议：不出。仅当本地确认无解时才采纳（有牌必打保险）
      const legal = legalPlays(S.hands[S.turn], { last: S.last ? S.last.combo : null, oppCount: S.hands[1 - S.turn].length }, S.opts);
      return legal.length === 0 ? { pass: true } : null;
    }
    const cards = cardsFromBridge(r.cards, S.turn);
    if (!cards) { bridgeResync(); return null; }         // 建议牌不在手中 -> 影子失步
    if (!validateSelection(S.turn, cards).ok) { bridgeResync(); return null; }  // 全规则校验
    return { cards, combo: analyzeShape(cards) };
  } catch (e) { return { error: 'AI 提示请求失败：' + e }; }
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
 * 返回值（不降级策略）：
 *   - 牌数组：必须与 legal 中某个候选完全一致（按牌 id 集合匹配），否则视为模型异常；
 *   - []：表示「不出」，需本地确认确实无解，否则视为模型异常；
 *   - null / 抛异常：一律视为 AI 服务异常 —— 暂停对局并弹窗报错，绝不使用内置 AI 代打。
 */
let externalAI = null;                                   // (ctx) => cards|null，由 pdkRegisterAI 注册
function pdkRegisterAI(fn) { externalAI = fn; }

/* ===== 15张模式：内置简易 AI（贪心占位；深度模型接口见 aiMove 内 Pdk15AI.adapter）===== */
function pdk15Greedy(ctx) {
  const legal = (ctx.legal || []).slice();
  if (!legal.length) return null;                          // 无解 -> 过牌
  const isLead = !ctx.last;
  // combo 结构（analyzeShape）：{t:'single'|'pair'|...字符串牌型, key:主点数, len:张数}
  const val = m => {
    const c = m.combo;
    if (c.t === 'bomb') return 10000 + c.key;              // 炸弹尽量留着
    return c.key * 10 + m.cards.length * 0.1;
  };
  // 一手走完直接赢
  const done = legal.find(m => m.cards.length === ctx.hand.length);
  if (done) return done.cards;
  let pick;
  if (isLead) {
    // 领出：最小结构；对手只剩1张时领最大的单张（简单顶牌意识）
    if (ctx.oppCount === 1) {
      const singles = legal.filter(m => m.combo.t === 'single');
      if (singles.length) pick = singles.reduce((a, b) => a.combo.key > b.combo.key ? a : b);
    }
    if (!pick) pick = legal.reduce((a, b) => val(a) <= val(b) ? a : b);
  } else {
    // 跟牌：取最小合法着法（有牌必打：只剩炸弹能管也必须管，与 UI/深度模型一致）
    const nonBomb = legal.filter(m => m.combo.t !== 'bomb');
    pick = (nonBomb.length ? nonBomb : legal).slice().sort((a, b) => val(a) - val(b))[0];
    if (!pick) return null;
  }
  return pick.cards;
}

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
  if (isDeck15()) {
    /* ===== 15张模式：AI 适配接口（预留）=====
     * 深度模型接入时实现 Pdk15AI.adapter = { play(ctx) -> cards数组|[]（过牌）|null（无解） }，
     * ctx 与 16 张 externalAI 相同（hand/last/legal/opts，全部为本地真实数据）。
     * 当前 adapter 为空 -> 走内置简易 AI（贪心：能压取最小、领出取最小结构）。
     */
    const adapterRet = (window.Pdk15AI && typeof window.Pdk15AI.adapter === 'function')
      ? await window.Pdk15AI.adapter(ctx) : null;
    if (adapterRet === null) {
      const mv = pdk15Greedy(ctx);
      cards = mv;                                          // null=过牌（pdk15Greedy 已确认无解）
    } else if (Array.isArray(adapterRet) && adapterRet.length === 0) {
      // 有牌必打：本地与桥内一致，适配器不得在有合法着法时过牌
      if (!ctx.legal || !ctx.legal.length) { applyPass(seat); return; }
      console.warn('[Pdk15AI] 适配器在有合法着法时要求过牌，已改为最小合法着法');
      cards = ctx.legal.slice().sort((a, b) => a.combo.key - b.combo.key)[0].cards;
    } else if (Array.isArray(adapterRet)) {
      cards = adapterRet;
    }
  } else if (typeof externalAI === 'function') {
    if (!bridge.ready && bridge.initHands) {
      const t0 = Date.now();
      while (!bridge.ready && Date.now() - t0 < 25000) {     // 等重建完成（跨海需数秒）
        if (!bridge.syncing) await bridgeResyncQueued();
        await new Promise(r => setTimeout(r, 800));
      }
      if (bridge.ready) toast('AI 服务已恢复，继续对局', 1800);
    }
    try {
      const ret = await externalAI(ctx);
      if (Array.isArray(ret) && ret.length === 0) {
        applyPass(seat);                     // 模型明确过牌（externalAI 已确认本地无解）
        return;
      }
      if (Array.isArray(ret)) cards = ret;   // externalAI 已做精确映射 + validateSelection 校验
    } catch (e) {
      console.error('[PdkAI] 生产模型调用失败：', e);
    }
  }
  if (!cards) {
    // 不降级：AI 服务异常 -> 暂停本局并明确报错，绝不代打
    aiError(S.aiErrorMsg || 'AI 服务未能给出走法（生产模型不可用）');
    return;
  }
  applyPlay(seat, cards);                                // 镜像统一在 applyPlay/applyPass 里做
}

/* 不降级错误处理：暂停对局 + 明确报错 + 重试 */
function aiError(msg) {
  S.aiErrorMsg = msg;
  S.aiBlocked = true;
  clearTimeout(S.aiTimer);
  $('m-title').textContent = '⛔ AI 服务异常（已暂停，不降级）';
  $('m-list').innerHTML =
    `<div class="res-line hl"><span class="v">${String(msg).replace(/[<>]/g, '')}</span></div>` +
    `<div class="res-line"><span class="k">当前状态</span><span class="v">对局已暂停：电脑<b>不会</b>用内置 AI 代打；恢复后自动继续</span></div>` +
    `<div class="res-line"><span class="k">诊断</span><span class="v">` +
    `桥：${bridge.ready ? '已连接' : '未连接'}${bridge.no ? ' ｜ 本局 ' + bridge.no : ''}` +
    `${bridge.lastErr ? ' ｜ 最近错误：' + String(bridge.lastErr).slice(0, 120) : ''}</span></div>` +
    `<div class="res-line"><span class="k">请求 id</span><span class="v"><code>${String(bridge.lastRid || '-').replace(/[<>]/g, '')}</code>` +
    `（报障时提供它，服务方可按 rid 精确定位该次调用）</span></div>`;
  $('btn-next').classList.add('hidden');
  $('btn-rematch').classList.add('hidden');
  $('btn-back-lobby').classList.remove('hidden');
  $('btn-ai-retry').classList.remove('hidden');
  $('modal').classList.remove('hidden');
  scheduleBridgeRecover();
  render();
}
async function aiRetry() {
  hideModal();
  const ok = await bridgeResyncQueued();
  if (ok) {
    S.aiBlocked = false; S.aiErrorMsg = '';
    toast('AI 服务已恢复，继续对局', 2200);
    render();
    beginTurn();
  } else {
    aiError('仍然连不上 AI 服务（生产模型不可用）');
  }
}
/* ================= 在线双人对战（房间制，服务器权威 + 轮询） ================= */
const ONLINE = {
  token: null, seat: -1, code: '',
  st: null,                       // 服务器视角快照
  pollTimer: null, pollBusy: false,
  legalIds: [],                   // 轮到自己时服务器下发的合法动作（牌id列表的列表）
  lastSyncSig: '',
};
function pdkToCard(id) {
  const idx = id >> 2;
  return { i: id, r: idx === 12 ? 15 : idx + 3, s: id & 3 };
}
function onlineApi(path, body, timeoutMs = 15000) {
  const ctl = new AbortController();
  const tm = setTimeout(() => ctl.abort(), timeoutMs);
  return fetch('/online' + path, {
    method: body !== undefined ? 'POST' : 'GET',
    headers: { 'Content-Type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
    signal: ctl.signal,
  }).then(r => r.json()).finally(() => clearTimeout(tm));
}
function onTip(msg) { $('on-tip').textContent = msg || ''; }

async function onlineCreate() {
  const name = $('on-nick').value.trim() || '房主';
  onTip('创建中…');
  try {
    const r = await onlineApi('/api/create', {
      name, rounds: +$('sel-rounds').value,
      opts: { sanzhang: $('opt-sanzhang').checked, nobomb: $('opt-nobomb').checked,
              red10: $('opt-red10').checked, four3: $('opt-four3').checked },
    });
    if (r.error) { onTip(r.error); return; }
    ONLINE.token = r.token; ONLINE.seat = r.seat; ONLINE.code = r.code;
    onTip('房间已创建，房号 ' + r.code + '，等待对手加入…');
    startOnlineGame();
  } catch (e) { onTip('创建失败：' + e.message); }
}
async function onlineJoin() {
  const code = $('on-code').value.trim();
  const name = $('on-nick').value.trim() || '玩家';
  if (!/^\d{4}$/.test(code)) { onTip('请输入4位房号'); return; }
  onTip('加入中…');
  try {
    const r = await onlineApi('/api/join', { code, name });
    if (r.error) { onTip(r.error); return; }
    ONLINE.token = r.token; ONLINE.seat = r.seat; ONLINE.code = r.code;
    startOnlineGame();
  } catch (e) { onTip('加入失败：' + e.message); }
}
function startOnlineGame() {
  S.mode = 'online';
  S.opts = {
    sanzhang: $('opt-sanzhang').checked, nobomb: $('opt-nobomb').checked,
    red10: $('opt-red10').checked, four3: $('opt-four3').checked,
  };
  S.rounds = +$('sel-rounds').value;
  S.total = [0, 0]; S.history = []; S.roundNo = 0; S.lastWinner = null;
  S.names = [ONLINE.seat === 0 ? ($('on-nick').value.trim() || '房主') : '房主',
             ONLINE.seat === 0 ? '对手' : ($('on-nick').value.trim() || '玩家')];
  S.avatars = ['🧑', '🧑'];
  try { localStorage.setItem('pdk_online', JSON.stringify({ token: ONLINE.token, code: ONLINE.code, seat: ONLINE.seat })); } catch {}
  clearTimeout(S.aiTimer); clearTimeout(ONLINE.pollTimer);
  showScreen('game');
  onlinePoll();                                    // 轮询驱动（含等待对手）
}
function onlineRestore() {
  // 刷新/重开浏览器后恢复在线对局（token 仍有效时）
  try {
    const saved = JSON.parse(localStorage.getItem('pdk_online') || 'null');
    if (saved && saved.token) {
      ONLINE.token = saved.token; ONLINE.seat = saved.seat; ONLINE.code = saved.code;
      startOnlineGame();
      return true;
    }
  } catch {}
  return false;
}
function onlineClearSaved() {
  try { localStorage.removeItem('pdk_online'); } catch {}
}
function onlineStopPoll() { clearTimeout(ONLINE.pollTimer); ONLINE.pollTimer = null; }

async function onlinePoll() {
  if (S.mode !== 'online' || S.screen !== 'game') return;
  if (ONLINE.pollBusy) { ONLINE.pollTimer = setTimeout(onlinePoll, 600); return; }
  ONLINE.pollBusy = true;
  try {
    const st = await onlineApi('/api/state?token=' + ONLINE.token, undefined, 12000);
    if (st.error) {
      // 房间失效（过期/被清理/服务重启）：清理本地会话并回到大厅，避免卡在牌桌
      toast('房间已失效（' + st.error + '），已退出到大厅', 3000);
      quitToLobby();
      return;
    }
    ONLINE.st = st; onlineApplyState(st);
  } catch {}                                         // 网络抖动：下一轮继续
  ONLINE.pollBusy = false;
  if (S.mode === 'online' && S.screen === 'game')
    ONLINE.pollTimer = setTimeout(onlinePoll, S.phase === 'playing' ? 900 : 1200);
}
function onlineApplyState(st) {
  const me = st.seat, opp = 1 - me;
  S.names[me] = st.names[me]; S.names[opp] = st.names[opp];
  if (S.names[me] && S.names[opp]) {
    try { localStorage.setItem('pdk_online', JSON.stringify({ token: ONLINE.token, code: ONLINE.code, seat: ONLINE.seat })); } catch {}
  }
  S.roundNo = st.round; S.rounds = st.rounds;
  S.total = st.total.slice();
  S.turn = st.turn; S.kitty = new Array(st.kittyN).fill(null);
  S.history = st.history.map(h => ({ no: h.round, winner: h.winner, rem: h.rem, shut: h.shut, d0: h.delta[0], d1: h.delta[1] }));
  S.hands[me] = st.hand.map(pdkToCard);
  S.hands[opp] = new Array(st.oppN).fill(null);      // 对手手牌不下发，仅张数
  S.shown[me] = st.myShown && !st.myShown.pass
    ? { cards: st.myShown.cards.map(pdkToCard), pass: false }
    : (st.myShown ? { pass: true } : null);
  S.shown[opp] = st.oppShown && !st.oppShown.pass
    ? { cards: st.oppShown.cards.map(pdkToCard), pass: false }
    : (st.oppShown ? { pass: true } : null);
  S.last = st.last ? { combo: trickToCombo(st.last.trick), by: opp } : null;
  ONLINE.legalIds = st.legal || [];
  const prevPhase = S.phase;
  S.phase = st.phase === 'waiting' ? 'waiting' : (st.phase === 'playing' ? 'playing' : 'roundEnd');
  if (st.matchEnd) S.phase = 'matchEnd';
  S.roundResult = st.result || null;
  if (prevPhase !== S.phase && (S.phase === 'roundEnd' || S.phase === 'matchEnd')) {
    if (S.phase === 'roundEnd' && S.roundResult) {
      const rr = S.roundResult;
      S.history.push({ no: st.round, winner: rr.winner, rem: rr.rem, shut: rr.shut, d0: rr.delta[0], d1: rr.delta[1] });
      showOnlineRoundModal(rr, st.matchEnd);
    } else if (S.phase === 'matchEnd') {
      showOnlineMatchEnd(st);
    }
  }
  render();
  // 轮到我时立即拉取一次，降低等待感
  if (st.turn === me && S.phase === 'playing' && !ONLINE.pollBusy) onlinePoll();
}
function trickToCombo(t4) {
  if (!t4) return null;
  const map = { 0: 'single', 1: 'pair', 2: 'pairseq', 3: 'triple', 4: 't2', 5: 't1', 6: 'plane', 7: 'straight', 8: 'bomb', 9: 'quad3' };
  return { t: map[t4[0]] || 'single', key: t4[1] === 12 ? 15 : t4[1] + 3, len: t4[2] };
}
function comboToTrick(combo) {
  const map = { single: 0, pair: 1, pairseq: 2, triple: 3, t2: 4, t1: 5, plane: 6, planeBare: 6, straight: 7, bomb: 8, quad3: 9 };
  return [map[combo.t] ?? 0, combo.key === 15 ? 12 : combo.key - 3, combo.len, combo.k || 0];
}
function showOnlineRoundModal(rr, matchEnd) {
  const w = rr.winner;
  $('m-title').textContent = '🏆 ' + S.names[w] + ' 获胜';
  const lines = [];
  lines.push(`<div class="res-line"><span class="k">底分</span><span class="v">${S.names[1 - w]}剩 ${rr.rem} 张` +
    (rr.rem === 1 ? '（仅剩1张，不计分）' : '') + (rr.shut ? '，<b>关门！失分×2</b>' : '') + `</span></div>`);
  lines.push(`<div class="res-line hl"><span class="k">本局得分</span><span class="v">${S.names[0]} <b class="num ${rr.delta[0] >= 0 ? 'pos' : 'neg'}">${rr.delta[0] > 0 ? '+' : ''}${rr.delta[0]}</b> ｜ ${S.names[1]} <b class="num ${rr.delta[1] >= 0 ? 'pos' : 'neg'}">${rr.delta[1] > 0 ? '+' : ''}${rr.delta[1]}</b></span></div>`);
  lines.push(`<div class="res-line"><span class="k">累计</span><span class="v">${S.names[0]} <b>${S.total[0]}</b> : <b>${S.total[1]}</b> ${S.names[1]}（第 ${S.roundNo}/${S.rounds} 局）</span></div>`);
  $('m-list').innerHTML = lines.join('');
  $('btn-next').classList.remove('hidden');
  $('btn-next').textContent = matchEnd ? '查看总成绩 →' : '下一局 →';
  $('btn-rematch').classList.add('hidden');
  $('btn-back-lobby').classList.add('hidden');
  $('modal').classList.remove('hidden');
  onlineStopPoll();
}
function showOnlineMatchEnd(st) {
  S.phase = 'matchEnd';
  const w = S.total[0] > S.total[1] ? 0 : S.total[1] > S.total[0] ? 1 : -1;
  $('m-title').textContent = w < 0 ? '🤝 平局' : `🎉 ${S.names[w]} 最终获胜`;
  let html = `<div class="big-score">总比分　${S.names[0]} <b>${S.total[0]}</b> : <b>${S.total[1]}</b> ${S.names[1]}</div>`;
  html += '<div class="score-table-wrap"><table class="score-table"><tr><th>局</th><th>胜者</th><th>剩牌</th><th>关门</th><th>' + S.names[0] + '</th><th>' + S.names[1] + '</th></tr>';
  for (const h of S.history) {
    html += `<tr><td>${h.no}</td><td>${S.names[h.winner]}</td><td>${h.rem}</td><td>${h.shut ? '×2' : '-'}</td>` +
      `<td class="${h.d0 >= 0 ? 'pos' : 'neg'}">${h.d0 > 0 ? '+' : ''}${h.d0}</td><td class="${h.d1 >= 0 ? 'pos' : 'neg'}">${h.d1 > 0 ? '+' : ''}${h.d1}</td></tr>`;
  }
  html += `<tr class="total"><td colspan="4">累计</td><td>${S.total[0]}</td><td>${S.total[1]}</td></tr></table></div>`;
  $('m-list').innerHTML = html;
  $('btn-next').classList.add('hidden');
  $('btn-rematch').classList.remove('hidden');
  $('btn-rematch').textContent = '回到大厅';
  $('btn-back-lobby').classList.remove('hidden');
  $('modal').classList.remove('hidden');
  onlineStopPoll();
}
/* 在线模式动作提交 */
async function onlineAct(cards) {
  try {
    const r = await onlineApi('/api/act', { token: ONLINE.token, cards });
    if (r.error) { toast(r.error); return false; }
    return true;
  } catch (e) { toast('提交失败：' + e.message); return false; }
}
/* 在线/热座的深度提示（无状态 decide，pdk-ai 生产内核） */
async function decideHint(seat) {
  const hand = S.hands[seat];
  if (!hand.length) return null;
  const moves = S.roundMoves.map(m => ({
    seat: S.mode === 'online' ? (m.seat === seat ? 0 : 1) : m.seat,
    move: m.cards,                     // roundMoves 里存的已是牌 id（数字）
    pass_on: m.pass_on || undefined,
  }));
  const trick = S.last ? comboToTrick(S.last.combo) : null;
  try {
    const r = await bridgeApi('/api/decide', {
      my_hand: hand.map(toBotCard),
      opp_n: S.hands[1 - seat].length,
      trick, history: moves,
      mode: prodMode(),                  // 与首页「AI 打法」一致（hybrid 胜率 / dual 净分）
    }, 45000);
    if (r && r.error) return { error: r.error };
    if (!r || !Array.isArray(r.move)) return { error: 'AI 未返回建议（不降级）' };
    if (r.move.length === 0) {
      const legal = legalPlays(hand, { last: S.last ? S.last.combo : null, oppCount: S.hands[1 - seat].length }, S.opts);
      return legal.length === 0 ? { pass: true } : null;
    }
    const cards = cardsFromBridge(r.move, seat);
    if (!cards) return null;
    if (!validateSelection(seat, cards).ok) return null;
    return { cards, combo: analyzeShape(cards) };
  } catch { return null; }
}

/* 在线模式专用渲染（结构复用热座 DOM） */
function renderOnline() {
  const st = ONLINE.st || {};
  const me = ONLINE.seat, opp = 1 - me;
  const waiting = S.phase === 'waiting';
  // 顶栏
  $('g-round').textContent = st.round || S.roundNo; $('g-rounds').textContent = st.rounds || S.rounds;
  $('g-mode').textContent = (isDeck15() ? '15张玩法 · ' : '') + '在线对战 · 房号 ' + ONLINE.code;
  $('g-score').innerHTML = `${S.names[0]} <b>${S.total[0]}</b> : <b>${S.total[1]}</b> ${S.names[1]}`;
  // 对手座位
  $('opp-avatar').textContent = '🧑';
  $('opp-name').textContent = S.names[opp];
  $('opp-count').textContent = st.oppN ?? 16;
  $('opp-tags').innerHTML = '<span class="tag human">真人</span>';
  $('seat-opp').classList.toggle('turn', S.turn === opp && S.phase === 'playing');
  $('opp-bao').classList.toggle('hidden', st.oppN !== 1);
  renderOppArea(st.oppN ?? 0, false);
  // 我方座位
  $('my-avatar').textContent = '🙂';
  $('my-name').textContent = S.names[me];
  $('my-count').textContent = st.hand ? st.hand.length : 0;
  $('my-tags').innerHTML = '<span class="tag human">真人</span><span class="tag hint-on">有AI提示</span>';
  $('my-seat').classList.toggle('turn', S.turn === me && S.phase === 'playing');
  $('my-bao').classList.toggle('hidden', st.hand ? st.hand.length !== 1 : true);
  // 出牌区
  const so = S.shown[opp], sm = S.shown[me];
  $('opp-play-cards').innerHTML = so && !so.pass ? cardsHTML(so.cards) : '';
  $('opp-play-label').innerHTML = !so ? '' : so.pass
    ? `<span class="pass">${S.names[opp]}：不出</span>`
    : `${S.names[opp]}：${comboName(analyzeShape(so.cards))}`;
  $('my-play-cards').innerHTML = sm && !sm.pass ? cardsHTML(sm.cards) : '';
  $('my-play-label').innerHTML = !sm ? '' : sm.pass
    ? `<span class="pass">${S.names[me]}：不出</span>`
    : `${S.names[me]}：${comboName(analyzeShape(sm.cards))}`;
  // 中央消息 / 手牌 / 按钮
  const cm = $('center-msg');
  if (waiting) cm.textContent = `等待对手加入… 房号 ${ONLINE.code}（把房号告诉好友）`;
  else if (S.phase === 'playing') {
    const my = myTurnHuman() === me;
    cm.textContent = my ? (S.last ? '轮到你了：管上或不出' : '轮到你了：任意牌型先出')
      : '等待对方出牌…';
  } else cm.textContent = '';
  renderHandAndActions(me);
}

function renderOppArea(n, reveal) {
  const backs = $('opp-backs');
  $('btn-reveal').classList.toggle('hidden', S.mode !== 'ai');
  if (S.revealOpp && S.mode === 'ai') {
    backs.classList.add('revealed');
    backs.innerHTML = cardsHTML(S.hands[1], true);
    $('btn-reveal').textContent = '🙈 暗牌';
  } else {
    backs.classList.remove('revealed');
    backs.innerHTML = '<i></i>'.repeat(Math.min(16, n));
    $('btn-reveal').textContent = '👁 明牌';
  }
}

/* ================= 流程 ================= */
function startMatch() {
  S.mode = chosenMode;
  if (S.deckSpec === 15 && chosenMode === 'online') { toast('15张模式暂不支持在线对战，已切回16张'); S.deckSpec = 16; syncDeckSpecUI(); }
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
  if (isDeck15()) {
    if (S.mode === 'ai') toast('🧪 15张模式：当前为内置简易 AI（深度模型待接入）', 3000);
  } else bridgeHealth().then(ok => {
    if (S.mode === 'ai') toast(ok ? '🤖 已接入生产版 AI 机器人（hybrid / dual 双模式）'
                                   : '⛔ AI 服务未连接：对局将暂停并提示重试（不降级）', 3000);
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
  const kittyN = isDeck15() ? 15 : 16;
  const handN = kittyN;                                   // 两家各 = 底牌数（15/16）
  const deck = shuffle(isDeck15() ? buildDeck15() : buildDeck());
  S.kitty = deck.slice(0, kittyN);
  S.hands = [sortHand(deck.slice(kittyN, kittyN + handN)), sortHand(deck.slice(kittyN + handN, kittyN + 2 * handN))];
  S.initialHands = [S.hands[0].slice(), S.hands[1].slice()];
  S.roundMoves = [];
  S.roundNo++;
  S.last = null; S.shown = [null, null];
  S.bombs = []; S.playsMade = [0, 0];
  S.selected = new Set(); S.hints = []; S.hintIdx = -1;
  clearOppExplain();
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
  if (S.mode === 'ai' && !isDeck15()) bridgeNewRound();   // 16张：同步影子牌局给深度 AI（15张模式不接桥，接口预留）
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
    if (S.mode === 'ai' && S.aiBlocked) return;           // 暂停中：不自动行动
    const startAI = () => {
      if (S.mode === 'ai' && isDeck15()) {                // 15张：简易 AI（本地），不依赖桥
        S.aiTimer = setTimeout(aiMove, 600 + randInt(500));
        return;
      }
      if (S.mode === 'ai' && !bridge.ready) {             // 不降级：桥不可用 -> 报错暂停
        aiError(bridge.lastErr || 'AI 服务未连接（生产模型不可用）');
        return;
      }
      S.aiTimer = setTimeout(aiMove, 600 + randInt(500));
    };
    if (S.mode === 'ai' && !isDeck15() && bridge.initing) bridge.waiters.push(startAI);  // 等桥初始化完成，消除先手竞态
    else startAI();
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
  S.roundMoves.push({ seat, cards: cards.map(c => c.i), combo: { ...combo }, ts: Date.now() });
  if (seat === 1 && S.revealOpp && S.mode === 'ai') showOppExplain(S.roundMoves.length);   // 明牌：解释 AI 这手
  bfOnPly();                                              // 新的一手：面板打开时自动重算记牌猜牌
  if (seat === 0) clearOppExplain();
  if (S.mode === 'ai' && !isDeck15()) bridgeMirror(seat, cards);   // 镜像出牌（仅16张深度AI）
  if (hand.length === 1) toast('⚠ ' + S.names[seat] + ' 报单！只剩1张', 1800);
  if (msg) toast(msg, 1600);
  if (hand.length === 0) { S.lastWinner = seat; endRound(seat); return; }
  S.turn = 1 - seat;
  beginTurn();
}
function applyPass(seat) {
  if (S.phase !== 'playing' || S.turn !== seat) return;
  const passed = S.last;                                  // 被过的牌型（S.last 下面会清空）
  S.shown[seat] = { pass: true };
  S.last = null;                                          // 对方获得自由出牌权
  S.selected = new Set(); S.hints = []; S.hintIdx = -1;
  S.roundMoves.push({ seat, cards: [], combo: null, pass: true,
    pass_on: passed ? comboToTrick(passed.combo) : null, ts: Date.now() });
  if (S.mode === 'ai' && !isDeck15()) bridgeMirror(seat, []);      // 镜像过牌（仅16张深度AI）
  S.turn = 1 - seat;
  if (seat === 1 && S.revealOpp && S.mode === 'ai') showOppExplain(S.roundMoves.length);  // 明牌：AI 为何不出
  bfOnPly();
  if (seat === 0) clearOppExplain();
  beginTurn();
}

function saveReplay(replay) {
  try {
    const key = 'pdk_replays_v1';
    const all = JSON.parse(localStorage.getItem(key) || '[]');
    all.unshift(replay);
    localStorage.setItem(key, JSON.stringify(all.slice(0, 100)));
  } catch (e) { console.warn('[Replay] 保存失败', e); }
}
function loadReplayArchive() {
  try { S.replayArchive = JSON.parse(localStorage.getItem('pdk_replays_v1') || '[]'); }
  catch { S.replayArchive = []; }
}
function downloadJSON(filename, value) {
  const blob = new Blob([JSON.stringify(value, null, 2)], { type: 'application/json;charset=utf-8' });
  const url = URL.createObjectURL(blob), a = document.createElement('a');
  a.href = url; a.download = filename; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
/* ================= 出牌解释（AI 为什么这么出） ================= */
const EXPLAIN_PATH_CN = {
  opening_search: '开局搜索', pimc_c: '残局求解(定胜负)', pimc_cn: '残局求解(算分)',
  endgame_order: '残局连续保权', report_dump: '报单保权', one_shot: '一手打完',
  lookahead: '前瞻搜索', fallback_net: '策略网络', forced: '唯一合法手',
};
const EXPLAIN_CACHE = new Map();          // key -> explain 结果（同一手只算一次）

async function bridgeExplain(payload) {
  const key = JSON.stringify(['ex', payload.sid || '', payload.ply || 0, payload.moves ? payload.moves.length : 0,
                              payload.initial_hands ? payload.initial_hands[0].length : 0]);
  if (EXPLAIN_CACHE.has(key)) return EXPLAIN_CACHE.get(key);
  const r = await bridgeApi('/api/explain', payload, 40000);
  if (!r || r.error || !r.text) throw new Error((r && r.error) || '解释不可用');
  EXPLAIN_CACHE.set(key, r);
  return r;
}

/* 复盘分析：按真实局面问「AI 会怎么打这一手 + 为什么」（AI 手、人类手都适用） */
async function bridgeAnalyze(payload) {
  const key = JSON.stringify(['an', payload.sid || '', payload.ply || 0,
                              payload.moves ? payload.moves.length : 0,
                              payload.initial_hands ? payload.initial_hands[0].length : 0]);
  if (EXPLAIN_CACHE.has(key)) return EXPLAIN_CACHE.get(key);
  const r = await bridgeApi('/api/analyze', payload, 60000);
  if (!r || r.error || !r.recorded) throw new Error((r && r.error) || '分析不可用');
  EXPLAIN_CACHE.set(key, r);
  return r;
}

/* 历史人机局只存了动作码：解码成「谁出的 + 具体哪些牌」才能正确复盘 */
async function bridgeDecode(payload) {
  const r = await bridgeApi('/api/decode', payload, 30000);
  if (!r || r.error || !r.moves) throw new Error((r && r.error) || '解码不可用');
  return r.moves;
}

/* 把解释渲染成紧凑中文（明牌条 & 复盘面板共用） */
function explainHTML(d, opts) {
  const o = opts || {};
  const bits = [];
  if (d.state_text) bits.push(`<div class="row dim">局面：${esc(d.state_text)}</div>`);
  const chosen = d.text || '';
  let line = `<span class="k">AI 出牌</span><span class="pt">${esc(chosen)}</span>`;
  if (d.decided && d.recorded && d.decided !== d.recorded)
    line += ` <span class="warn">（当时记录：${esc(d.recorded)} → AI 现在会改打：${esc(d.decided)}）</span>`;
  bits.push(`<div class="row">${line}</div>`);
  if (d.path) bits.push(`<div class="row"><span class="k">依据</span>${esc(EXPLAIN_PATH_CN[d.path] || d.path)}</div>`);
  if (d.reason) bits.push(`<div class="row"><span class="k">理由</span>${esc(d.reason)}</div>`);
  if (d.note) bits.push(`<div class="row dim">${esc(d.note)}</div>`);
  const cands = (d.cands || []).slice(0, 3).filter(c => c && c.text);
  if (cands.length && !o.hideCands) {
    const txt = cands.map(c => `${esc(c.text)}${c.win_prob != null ? ' ' + Math.round(c.win_prob * 100) + '%' : ''}`).join('、');
    bits.push(`<div class="row dim">候选：${txt}</div>`);
  }
  const facts = d.belief && d.belief.facts;
  if (facts && facts.length) bits.push(`<div class="row dim">算牌：${esc(facts.join('、'))}</div>`);
  if (d.anchored_back) bits.push(`<div class="row dim">（该手不是 AI 决策点，展示 AI 最近一次决策依据）</div>`);
  return bits.join('');
}
function esc(x) { return String(x == null ? '' : x).replace(/[<>]/g, ''); }

/* 复盘分析渲染：这一手实际出了什么 vs AI 会怎么打（人类手=反事实对照） */
function analyzeHTML(r, o) {
  o = o || {};
  const rec = r.recorded || {}, dec = r.decided || {}, ex = r.explain || {};
  const bits = [];
  const who = o.who || (r.seat === 1 ? 'AI' : '你');
  if (r.state_text) bits.push(`<div class="row dim">局面：${esc(r.state_text)}</div>`);
  const one = m => (m.pass || !(m.cards || []).length)
    ? '<span class="warn">不出</span>'
    : `${esc(m.patText || '')} <span class="dim">${esc(rvCardText(m.cards))}</span>`;
  bits.push(`<div class="row"><span class="k">${esc(o.recLabel || who + '实际出')}</span>`
    + `<span class="pt">${one(rec)}</span></div>`);
  const same = (dec.cards || []).join(',') === (rec.cards || []).join(',');
  bits.push(`<div class="row"><span class="k">AI 会打</span><span class="pt">${one(dec)}</span>`
    + (same ? ' <span class="rv-same">与这手一致 ✓</span>' : ' <span class="rv-diff">与这手不同</span>')
    + '</div>');
  if (ex.path) bits.push(`<div class="row"><span class="k">依据</span>${esc(EXPLAIN_PATH_CN[ex.path] || ex.path)}</div>`);
  if (ex.reason) bits.push(`<div class="row"><span class="k">理由</span>${esc(ex.reason)}</div>`);
  if (r.note) bits.push(`<div class="row dim">${esc(r.note)}</div>`);
  const cands = (ex.cands || []).slice(0, 3).filter(c => c && c.text);
  if (cands.length) {
    const txt = cands.map(c => `${esc(c.text)}${c.win_prob != null ? ' ' + Math.round(c.win_prob * 100) + '%' : ''}`).join('、');
    bits.push(`<div class="row dim">候选：${txt}</div>`);
  }
  const facts = ex.belief && ex.belief.facts;
  if (facts && facts.length) bits.push(`<div class="row dim">算牌：${esc(facts.join('、'))}</div>`);
  return bits.join('');
}

/* 明牌模式下：AI 出牌后自动给出理由 */
async function showOppExplain(ply) {
  const box = $('opp-explain');
  if (!box) return;
  if (!(S.mode === 'ai' && !isDeck15() && S.revealOpp && bridge.ready && bridge.sid)) { box.classList.add('hidden'); return; }
  box.classList.remove('hidden');
  box.innerHTML = '<span class="explain-loading">🤖 解析 AI 这手…</span>';
  try {
    const d = await bridgeExplain({ sid: bridge.sid, ply });
    if (!S.revealOpp) { box.classList.add('hidden'); return; }
    box.innerHTML = '🤖 ' + explainHTML(d, { hideCands: false });
  } catch (e) {
    box.innerHTML = '<span class="dim">🤖 解析暂不可用</span>';
  }
}
function clearOppExplain() {
  const box = $('opp-explain');
  if (box) { box.classList.add('hidden'); box.innerHTML = ''; }
}

/* ---- 对局记录：服务器列表（唯一编号 + 时间 + 点评数），点击进入复盘 ---- */
const RV = { game: null, step: 0, total: 0, comments: [], snapshots: [], no: '', names: [] };
const PTT = { 0: '单张', 1: '对子', 2: '连对', 3: '三张', 4: '三带二', 5: '三带一', 6: '飞机', 7: '顺子', 8: '炸弹', 9: '四带三' };
const RV_RANK = '3456789XJQKA2';
const RV_SUIT = '♠♥♣♦';

function rvCardText(ids) { return (ids || []).map(c => RV_SUIT[c & 3] + RV_RANK[c >> 2]).join(' '); }

let HS_SCOPE = 'mine';                                  // mine=我的对局(A/H)  api=对外调用(E)
let HS_LIMIT = 50;                                      // 列表默认只拉最新 50 条（手机端少下 7 倍数据）
let HS_TOTAL = 0;

async function showReplayHistory(scope) {
  if (scope && scope !== HS_SCOPE) { HS_SCOPE = scope; HS_LIMIT = 50; }
  if (scope) HS_SCOPE = scope;
  const box = $('history-list');
  const sbox = $('history-stats');
  box.innerHTML = '<div class="hint-modal-desc">加载中…</div>';
  if (sbox) sbox.innerHTML = '';
  $('history-modal').classList.remove('hidden');
  $('hs-mine').classList.toggle('active', HS_SCOPE === 'mine');
  $('hs-api').classList.toggle('active', HS_SCOPE === 'api');
  try {
    const r = await fetch('/replays/list?scope=' + HS_SCOPE + '&limit=' + HS_LIMIT).then(x => x.json());
    HS_TOTAL = (r && (r.total != null ? r.total : (r.games || []).length)) || 0;
    const games = (r && r.games) || [];
    // 统计条：对外场次看调用方胜负，我的场次看我的胜负与点评数
    try {
      const st = await fetch('/replays/stats?scope=' + HS_SCOPE).then(x => x.json());
      if (sbox && st && st.summary) {
        // 与列表口径一致：列表默认只显示最新 HS_LIMIT 条，这里标注清楚
        if (HS_TOTAL) st.summary.shown_note = '已显示 ' + Math.min(HS_LIMIT, HS_TOTAL) + '/' + HS_TOTAL + ' 局';
        const a = st.summary;
        const parts = [a.shown_note ? `📄 ${a.shown_note}` : '', `共 <b>${a.games}</b> 局`].filter(Boolean);
        if (a.finished) parts.push(`已完成 <b>${a.finished}</b>`);
        if (a.live) parts.push(`进行中 <b>${a.live}</b>`);
        if (a.finished) {
          parts.push(HS_SCOPE === 'api'
            ? `调用方胜 <b>${a.seat0_wins}</b> ｜ AI 胜 <b>${a.seat1_wins}</b>`
            : `我胜 <b>${a.seat0_wins}</b> ｜ AI 胜 <b>${a.seat1_wins}</b>`);
          if (a.avg_moves) parts.push(`平均 <b>${a.avg_moves}</b> 手`);
          if (a.avg_duration) parts.push(`平均用时 <b>${a.avg_duration}s</b>`);
        }
        if (st.by_caller && Object.keys(st.by_caller).length) {
          const cs = Object.entries(st.by_caller).slice(0, 4)
            .map(([k, v]) => `${esc(k)} ${v.finished ? v.seat0_wins + ':' + v.seat1_wins : '—'}`).join('　');
          parts.push(`按调用方：${cs}`);
        }
        sbox.innerHTML = parts.join(' ｜ ');
      }
    } catch (e) { /* 统计失败不影响列表 */ }
    if (!games.length) {
      box.innerHTML = '<div class="hint-modal-desc">' +
        (HS_SCOPE === 'api' ? '还没有外部调用方的对局（对方通过 API 打完一局后会出现在这里）。' : '暂无对局记录。') + '</div>';
      return;
    }
    // 同一编号出现多次（历史编号撞号遗留）时，追加会话短号以便区分，并在复盘时精确锁定
    const dupCount = {};
    games.forEach(g => { dupCount[g.no] = (dupCount[g.no] || 0) + 1; });
    box.innerHTML = games.map(g => `<div class="res-line rv-row" data-no="${g.no}" data-sid="${g.sid || g.file || ''}">
        <span class="rv-no">${g.no}${dupCount[g.no] > 1 && (g.sid || g.file) ? `<em class="rv-sid">·${String(g.sid || g.file).slice(0, 4)}</em>` : ''}</span>
        <span class="v">${g.time} ｜ ${g.kind} ｜ ${g.participants} ｜ ${g.moves}手 ｜ ${g.result}${
          dupCount[g.no] > 1 ? '' : ''}</span>
        <span class="rv-badge${g.comments ? '' : ' hidden'}">✍️${g.comments}</span>
        <button class="btn ghost small">复盘</button>
      </div>`).join('');
    box.querySelectorAll('.rv-row').forEach(el =>
      el.addEventListener('click', () => openReview(el.dataset.no, el.dataset.sid)));
    // 只加载最新 N 条：还有更早的局时给一个"加载更多"（每次 +50）
    if (HS_TOTAL > games.length) {
      const more = document.createElement('button');
      more.className = 'btn ghost small';
      more.id = 'hs-more';
      more.textContent = '加载更多（已显示 ' + games.length + ' / 共 ' + HS_TOTAL + ' 局）';
      more.style.marginTop = '10px';
      more.addEventListener('click', () => { HS_LIMIT += 50; showReplayHistory(); });
      box.appendChild(more);
    }
  } catch (e) {
    loadReplayArchive();
    box.innerHTML = S.replayArchive.length
      ? S.replayArchive.map((x, i) => `<div class="res-line"><span class="k">${new Date(x.ts).toLocaleString()}</span><span class="v">第${x.round}局 · ${x.moves.length}手</span><button class="btn ghost small" data-replay="${i}">下载</button></div>`).join('')
      : '<div class="hint-modal-desc">服务器不可用，且本地无记录。</div>';
    box.querySelectorAll('[data-replay]').forEach(b => b.addEventListener('click', () => downloadJSON('replay-' + b.dataset.replay + '.json', S.replayArchive[+b.dataset.replay])));
  }
}
async function exportAllReplays() {
  try {
    const r = await fetch('/replays/list?limit=all').then(x => x.json());   // 导出必须全量
    const all = [];
    for (const g of (r.games || [])) {
      const d = await fetch('/replays/get?no=' + encodeURIComponent(g.no)).then(x => x.json());
      if (d && d.game) all.push(Object.assign({}, d.game, { humanComments: d.comments || [] }));
    }
    downloadJSON('paodekuai-all-games.json', all);
  } catch { loadReplayArchive(); downloadJSON('paodekuai-local-replays.json', S.replayArchive); }
}

/* ---- 逐手复盘查看器 ---- */
async function openReview(no, sid) {
  $('rv-move').textContent = '加载中…';
  $('review-modal').classList.remove('hidden');
  let data;
  // sid 用于精确锁定会话：历史数据里有"一编号两局"，只按编号会打开另一局（与真实出牌对不上）
  const q = '/replays/get?no=' + encodeURIComponent(no) + (sid ? '&sid=' + encodeURIComponent(sid) : '');
  try { data = await fetch(q).then(x => x.json()); }
  catch { toast('复盘加载失败'); return; }
  if (!data || !data.game) { toast((data && data.error) || '复盘加载失败'); return; }
  const g = data.game;
  RV.game = g; RV.no = g.no || no; RV.comments = data.comments || []; RV.step = 0;
  RV.live = !!g.live;

  const isOnline = g.source === 'online_room';
  let names = g.names || (isOnline ? ['甲', '乙'] : ['你', 'AI']);
  if (!isOnline && names[0] === 'human' && names[1] === 'ai') names = ['你', '电脑'];   // 人机局显示名
  RV.names = names;
  const init0 = ((isOnline ? g.initialHands : g.hands) || [])[0] || [];
  const init1 = ((isOnline ? g.initialHands : g.hands) || [])[1] || [];
  let seq = (g.moves && g.moves.length) ? g.moves : (g.legacyMoves || []);

  // 历史人机局只存了动作码：解码成「谁出的 + 具体哪些牌」，手牌快照与归属才正确
  if (!isOnline && !(g.moves && g.moves.length) && (g.codes || []).length && bridge.ready) {
    $('rv-move').textContent = '正在解码牌谱…';
    try {
      seq = await bridgeDecode({ initial_hands: [init0, init1], first_player: g.leader,
                                opts: g.opts || {}, moves: g.codes });
      g.moves = seq;
    } catch (e) { /* 解码失败：退回旧的“点数文本”显示 */ }
  }
  RV.total = seq.length;

  // 逐步手牌快照（初始 + 每手之后）
  RV.snapshots = [];
  let h0 = init0.slice(), h1 = init1.slice();
  RV.snapshots.push({ h0: h0.slice(), h1: h1.slice(), move: null });
  for (const m of seq) {
    const cards = m.cards || [];
    if (m.seat === 0) h0 = h0.filter(c => cards.indexOf(c) < 0);
    else h1 = h1.filter(c => cards.indexOf(c) < 0);
    RV.snapshots.push({ h0: h0.slice(), h1: h1.slice(), move: m });
  }

  $('rv-title').textContent = '复盘 ' + RV.no;
  $('rv-meta').innerHTML = isOnline
    ? `${g.timeText || ''} ｜ 真人局 房号${g.code} 第${g.round}/${g.rounds}局 ｜ ${names[0]} vs ${names[1]} ｜ 先手 座位${g.firstPlayer}`
    : `${g.timeText || ''} ｜ 人机局 ｜ ${names[0]}(座位0) vs ${names[1]}(座位1) ｜ 模式 ${g.mode || ''} ｜ ${g.net || ''}`;
  $('rv-h0-title').textContent = names[0] + '（座位0）';
  $('rv-h1-title').textContent = names[1] + '（座位1）';

  $('rv-explain-box').classList.add('hidden');
  $('rv-explain-box').innerHTML = '';
  const sel = $('rv-comment-ply');
  sel.innerHTML = '<option value="">整局点评</option>' + seq.map((m, i) =>
    `<option value="${i + 1}">第${i + 1}手（${names[m.seat] || '座位' + m.seat}）</option>`).join('');
  renderReview();
}

function renderReview() {
  const s = RV.snapshots[RV.step];
  if (!s) return;
  const mv = s.move;
  const ptype = (mv && mv.combo) ? (PTT[mv.combo.ptype] || '牌型') : '';
  if (!mv) {
    $('rv-move').innerHTML = '<b>开局</b>：双方各 16 张（下方为初始手牌）';
  } else if (mv.pass) {
    $('rv-move').innerHTML = `<b>第${mv.ply}手</b>：${RV.names[mv.seat]} <span class="rv-pass">不出</span>${mv.pass_on ? '（被过牌型 ' + JSON.stringify(mv.pass_on) + '）' : ''}`;
  } else {
    const shown = mv.text ? mv.text : rvCardText(mv.cards);
    $('rv-move').innerHTML = `<b>第${mv.ply}手</b>：${RV.names[mv.seat]} 出 <span class="rv-cards">${shown}</span>`
      + (ptype ? `（${ptype}）` : '')
      + (mv.handAfter !== undefined ? ` · 出后剩 ${mv.handAfter} 张` : '');
  }
  $('rv-pos').textContent = RV.step + ' / ' + RV.total + (RV.live ? '（进行中）' : '');
  $('rv-refresh').classList.toggle('hidden', !RV.live);
  $('rv-h0').innerHTML = rvHandHTML(s.h0);
  $('rv-h1').innerHTML = rvHandHTML(s.h1);
  renderReviewComments();
}

function rvHandHTML(cards) {
  if (!cards.length) return '<span class="rv-empty">（已出完）</span>';
  return cards.map(c => {
    const red = (c & 3) === 1 || (c & 3) === 3;
    return `<span class="rv-card${red ? ' red' : ''}">${RV_RANK[c >> 2]}<i>${RV_SUIT[c & 3]}</i></span>`;
  }).join('');
}

function renderReviewComments() {
  const box = $('rv-comment-list');
  if (!RV.comments.length) {
    box.innerHTML = '<div class="rv-empty">还没有点评。写下第一条，帮 AI 快速定位学习。</div>';
    return;
  }
  box.innerHTML = RV.comments.map(c => `<div class="rv-comment">
      <span class="rv-tag">${c.ply ? '第' + c.ply + '手' : '整局'}</span>
      <span class="rv-tag2">${c.kind || 'human_comment'}</span>
      <span class="rv-ctext">${String(c.text || '').replace(/[<>]/g, '')}</span>
      <span class="rv-cts">${c.ts || ''} ${c.author || ''}</span>
    </div>`).join('');
}

async function saveReviewComment() {
  const text = $('rv-comment-text').value.trim();
  if (!text) { toast('请先写点评内容'); return; }
  const plyRaw = $('rv-comment-ply').value;
  const body = { no: RV.no, text: text, ply: plyRaw ? +plyRaw : null };
  try {
    const r = await fetch('/replays/comment', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }).then(x => x.json());
    if (!r.ok) { toast(r.error || '保存失败'); return; }
    RV.comments = r.comments || [];
    $('rv-comment-text').value = '';
    renderReviewComments();
    toast('点评已保存（标记 human_review）');
  } catch { toast('保存失败'); }
}

/* ---- 对局页：复盘当局 / 复盘上一局 ---- */
async function reviewCurrentGame() {
  if (!bridge.no) {
    toast('本局暂无编号（AI 服务未连接或这是第一手前）');
    return;
  }
  RV.from = 'game';
  await openReview(bridge.no, bridge.sid);          // 带 sid：同号时锁定"本局"这个会话
  // 默认定位到最新一手，方便就当前局面写点评
  if (RV.total > 0) { RV.step = RV.total; renderReview(); }
  const sel = $('rv-comment-ply');
  if (sel && RV.total > 0) sel.value = String(RV.total);
  $('rv-comment-ply').classList.toggle('hidden', false);
}

async function reviewPreviousGame() {
  RV.from = 'game';
  let no = S.prevRoundNo || null, sid = S.prevRoundSid || null;
  if (!no) {
    // 没打过上一局 -> 取最近一局已结束的对局
    try {
      const r = await fetch('/replays/list?limit=200').then(x => x.json());
      const done = (r.games || []).filter(g => !g.live && g.no !== S.currentNo);
      if (done.length) { no = done[0].no; sid = done[0]._file; }
    } catch {}
  }
  if (!no) { toast('还没有可复盘的上一局'); return; }
  await openReview(no, sid);
  if (RV.total > 0) { RV.step = RV.total; renderReview(); }
  const sel = $('rv-comment-ply');
  if (sel && RV.total > 0) sel.value = String(RV.total);
}

/* 复盘：解析当前这一手 —— AI 手看它当时的依据，人类手看「换 AI 来打会怎么出」 */
async function explainCurrentPly() {
  const box = $('rv-explain-box');
  if (!RV.game) return;
  const g = RV.game;
  const ply = RV.step;                      // 0=开局，不解析
  if (!ply) {
    box.classList.remove('hidden');
    box.innerHTML = '<div class="row dim">请先翻到具体某一手（第 1 手起）再解析。</div>';
    return;
  }
  box.classList.remove('hidden');
  box.innerHTML = '<span class="explain-loading">🤖 AI 正在分析这一手…（含搜索，可能数秒）</span>';
  try {
    const isOnline = g.source === 'online_room';
    // 正在进行的本局：直接问会话（局面 100% 对得上，最快）
    const useSession = !isOnline && bridge.ready && bridge.sid && g.live &&
                       S.mode === 'ai' && S.currentNo === g.no;
    const mvSeat = (g.moves && g.moves[ply - 1]) ? g.moves[ply - 1].seat : (ply - 1) % 2;
    let r;
    if (useSession) {
      r = await bridgeAnalyze({ sid: bridge.sid, ply });
    } else {
      const initial = (isOnline ? g.initialHands : g.hands) || [[], []];
      const moves = (g.moves || []).map(m => m.pass ? [] : (m.cards || []));
      r = await bridgeAnalyze({
        initial_hands: initial, first_player: isOnline ? g.firstPlayer : g.leader,
        opts: g.opts || {}, moves, ply, mode: g.mode || 'hybrid',
      });
    }
    const seat = (r.seat != null) ? r.seat : mvSeat;
    const aiSeat = (r.ai_seat != null) ? r.ai_seat : 1;
    // 人机局：区分“AI 自己的手”与“人类的手（反事实对照）”；真人局：AI 视角
    let title, who;
    if (isOnline) { title = 'AI 视角 · 这一手它会怎么打'; who = '座位' + seat; }
    else if (seat === aiSeat) { title = 'AI 出牌依据（第 ' + ply + ' 手 · AI）'; who = 'AI '; }
    else { title = '换 AI 来打这一手（第 ' + ply + ' 手 · 你）'; who = '你 '; }
    box.innerHTML = '🤖 <b>' + esc(title) + '</b><div style="margin-top:4px">'
      + analyzeHTML(r, { who: (who || ((seat === 1) ? 'AI' : '你')).trim(), recLabel: ((isOnline ? ('座位' + seat) : who) + '实际出').trim() })
      + '</div>';
  } catch (e) {
    box.innerHTML = '<span class="dim">🤖 解析不可用：' + esc((e && e.message) || e) + '</span>';
  }
}

function reviewStep(d) {
  RV.step = Math.max(0, Math.min(RV.total, RV.step + d));
  $('rv-explain-box').classList.add('hidden');
  $('rv-explain-box').innerHTML = '';
  renderReview();
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
  S.prevRoundNo = S.currentNo || null;      // 本局结束 -> 成为“上一局”
  S.prevRoundSid = bridge.sid || null;      // 连同会话号记下：同号时"复盘上局"才打得准
  S.currentNo = null;
  S.phase = 'roundEnd';
  clearTimeout(S.aiTimer);
  const r = settle(winner);
  const d0 = r.winner === 0 ? r.dW : r.dL;
  const d1 = r.winner === 0 ? r.dL : r.dW;
  const replay = {
    version: 1, game: 'paodekuai-2p', ts: new Date().toISOString(),
    round: S.roundNo, mode: S.mode, opts: { ...S.opts },
    initialHands: S.initialHands.map(h => h.map(c => c.i)),
    kitty: S.kitty.map(c => c.i), firstPlayer: S.roundLeader,
    moves: S.roundMoves.slice(), result: { winner: r.winner, loser: r.loser, rem: r.rem, shut: r.shut, base: r.base, bombs: [r.bw, r.bl], redTxt: r.redTxt, delta: [d0, d1], total: S.total.slice() },
  };
  S.replayArchive.push(replay);
  saveReplay(replay);
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
  if (S.mode === 'online') {
    return (ONLINE.st && ONLINE.st.turn === ONLINE.seat) ? ONLINE.seat : -1;
  }
  return S.turn;
}
function hasHintRight(seat) {
  if (S.mode === 'ai') return seat === 0;
  if (S.mode === 'online') return true;                  // 在线双方都有 AI 提示
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
  if (S.mode === 'online') return renderOnline();
  const me = S.mode === 'ai' ? 0 : S.turn;               // 热座渲染当前座位视角
  const opp = 1 - me;
  // 顶栏
  $('g-round').textContent = S.roundNo; $('g-rounds').textContent = S.rounds;
  $('g-mode').textContent = (isDeck15() ? '15张 · ' : '') + (S.mode === 'ai' ? '人机对战' : (S.mode === 'online' ? '在线对战' : '双人热座'));
  const noEl = $('g-game-no');
  noEl.textContent = S.currentNo ? ('本局 ' + S.currentNo) : (S.prevRoundNo ? ('上局 ' + S.prevRoundNo) : '');
  noEl.classList.toggle('hidden', !S.currentNo && !S.prevRoundNo);
  // 复盘按钮：仅人机对战模式（有桥、有编号体系）
  const reviewable = S.mode === 'ai';
  $('btn-review-cur').classList.toggle('hidden', !reviewable);
  $('btn-review-prev').classList.toggle('hidden', !reviewable);
  $('g-score').innerHTML = `${S.names[0]} <b class="${S.total[0] >= S.total[1] ? 'pos' : 'neg'}">${S.total[0]}</b> : <b class="${S.total[1] >= S.total[0] ? 'pos' : 'neg'}">${S.total[1]}</b> ${S.names[1]}`;
  // 对手座位
  $('opp-avatar').textContent = S.avatars[opp];
  $('opp-name').textContent = S.names[opp];
  $('opp-count').textContent = S.hands[opp].length;
  const oppTags = [];
  if (S.mode === 'ai') oppTags.push('<span class="tag ai">' +
    (S.aiBlocked ? 'AI·已暂停（待重试）'
                 : (S.bridgeMode ? 'AI·' + (bridge.mode === 'dual' ? '净分优先' : '胜率优先')
                                 : 'AI·未连接（已暂停）')) + '</span>');
  else oppTags.push('<span class="tag human">真人</span>');
  $('opp-tags').innerHTML = oppTags.join('');
  $('seat-opp').classList.toggle('turn', S.turn === opp && S.phase === 'playing');
  $('opp-bao').classList.toggle('hidden', S.hands[opp].length !== 1);
  // 对手牌背 / 明牌（AI 人机模式的测试功能；热座/在线隐藏防剧透）
  renderOppArea(S.hands[opp].length, S.revealOpp);
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
  if (S.mode === 'online') return renderHandOnline(me);
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
  // 手牌：增量更新，避免每次渲染全量重建导致的“重发一遍”闪烁
  const handEl = $('hand');
  const sig = hand.map(c => c.i).join(',');
  const prevSig = handEl.dataset.sig || '';
  if (sig !== prevSig) {
    handEl.innerHTML = hand.map(c => cardHTML(c, false)).join('');
    handEl.dataset.sig = sig;
    handEl.querySelectorAll('.card').forEach(el => {
      el.addEventListener('click', () => onCardClick(+el.dataset.id));
    });
  }
  handEl.querySelectorAll('.card').forEach(el => {
    el.classList.toggle('sel', S.selected.has(+el.dataset.id));
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
  bfSyncBtn();                                          // 记牌猜牌按钮（人机/热座可用）
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
    el.style.zIndex = idx + 1;   // 选中牌只弹起，不压到未选中牌前面
  });
}
// 视口尺寸变化（含移动端旋转）后重测一次，rAF 确保在样式应用后再测量
function relayoutHand() {
  requestAnimationFrame(() => { if (S.screen === 'game') layoutHand(); });
}

/* 在线模式的手牌与按钮（服务器权威：legal 白名单） */
function renderHandOnline(me) {
  const st = ONLINE.st || {};
  const handEl = $('hand');
  const hand = S.hands[me] || [];
  const sig = hand.map(c => c.i).join(',');
  if (handEl.dataset.sig !== sig) {
    handEl.innerHTML = hand.map(c => cardHTML(c, false)).join('');
    handEl.dataset.sig = sig;
    handEl.querySelectorAll('.card').forEach(el => {
      el.addEventListener('click', () => onCardClick(+el.dataset.id));
    });
  }
  handEl.querySelectorAll('.card').forEach(el => {
    el.classList.toggle('sel', S.selected.has(+el.dataset.id));
  });
  layoutHand();
  relayoutHand();
  const isMyTurn = myTurnHuman() === me;
  const selIds = [...S.selected].sort((a, b) => a - b).join(',');
  const playOK = isMyTurn && selIds && ONLINE.legalIds.some(l => l.slice().sort((a, b) => a - b).join(',') === selIds);
  $('btn-play').disabled = !playOK;
  $('btn-pass').disabled = !(isMyTurn && st.legalPass);
  $('btn-hint').classList.toggle('hidden', !isMyTurn);
  if (!isMyTurn) $('hint-bar').classList.add('hidden');
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
/* 首页展示当前 AI 版本（模型 + 开局搜索 + 回退告警） */
async function refreshAIVersion() {
  const el = $('ai-version');
  if (!el) return;
  if (isDeck15()) {
    el.classList.remove('warn');
    el.innerHTML = '🧪 <b>15张玩法</b>：当前为内置简易 AI ｜ 深度模型接口已预留（Pdk15AI.adapter），待接入 ｜ 在线对战暂不支持';
    return;
  }
  try {
    const r = await bridgeApi('/health', undefined, 6000);
    if (!r || !r.ok) throw new Error('down');
    const md5 = (r.assets && r.assets.files && r.assets.files['ckpt/policy_a2c_final56.pt']) === 'OK';
    const cfg = r.productionConfig || {};
    const probe = r.netProbe || {};
    const commit = r.pdkCommit || {};
    const isFallback = probe.net && probe.net.indexOf('final56') < 0;
    el.classList.toggle('warn', !!isFallback);
    el.innerHTML = 'AI 模型：<b>' + (r.productionModel || '未知') + '</b>' +
      (md5 ? ' <span class="ok">md5✓</span>' : ' <span class="bad">md5✗</span>') +
      (commit.hash ? ' ｜ 版本：<b>' + esc(commit.hash) + '</b>' +
        (commit.date ? ' <span class="dim">(' + esc(commit.date) + ')</span>' : '') : '') +
      ' ｜ 开局搜索：<b>' + (cfg.openingSearch ? '开' : '关') + '</b>' +
      (cfg.openingBudget != null ? '(≤' + cfg.openingBudget + 's)' : '') +
      ' ｜ 双模式：' + (r.modes || []).join('/') +
      ' ｜ 实际加载：<b>' + (probe.net || '未知') + '</b>' +
      (isFallback ? ' <span class="bad">⚠ 已回退旧网络</span>' : ' <span class="ok">在线</span>');
  } catch {
    el.classList.add('warn');
    el.innerHTML = 'AI 模型：<b>未连接</b> ⛔ AI 服务不可用 —— 按“不降级”策略，对局会暂停并提示重试（不会用内置 AI 代打）';
  }
}

/* 牌副规格（置顶选择）：15张=45张牌副新玩法；持久化到 localStorage */
function syncDeckSpecUI() {
  document.querySelectorAll('.decksz').forEach(el =>
    el.classList.toggle('active', +el.dataset.deck === S.deckSpec));
  $('deck15-hint').classList.toggle('hidden', S.deckSpec !== 15);
  $('slogan-line').textContent = S.deckSpec === 15
    ? '两人对战 · 45张牌副 · 各15张 · 黑桃2最大 · 黑桃3先出 · 有牌必打'
    : '两人对战 · 48张牌 · 黑桃2最大 · 黑桃3先出 · 有牌必打';
  $('btn-selftest').classList.toggle('hidden', S.deckSpec === 15);   // 自检修的是16张深度AI链路
  document.querySelectorAll('.rev-btn').forEach(b =>
    b.classList.toggle('hidden', S.deckSpec === 15));                // 服务端复盘仅覆盖16张局
  bfSyncBtn();
  refreshAIVersion();                                                // 版本行随牌副切换（15张显示"简易AI/接口预留"）
}

function initLobby() {
  try { S.deckSpec = +localStorage.getItem('pdk_deck_spec') === 15 ? 15 : 16; } catch {}
  syncDeckSpecUI();
  document.querySelectorAll('.decksz').forEach(el => {
    el.addEventListener('click', () => {
      const was15 = S.deckSpec === 15;
      S.deckSpec = +el.dataset.deck;
      try { localStorage.setItem('pdk_deck_spec', String(S.deckSpec)); } catch {}
      syncDeckSpecUI();
      if (S.deckSpec === 15 && !was15 && chosenMode === 'online') {
        toast('15张模式暂不支持在线对战，请选人机或热座', 2600);
        document.querySelector('.mode[data-mode="ai"]').dispatchEvent(new MouseEvent('click', { bubbles: true }));
      }
    });
  });
  document.querySelectorAll('.mode').forEach(el => {
    el.addEventListener('click', () => {
      if (S.deckSpec === 15 && el.dataset.mode === 'online') {
        toast('15张模式暂不支持在线对战（深度 AI 待接入），请选人机对战或双人热座', 3000);
        return;
      }
      document.querySelectorAll('.mode').forEach(x => x.classList.remove('active'));
      el.classList.add('active');
      chosenMode = el.dataset.mode;
      $('online-panel').classList.toggle('hidden', chosenMode !== 'online');
      $('btn-start').classList.toggle('hidden', chosenMode === 'online');
      if (chosenMode === 'online') {
        $('btn-history-lobby').classList.add('hidden');
        $('btn-start').classList.add('hidden');
      } else {
        $('btn-start').classList.remove('hidden');
        $('btn-history-lobby').classList.remove('hidden');
      }
    });
  });
  document.querySelector('.mode[data-mode="ai"]').classList.add('active');
  $('btn-start').addEventListener('click', () => { startMatch(); });
  $('btn-on-create').addEventListener('click', onlineCreate);
  $('btn-on-join').addEventListener('click', onlineJoin);
  $('on-code').addEventListener('keydown', e => { if (e.key === 'Enter') onlineJoin(); });
  $('btn-history-lobby').addEventListener('click', showReplayHistory);
  $('btn-selftest').addEventListener('click', () => runSelftest());
  $('btn-history-close').addEventListener('click', () => $('history-modal').classList.add('hidden'));
  $('hs-mine').addEventListener('click', () => showReplayHistory('mine'));
  $('hs-api').addEventListener('click', () => showReplayHistory('api'));
  $('btn-export-all').addEventListener('click', exportAllReplays);
  // 复盘查看器
  $('rv-first').addEventListener('click', () => { RV.step = 0; renderReview(); });
  $('rv-prev').addEventListener('click', () => reviewStep(-1));
  $('rv-next').addEventListener('click', () => reviewStep(1));
  $('rv-last').addEventListener('click', () => { RV.step = RV.total; renderReview(); });
  $('rv-comment-save').addEventListener('click', saveReviewComment);
  $('rv-explain').addEventListener('click', explainCurrentPly);
  $('rv-refresh').addEventListener('click', async () => {
    await openReview(RV.no);
    if (RV.total > 0) { RV.step = RV.total; renderReview(); }
  });
  $('btn-ai-retry').addEventListener('click', aiRetry);
  $('btn-review-cur').addEventListener('click', () => reviewCurrentGame());
  $('btn-review-prev').addEventListener('click', () => reviewPreviousGame());
  $('btn-review-close').addEventListener('click', () => {
    $('review-modal').classList.add('hidden');
    if (RV.from === 'game') { if (S.screen === 'game') render(); }   // 从对局页进入 -> 回到对局
    else showReplayHistory();
  });
}
function humanPlay() {
  const me = myTurnHuman();
  if (me < 0) return;
  if (S.mode === 'online') return void onlinePlay();
  const sel = S.hands[me].filter(c => S.selected.has(c.i));
  const v = validateSelection(me, sel);
  if (!v.ok) { toast(v.err); return; }
  applyPlay(me, sel);
}
async function onlinePlay() {
  const me = ONLINE.seat;
  const sel = S.hands[me].filter(c => S.selected.has(c.i));
  if (!sel.length) { toast('请先选牌'); return; }
  const ids = sel.map(c => c.i).sort((a, b) => a - b);
  const hit = ONLINE.legalIds.find(l => l.slice().sort((a, b) => a - b).join(',') === ids.join(','));
  if (!hit) { toast('不合法的出牌'); return; }
  if (await onlineAct(ids)) {
    S.selected = new Set();
  }
}
async function onlinePass() {
  if (await onlineAct([], true)) S.selected = new Set();
}
function humanPass() {
  const me = myTurnHuman();
  if (me < 0) return;
  if (S.mode === 'online') return void onlinePass();
  if (!S.last) { toast('先出牌，不能不出'); return; }
  const legal = legalPlays(S.hands[me], { last: S.last.combo, oppCount: S.hands[1 - me].length }, S.opts);
  if (legal.length) { toast('有牌必打：你能管上，不能不出'); render(); return; }
  applyPass(me);
}
async function showHint() {
  const me = myTurnHuman();
  if (me < 0 || !hasHintRight(me)) return;
  let p = null, src = '深度模型·' + (prodMode() === 'dual' ? '净分优先' : '胜率优先');
  const showWaiting = (txt) => {
    $('hint-bar').classList.remove('hidden');
    $('hint-text').textContent = txt;
    $('btn-adopt').classList.add('hidden');
  };
  if (isDeck15()) {
    // 15张模式：本地简易提示（不接桥；深度模型待接入）
    const seatCtx = {
      hand: S.hands[me].slice(), oppCount: S.hands[1 - me].length,
      last: S.last ? S.last.combo : null, opts: S.opts,
    };
    seatCtx.legal = legalPlays(seatCtx.hand, seatCtx, S.opts);
    const cards = pdk15Greedy(seatCtx);
    src = '简易提示·15张';
    if (!cards) {
      $('btn-adopt').classList.add('hidden');           // 15张不接深度模型（接口预留 Pdk15AI.adapter）
      $('hint-bar').classList.remove('hidden');
      $('hint-text').innerHTML = '建议<b>[' + src + ']</b>：不出（当前无合法压制）';
      return;
    }
    const combo = analyzeShape(cards);
    S.hints = [{ cards, combo }]; S.hintIdx = 0;
    S.selected = new Set(cards.map(c => c.i));
    $('btn-adopt').classList.add('hidden');
    const hb2 = $('hint-bar');
    hb2.classList.remove('hidden');
    $('hint-text').innerHTML = '建议<b>[' + src + ']</b> ' + comboName(combo) + '：<span class="hint-cards">' +
      cards.map(c => cardText(c)).join(' ') + '</span>';
    render();
    return;
  }
  if (S.mode === 'ai') {
    // 先手开局时桥可能仍在初始化（跨海约1~2秒）：等它完成再决策
    for (let i = 0; i < 60 && bridge.initing; i++) await new Promise(r => setTimeout(r, 200));
    if (bridge.lastMirror) { try { await bridge.lastMirror; } catch {} }   // 等最后一手镜像落库，避免重放缺手
    showWaiting('🤖 深度模型思考中…（残局求解可能需要几秒）');
    p = await bridgeSuggestForTurn();                // 只走生产模型（不降级）
    $('btn-adopt').classList.remove('hidden');
  } else {
    // 热座（玩家一）/ 在线（双方）：pdk-ai 生产内核无状态 decide
    showWaiting('🤖 深度模型思考中…（残局求解可能需要几秒）');
    p = await decideHint(me);
    $('btn-adopt').classList.remove('hidden');
  }
  if (p && p.pass) {                                   // 深度建议：不出（当前被压且无解）
    S.selected = new Set(); S.hints = []; S.hintIdx = -1;
    $('btn-adopt').classList.add('hidden');
    render();                                          // render 会隐藏提示条（无 S.hints）-> 之后再显示文字
    $('hint-bar').classList.remove('hidden');
    $('hint-text').innerHTML = '建议<b>[' + src + ']</b>：不出（当前无合法压制）';
    return;
  }
  if (!p || p.error) {                                 // 不降级：直接报错，不提供内置建议
    $('btn-adopt').classList.add('hidden');
    S.selected = new Set(); render();                   // 同上：先 render 再显示报错，避免被隐藏
    $('hint-bar').classList.remove('hidden');
    $('hint-text').innerHTML = '<b>AI 提示不可用</b>：' + String((p && p.error) || '未返回建议').replace(/[<>]/g, '');
    return;
  }
  S.hints = [p]; S.hintIdx = 0;
  S.selected = new Set(p.cards.map(c => c.i));
  const hb = $('hint-bar');
  hb.classList.remove('hidden');
  $('hint-text').innerHTML = '建议<b>[' + src + ']</b> ' + comboName(p.combo) + '：<span class="hint-cards">' +
    p.cards.map(c => cardText(c)).join(' ') + '</span>' +
    '';
  render();
}
function quitToLobby() {
  clearTimeout(S.aiTimer);
  if (bridgeRecoverTimer) { clearInterval(bridgeRecoverTimer); bridgeRecoverTimer = null; }
  onlineStopPoll();
  if (S.mode === 'online') onlineClearSaved();
  S.phase = 'idle'; S.awaiting = null;
  if (S.mode === 'online') S.mode = 'ai';
  hideModal();
  $('handover').classList.add('hidden');
  $('hint-bar').classList.add('hidden');
  if (typeof bfClose === 'function') bfClose();
  showScreen('lobby');
}

/* ================= 记牌猜牌面板（AI 心眼） =================
 * 数据源：桥接 /api/belief（上游 pdk-ai 8db4fb79 起提供）
 *   人机对战（16 张，有会话）：{"sid","perspective":"ai"|"me"}
 *     ai = "AI 看我"（它眼里的你：台账 + 它认为你可能握什么 + 它认出哪些领出必压不住）
 *     me = "我看 AI"（以你为视角推 AI 的手牌；只用公开信息，不看对手底牌）
 *   双人热座（无会话）：无状态口径 {"my_hand","opp_n","trick","history"}（当前出牌方视角）
 * 不降级：拿不到就显示错误 + rid，绝不编造概率。
 */
const BF = { open: false, persp: 'ai', cache: {}, loading: false, err: '', rid: '', ply: -1, reqSeq: 0 };
const BF_RANK_ORDER = ['2', 'A', 'K', 'Q', 'J', 'X', '9', '8', '7', '6', '5', '4', '3'];
const BF_RANK_LABEL = { X: '10' };
const BF_COPIES = { 2: 1, A: 3 };                       // 其余点数各 4 张（48 张副）
const BF_FACT_ICON = {
  lead_rule1: '📐', lead_rule2: '📐', lead_rule3: '📐',
  pass: '🚫', gap: '✂️', tell: '🗣️', follow: '↩️',
};

function bfRankKey(r) { return BF_RANK_LABEL[r] || r; }

/* 面板在哪些场景可用：人机(16张，深度模型会话) / 热座(无状态，当前出牌方视角) */
function beliefAvailable() {
  if (isDeck15()) return false;
  if (S.mode === 'ai') return true;                     // 桥未就绪时点击会给出明确报错
  if (S.mode === 'hotseat') return S.phase === 'playing' || S.phase === 'roundEnd';
  return false;
}
function bfSyncBtn() {
  const el = $('btn-belief');
  if (el) el.classList.toggle('hidden', !beliefAvailable());
}
function bfPerspSeat(persp) { return persp === 'ai' ? 1 : 0; }

/* 热座：把当前局面转成无状态 belief 请求（视角 = 当前出牌方） */
function bfStatelessPayload() {
  const seat = (S.awaiting != null) ? S.awaiting : S.turn;
  const oppSeat = 1 - seat;
  const trick = (S.last && S.last.by === oppSeat && S.last.combo) ? comboToTrick(S.last.combo) : null;
  const history = (S.roundMoves || []).map(m => ({
    seat: m.seat === seat ? 0 : 1,
    move: (m.cards || []).slice(),
    pass_on: m.pass_on || null,
  }));
  return { seat, payload: { my_hand: S.hands[seat].map(c => c.i).sort((a, b) => a - b),
                            opp_n: S.hands[oppSeat].length, trick, history } };
}

async function bfCall(persp) {
  const post = (body) => bridgeApi('/api/belief', body, 12000);   // 正常 <1s；超时=AI 在算牌，不要拖 30s
  const build = () => {
    if (S.mode === 'ai') return { sid: bridge.sid, perspective: persp };
    return bfStatelessPayload().payload;            // 热座：按当前局面重建（重试时状态可能已变）
  };
  if (S.mode === 'ai') {
    for (let i = 0; i < 40 && bridge.initing; i++) await new Promise(r => setTimeout(r, 200));
    if (!bridge.ready || !bridge.sid) throw new Error(bridge.lastErr || 'AI 服务未就绪（深度模型会话未建立）');
  }
  // 链路抖动（空响应/非 JSON/5xx/超时）自动重试 1 次；语义错误（400 等）不重试
  let last = null;
  for (let attempt = 0; attempt < 2; attempt++) {
    try { return await post(build()); }
    catch (e) {
      last = e;
      // 只重试"空响应/非 JSON/5xx"这类链路抖动；超时说明 AI 正在算牌，重试也是白等
      const retryable = (e && !e.timedOut && (e.transport || (e.status >= 500)));
      if (!retryable || attempt === 1) throw e;
      await new Promise(r => setTimeout(r, 1200));
    }
  }
  throw last;
}

function bfOpen() {
  if (!beliefAvailable()) return;
  BF.open = true;
  BF.persp = (S.mode === 'ai') ? BF.persp : 'me';       // 热座只有一个视角
  const sheet = $('belief-sheet');
  sheet.classList.remove('hidden');
  $('belief-tabs').classList.toggle('hidden', S.mode !== 'ai');
  document.querySelectorAll('#belief-tabs .seg-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.persp === BF.persp));
  requestAnimationFrame(() => sheet.classList.add('show'));
  bfRefresh(true);
  bfSyncBtn();
}
function bfClose() {
  const sheet = $('belief-sheet');
  if (!sheet || sheet.classList.contains('hidden')) return;
  sheet.classList.remove('show');
  const card = $('belief-card');
  if (card) card.style.transform = '';
  setTimeout(() => { sheet.classList.add('hidden'); BF.open = false; }, 240);
}
function bfKey() { return BF.persp + '#' + (S.roundMoves || []).length + '#' + (S.hands || []).map(h => h.length).join('-'); }

async function bfRefresh(force) {
  if (!BF.open) return;
  const key = bfKey();
  $('belief-tabs').classList.toggle('hidden', S.mode !== 'ai');
  document.querySelectorAll('#belief-tabs .seg-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.persp === BF.persp));
  if (!force && BF.cache[key]) { BF.loading = false; bfRender(BF.cache[key]); return; }
  BF.loading = true; BF.err = ''; BF.keep = !force && !!BF.cache[key];
  bfRender(null);
  const seq = ++BF.reqSeq;
  try {
    const d = await bfCall(BF.persp);
    if (seq !== BF.reqSeq || !BF.open) return;          // 期间又换了视角/关了面板 -> 丢弃
    if (!d || d.error) throw new Error((d && d.error) || '未返回数据');
    BF.cache[key] = d; BF.rid = d.rid || ''; BF.err = '';
    BF.loading = false;                                 // 必须先复位：bfRender 用它判断骨架屏
    bfRender(d);
  } catch (e) {
    if (seq !== BF.reqSeq || !BF.open) return;
    BF.err = String((e && e.message) || e); BF.rid = (e && e.rid) || '';
    BF.loading = false;
    bfRender(null);
  }
}
function bfOnPly() {                                    // 每手之后：面板开着就自动刷新
  if (!BF.open) return;
  setTimeout(() => { if (BF.open) bfRefresh(false); }, 350);
}

function bfMetaHTML(d) {
  if (!d) return '';
  const bits = [];
  const w = Number(d.worlds || 0);
  const v = d.view || {};
  if (v.ply != null) bits.push('第 <b>' + v.ply + '</b> 手');
  bits.push('可能构成 <b>' + bfNum(w) + '</b> 种');
  if (d.exhaustive) bits.push('<span class="bf-ok">全量枚举</span>');
  bits.push(d.weighted ? '加权' : '未加权');
  if (d.sharpness != null) bits.push('尖度 ' + Number(d.sharpness).toFixed(4));
  let s = bits.join(' · ');
  const warn = [];
  if (w > 50000) warn.push('信息不足（概率接近均匀，属正常）');
  const lk = d.lock || {};
  if (lk.worlds_capped) warn.push('同型牌锁牌核查已跳过（构成数过大）');
  if (lk.leads_skipped) warn.push('领出锁牌认证已跳过（构成数过大）');
  if (d.incomplete_snapshot) warn.push('快照不完整（动作史有缺）');
  if (warn.length) s += '<div class="warn">⚠ ' + warn.join('；') + '</div>';
  return s;
}
function bfPct(p) {
  const v = Number(p || 0) * 100;
  if (v < 0.01) return '&lt;0.01%';          // 桥里 round(p,5) 会把极小概率舍成 0：一律按"小于 0.01%"如实显示
  return v.toFixed(2) + '%';
}
function bfNum(n) {
  if (n >= 10000) return (n / 10000).toFixed(1) + ' 万';
  return String(n);
}

function bfRender(d) {
  const body = $('belief-body');
  if (!body) return;
  $('belief-title').textContent = '🧠 记牌猜牌';
  const perspName = BF.persp === 'ai' ? 'AI 看我' : '我看 AI';
  if (BF.loading) {
    $('belief-meta').innerHTML = perspName + ' · 🤖 正在算记牌与猜牌…';
    body.innerHTML = '<div class="bf-skel" style="width:70%"></div><div class="bf-skel" style="width:90%"></div>' +
                     '<div class="bf-skel" style="width:55%"></div><div class="bf-skel" style="width:80%"></div>';
    return;
  }
  if (BF.err) {
    $('belief-meta').innerHTML = perspName;
    body.innerHTML = '<div class="bf-err">⚠ 记牌猜牌不可用：' + bfEsc(BF.err) +
      (BF.rid ? '<br><span class="dim">rid: ' + bfEsc(BF.rid) + '</span>' : '') +
      '<br><button class="btn primary small" id="bf-retry">重试</button></div>';
    const btn = $('bf-retry');
    if (btn) btn.addEventListener('click', () => bfRefresh(true));
    return;
  }
  if (!d) { $('belief-meta').innerHTML = ''; body.innerHTML = ''; return; }
  const seat = BF.persp === 'me' ? '我' : 'AI';
  const who = BF.persp === 'me' ? 'AI 的手牌' : '你的手牌';
  $('belief-meta').innerHTML = perspName + ' · ' + bfMetaHTML(d);
  const out = [];
  out.push(bfSecProb(d, who, seat));
  out.push(bfCandidatesHTML(d));        // 对手剩<=2 张：候选点数（有才显示）
  out.push(bfSecPatterns(d, seat));     // 新：对手牌型概率分布
  out.push(bfSecInfer(d, seat));        // 推理链（无 inferences 时退回 facts）
  out.push(bfSecControls(d));           // 新：牌型归属（谁手里有当前最大）
  out.push(bfSecLedger(d, seat));
  out.push(bfSecWorlds(d));             // 新：逐世界推演（折叠）
  out.push(bfSecLock(d, seat));
  body.innerHTML = out.join('');
}

/* ① 对手手牌概率：top_hands + rank_prob */
/* 概率排序（用户要求：不要固定 2>A>K… 顺序）：
 * 以"对手持有该点数的概率"降序为准；概率先取整到 1%，避免每手微小波动让格子乱跳；
 * 同概率时未见张数多的优先，最后按传统点数大小稳定收尾。 */
function bfRanksByProb(d, led) {
  const rp = d.rank_prob || {};
  const bucket = r => Math.round((Number(rp[r]) || 0) * 100);
  const seen = r => (led ? Number(led[r] || 0) : 0);
  return BF_RANK_ORDER.slice().sort((a, b) =>
    (bucket(b) - bucket(a)) ||
    (seen(b) - seen(a)) ||
    (BF_RANK_ORDER.indexOf(a) - BF_RANK_ORDER.indexOf(b)));
}

/* 张数分布（上游 8325d96 的 rank_cnt_p）：把"有没有"细化成"几张"。
 * 只展示份额 >=2% 的档（或总共只有两档时全展示），众数加粗，避免手机上太碎。 */
function bfCountLine(d, r) {
  const cp = (d.rank_cnt_p || {})[r];
  if (!cp) return '';
  const items = Object.keys(cp).map(k => ({ k: +k, p: +cp[k] }))
    .sort((a, b) => a.k - b.k)
    .filter(x => x.p >= 0.02 || Object.keys(cp).length <= 2);
  if (!items.length) return '';
  const mode = items.reduce((a, b) => (b.p > a.p ? b : a));
  const title = Object.keys(cp).sort((a, b) => +a - +b)
    .map(k => (k === '0' ? '没有' : k + ' 张') + ' ' + Math.round(cp[k] * 1000) / 10 + '%').join('，');
  return '<div class="bf-cnt" title="' + bfEsc(title) + '">' + items.map(x =>
    '<span class="' + (x.k === mode.k ? 'on' : '') + '">' +
    (x.k === 0 ? '没有' : x.k + '张') + ' ' + Math.round(x.p * 100) + '%</span>').join('') +
    '</div>';
}
/* 最可能的一手牌（opp_most_likely = 权重最高那一手的点数→张数） */


/* 把一手牌渲染成可读的"几张什么牌"卡片：{K:2,8:1,7:1} -> K×2 · 8 · 7 */
function bfHandChips(ranks, dim) {
  const ks = Object.keys(ranks || {}).sort((a, b) =>
    (ranks[b] - ranks[a]) || (BF_RANK_ORDER.indexOf(a) - BF_RANK_ORDER.indexOf(b)));
  if (!ks.length) return '<span class="dim">（空）</span>';
  return '<span class="chips">' + ks.map(k =>
    '<span class="bf-chip2' + (dim ? ' off' : '') + '">' + bfEsc(k) +
    (ranks[k] > 1 ? '<em>×' + ranks[k] + '</em>' : '') + '</span>').join('') + '</span>';
}

function bfSecProb(d, who, seat) {
  const cp = d.rank_cnt_p || {}, rp = d.rank_prob || {};
  const modeOf = r => {
    const dist = cp[r];
    if (!dist) return null;
    let best = null;
    Object.keys(dist).forEach(k => {
      const v = +dist[k];
      if (!best || v > best.p || (v === best.p && +k > best.k)) best = { k: +k, p: v };
    });
    return best;
  };
  const entries = BF_RANK_ORDER.map(r => ({ r, m: modeOf(r) }))
    .filter(x => x.m || (rp[x.r] || 0) > 0.0005);
  const distText = r => Object.keys(cp[r] || {}).sort((a, b) => +a - +b)
    .map(k => (k === '0' ? '没有' : k + ' 张') + ' ' + Math.round(cp[r][k] * 100) + '%').join(' · ');
  // ① 最可能的几手：直接回答"有几张什么牌"（按概率排序）
  const hands = (d.top_hands || []).slice()
    .sort((a, b) => Number(b.p || 0) - Number(a.p || 0)).slice(0, 6);
  const pMax = Number((hands[0] || {}).p || 0);
  const tops = hands.map((h, i) => {
    const p = Number(h.p || 0);
    return '<div class="bf-hand"><span class="k">' + (i + 1) + '</span>' +
      bfHandChips(h.ranks || {}) +
      '<span class="v">' + (p >= 0.001 ? (p * 100).toFixed(2) + '%' : '&lt;0.1%') + '</span></div>';
  }).join('');
  const topsTitle = pMax >= 0.005
    ? '最可能的几手（按概率排序，读作"几张什么牌"）'
    : '最可能的几手（按概率排序；开局信息少，单种概率天然很低，看下面的点数张数更实在）';
  // ② 点数张数：只列"他较可能拿着"的点数（P(≥1) >= 25%），按张数分组；
  // 其余点数一行带过 —— 否则每人天然众数=0，会堆成一长串"没有"，看着像废话。
  const TH = 0.25;
  // 这一块只回答"他若拿着，多半几张"：条件众数（在 k>=1 内取最大），组头 = 张数，
  // chip 上的百分数 = 他持有该点数的概率 P(>=1)。这样不会再出现"较可能拿着"里写"没有"的矛盾。
  const likely = entries.filter(x => (rp[x.r] || 0) >= TH).map(x => {
    const dist = cp[x.r] || {};
    let best = null;
    Object.keys(dist).forEach(k => {
      const kk = +k;
      if (kk < 1) return;
      const cpk = +dist[k] / Math.max(1e-9, rp[x.r]);
      if (!best || cpk > best.cp || (cpk === best.cp && kk > best.k)) best = { k: kk, cp: cpk, raw: +dist[k] };
    });
    return { r: x.r, k: best ? best.k : 1, cp: best ? best.cp : 0, raw: best ? best.raw : 0,
             p: rp[x.r] || 0, dist };
  });
  const rest = entries.filter(x => (rp[x.r] || 0) < TH)
    .sort((a, b) => (rp[b.r] || 0) - (rp[a.r] || 0));
  const byCount = {};
  likely.forEach(x => { (byCount[x.k] = byCount[x.k] || []).push(x); });
  const chip = x => '<span class="bf-chip2" title="' +
    bfEsc('持有概率 ' + (x.p * 100).toFixed(1) + '%；若持有，最可能 ' + x.k + ' 张（' +
          (x.cp * 100).toFixed(0) + '%，即 ' + (x.raw * 100).toFixed(1) + '%）' +
          '；完整分布：' + (distText(x.r) || '—')) + '">' + bfEsc(x.r) +
    '<em>' + Math.round(x.p * 100) + '%</em></span>';
  const restLine = rest.length
    ? '<p class="bf-note" style="margin-top:6px">其余 ' + rest.length + ' 个点数他大概率都没有' +
      '（最高 ' + ((rp[rest[0].r] || 0) * 100).toFixed(0) + '%：' +
      rest.slice(0, 6).map(x => bfEsc(x.r)).join(' ') + '…）</p>'
    : '';
  const lines = Object.keys(byCount).map(Number).sort((a, b) => b - a).map(k => {
    const items = byCount[k].sort((a, b) => (b.p - a.p)).map(chip).join('');
    return '<div class="bf-grp"><span class="lb"><b>' + k + ' 张</b></span>' +
      '<span class="chips">' + items + '</span></div>';
  }).join('');
  // ③ 明细：每个点数的完整张数分布（默认收起）
  const detail = entries.slice().sort((a, b) => BF_RANK_ORDER.indexOf(a.r) - BF_RANK_ORDER.indexOf(b.r))
    .map(x => '<div class="bf-cnt"><span class="rk">' + bfEsc(x.r) + '</span>' +
      bfEsc(distText(x.r) || '信息不足') + '</div>').join('');
  return '<details class="bf-sec" open><summary>🃏 ' + who + '大概是什么' +
    '<span class="tag">' + (d.opp_n != null ? '对手剩 ' + d.opp_n + ' 张' : '') + '</span>' +
    '<span class="chev">▶</span></summary><div class="bf-inner">' +
    (tops ? '<p class="bf-note">' + topsTitle + '</p>' + tops : '') +
    '<p class="bf-note" style="margin-top:10px"><b>点数张数</b>：只列他有<b>可能拿着</b>的点数' +
    '（持有概率 ≥25%）；组头 = 他若拿着最可能几张，chip 小字 = 他持有该点数的概率。' +
    '张数多的组在前（"2 张/3 张"就是可能成对/成三的点数）。</p>' +
    (lines || '<p class="bf-note">暂无——他对每个点数都不太可能有牌（信息太散）。</p>') + restLine +
    '<details class="bf-sub"><summary>📋 每个点数的完整张数分布（0/1/2/3/4 各多少概率）</summary>' +
    '<div class="bf-subbody">' + detail + '</div></details>' +
    '</div></details>';
}

/* ② 记牌台账：各点数还没露面的张数 */
function bfSecLedger(d, seat) {
  const led = d.ledger || {}, rp = d.rank_prob || {};
  const cells = bfRanksByProb(d, led).map(r => {      // 同样概率降序（已断张自动沉底）
    const n = Number(led[r] || 0);
    const cap = BF_COPIES[r] || 4;
    const p = Number(rp[r] || 0);
    let dots = '';
    for (let i = 0; i < cap; i++) dots += '<i class="' + (i < n ? 'on' : '') + '"></i>';
    return '<div class="bf-cell' + (n === 0 ? ' zero' : '') + '">' +
      '<div class="r">' + bfEsc(r) + '</div><div class="n">' + n + '/' + cap + '</div>' +
      '<div class="dots">' + dots + '</div>' +
      (p > 0.0005 ? '<div class="p">' + Math.round(p * 100) + '%</div>' : '') + '</div>';
  }).join('');
  const total = Object.values(led).reduce((a, b) => a + Number(b || 0), 0);
  return '<details class="bf-sec" open><summary>🧮 记牌台账' +
    '<span class="tag">未见 ' + total + ' 张</span><span class="chev">▶</span></summary>' +
    '<div class="bf-inner"><p class="bf-note">各点数“还没露面”的张数（牌堆总量 − 我手牌 − 双方已出），' +
    '点亮的圆点 = 还剩几张。<b>格子按概率从高到低排</b>：下方百分比 = 对手持有该点数的概率；' +
    '已经断掉的点数（0/N）自动沉到最后。</p>' +
    '<div class="bf-grid">' + cells + '</div></div></details>';
}

/* ③ 推断链：facts（每条带实测命中率） */
/* ===== 上游 e037b99 新增字段的前端展示 ===== */

/* 推理链（inferences）：比 facts 更完整——L1 台账确定 / L2 过牌硬推理 / B1-B7 行为层(带置信度) */
const BF_INF_BADGE = {
  L1: { t: '台账·确定', cls: 'ok' }, L2: { t: '过牌·确定', cls: 'ok' },
  B1: { t: '行为', cls: '' }, B2: { t: '行为', cls: '' }, B3: { t: '行为', cls: '' },
  B4: { t: '行为', cls: '' }, B5: { t: '行为', cls: '' }, B6: { t: '行为', cls: 'warn' },
  B7: { t: '行为', cls: '' },
};

function bfSecInfer(d, seat) {
  const inf = d.inferences || [];
  const whoTxt = BF.persp === 'me' ? 'AI' : '你';
  if (!inf.length) return bfSecFacts(d, seat);        // 老数据/无推理链时退回 facts
  const items = inf.map(f => {
    const kind = String(f.kind || '');
    const bd = BF_INF_BADGE[kind] || { t: kind || '推理', cls: '' };
    const conf = f.conf == null ? null : Number(f.conf);
    const sure = (bd.cls === 'ok');                 // L1/L2 = 确定类，只需一个标签
    const confPill = sure ? ''
      : (conf != null ? '<span class="bf-pill' + (conf < 0.8 ? ' low' : '') + '">置信 ' + Math.round(conf * 100) + '%</span>' : '');
    return '<div class="bf-fact"><span class="ico">' + bfEsc(kind || '·') + '</span><span class="txt">' +
      bfEsc(String(f.text || '')) +
      '<span class="bf-pill ' + (bd.cls === 'ok' ? '' : 'rule') + '">' + bfEsc(bd.t) + '</span>' + confPill +
      '</span></div>';
  }).join('');
  return '<details class="bf-sec" open><summary>🔍 推理链' +
    '<span class="tag">' + inf.length + ' 条</span><span class="chev">▶</span></summary>' +
    '<div class="bf-inner"><p class="bf-note">模型从<b>' + whoTxt + '</b>的动作推出的完整链条：' +
    '<b>L1</b> 台账确定、<b>L2</b> 过牌硬推理（这两类为确定结论），<b>B1-B7</b> 行为层（带置信度，可逐条校验）。</p>' +
    items + '</div></details>';
}

/* 对手牌型概率（opp_patterns）：他"有没有这类牌型 / 能不能压我 / 最大可能到几" */
function bfSecPatterns(d, seat) {
  const ps = d.opp_patterns;
  const whoTxt = BF.persp === 'me' ? 'AI' : '你';
  if (!Array.isArray(ps) || !ps.length) return '';
  const rows = ps.filter(p => Number(p.p_has || 0) > 0.005).slice(0, 12).map(p => {
    const has = Number(p.p_has || 0), beat = Number(p.p_beats_me || 0);
    const mx = p.max_p50 && p.max_p50 !== null ? String(p.max_p50) : '—';
    const mx90 = p.max_p90 && p.max_p90 !== null ? String(p.max_p90) : '—';
    return '<div class="bf-row" title="' + bfEsc('他持有该牌型的概率 ' + (has * 100).toFixed(1) +
      '%；能压住我当前最大该牌型的概率 ' + (beat * 100).toFixed(1) + '%') + '">' +
      '<span class="k" style="width:52px">' + bfEsc(String(p.pattern || '')) + '</span>' +
      '<span class="bf-bar"><i style="width:' + Math.max(3, Math.round(has * 100)) + '%"></i></span>' +
      '<span class="v">' + (has * 100).toFixed(0) + '%</span>' +
      '<span class="v2" title="他最大可能到几（中位/9 成）">' + bfEsc(mx) + (mx90 !== mx ? '~' + bfEsc(mx90) : '') + '</span></div>' +
      (beat > 0.01 ? '<div class="bf-cnt">能压住我的概率 ' + (beat * 100).toFixed(0) + '%</div>' : '');
  }).join('');
  return '<details class="bf-sec"' + (rows ? ' open' : '') + '><summary>🀄 对手牌型概率' +
    '<span class="tag">' + ps.length + ' 类</span><span class="chev">▶</span></summary>' +
    '<div class="bf-inner"><p class="bf-note">按牌型预演：<b>他手里有没有这类牌型</b>（第一行百分比）、' +
    '他最大可能到几点（右侧「中位~九成」），以及<b>他能压住我</b>的概率。' +
    '按"有"的概率从高到低。</p>' + (rows || '<p class="bf-note">暂无</p>') + '</div></details>';
}

/* 牌型归属（controls）：当前最大单张/对子/三条/连对/顺子在谁手里 */
function bfSecControls(d) {
  const cs = d.controls;
  if (!Array.isArray(cs) || !cs.length) return '';
  const rows = cs.slice(0, 14).map(c => {
    const mine = !!c.i_hold_max;
    return '<div class="bf-ctl' + (mine ? ' ok' : '') + '">' +
      '<span class="p">' + bfEsc(String(c.pattern || '')) + '</span>' +
      '<span class="me">' + (mine ? '✅ 最大在我手里' : '⚠ 最大不在我手里') +
      (c.my_max ? '（我 ' + bfEsc(String(c.my_max)) + '）' : '') + '</span>' +
      '<span class="opp">' + (c.opp_possible_max ? '他最多到 ' + bfEsc(String(c.opp_possible_max)) : '他不可能有') + '</span></div>';
  }).join('');
  return '<details class="bf-sec" open><summary>🧩 牌型归属' +
    '<span class="tag">' + cs.filter(c => c.i_hold_max).length + '/' + cs.length + ' 在我手里</span>' +
    '<span class="chev">▶</span></summary><div class="bf-inner">' +
    '<p class="bf-note">"当前最大的那张/那对"到底在谁手里 —— 决定你这手能不能放心领出。' +
    '（结构计算，不是采样。）</p>' + rows + '</div></details>';
}

/* 残局候选点数（candidates）：对手剩 ≤2 张时 */
function bfCandidatesHTML(d) {
  const cs = d.candidates;
  if (!Array.isArray(cs) || !cs.length) return '';
  const txt = cs.slice(0, 8).map(c =>
    '<span class="bf-chip2">' + bfEsc(String(c.rank)) + '<em>未见' + c.unseen + '</em></span>').join('');
  return '<div class="bf-most">🎯 他只剩 ' + (d.opp_n != null ? d.opp_n : '?') +
    ' 张，候选点数：<span class="chips">' + txt + '</span></div>';
}

/* 逐世界推演（worlds_detail）：每候选锁率/连走/夺回 + 前 N 个世界里"他能否压住" */
function bfSecWorlds(d) {
  const wd = d.worlds_detail;
  if (!wd || wd.error || (!wd.cands || !wd.cands.length)) return '';
  const cands = (wd.cands || []).slice(0, 6);
  const candRows = cands.map(c =>
    '<div class="bf-row" title="' + bfEsc('锁率=对手压不住的概率；连走=领出后还能连续走几手；夺回=被压后能夺回的手数') + '">' +
    '<span class="k" style="width:64px;font-family:Georgia,serif">' + bfEsc(String(c.move || '')) + '</span>' +
    '<span class="bf-bar"><i style="width:' + Math.max(3, Math.round(Number(c.p_lock || 0) * 100)) + '%"></i></span>' +
    '<span class="v">锁' + (c.p_lock == null ? '—' : Math.round(c.p_lock * 100) + '%') + '</span>' +
    '<span class="v2">连' + (c.chain == null ? '—' : c.chain) + ' 夺' + (c.reclaim == null ? '—' : c.reclaim) + '</span></div>'
  ).join('');
  const moves = cands.map(c => String(c.move || ''));
  const worlds = (wd.worlds || []).slice(0, 16);
  const head = '<tr><th>世界</th><th>他手牌</th>' + moves.map(m => '<th>' + bfEsc(m) + '</th>').join('') + '</tr>';
  const body = worlds.map(w =>
    '<tr><td>' + (w.i + 1) + '</td><td class="cards">' + bfEsc(String(w.cards || '')) + '</td>' +
    moves.map(m => {
      const can = Number((w.can_beat || {})[m] || 0);
      return '<td class="' + (can ? 'yes' : 'no') + '">' + (can ? '✓' : '✗') + '</td>';
    }).join('') + '</tr>').join('');
  return '<details class="bf-sec"><summary>🌍 逐世界推演' +
    '<span class="tag">' + (wd.n || 0) + ' 种' + (wd.exhaustive ? '·全量' : '·采样') +
    (wd.shown ? '，列 ' + wd.shown : '') + '</span><span class="chev">▶</span></summary>' +
    '<div class="bf-inner">' +
    '<p class="bf-note">候选出法逐条画像：<b>锁率</b>=对手压不住的概率、<b>连走</b>=之后能连走几手、' +
    '<b>夺回</b>=被压后能夺回的手数。下表是前 ' + worlds.length + ' 个世界：他的具体手牌 + 能否压住每个候选。</p>' +
    candRows +
    '<div class="bf-wtable"><table>' + head + body + '</table></div>' +
    '</div></details>';
}

function bfSecFacts(d, seat) {
  const facts = d.facts || [];
  const whoTxt = BF.persp === 'me' ? 'AI' : '你';
  const items = facts.map(f => {
    const text = String(f.text || '');
    const m = text.match(/实测命中\s*(\d+)%/);
    let main = text;
    let pill = '';
    if (m) {
      const v = +m[1];
      main = text.replace(/\s*\(?实测命中\s*\d+%\)?/, '').trim();
      pill = '<span class="bf-pill' + (v < 80 ? ' low' : '') + '">命中 ' + v + '%</span>';
    }
    const rule = text.match(/用户规则([①②③④])/);
    if (rule) {
      main = main.replace(/\s*\(?用户规则[①②③④][^)]*\)?/, '').trim();
      pill += '<span class="bf-pill rule">规则' + rule[1] + '</span>';
    }
    const icon = BF_ICON_SAFE(f.kind);
    return '<div class="bf-fact"><span class="ico">' + icon + '</span><span class="txt">' +
      bfEsc(main) + pill + '</span></div>';
  }).join('');
  return '<details class="bf-sec"' + (facts.length ? ' open' : '') + '><summary>🔍 推断链' +
    '<span class="tag">' + facts.length + ' 条</span><span class="chev">▶</span></summary>' +
    '<div class="bf-inner"><p class="bf-note">这些是模型从' + whoTxt + '的出牌动作推出的事实，' +
    '每条都带实测命中率（可逐条校验该不该信）。</p>' +
    (items || '<p class="bf-note">暂无推断（动作还不够多）</p>') + '</div></details>';
}
function BF_ICON_SAFE(kind) { return BF_FACT_ICON[kind] || '•'; }

/* ④ 锁牌认证 */
function bfSecLock(d, seat) {
  const lk = d.lock || {};
  const vt = lk.vs_trick;
  const isAI = BF.persp === 'ai';
  const oppTxt = isAI ? '你' : 'AI';
  let html = '';
  if (vt && vt.p_beat != null) {
    // 语义说明：p_beat = 在"对手手牌"的全部可能世界里，能压住该同型牌型的权重占比。
    // 所以这里说"同型牌核查"，而不是"你能不能压住自己刚出的牌"。
    html += '<p class="bf-note">同型牌核查：' + oppTxt + '手里还能压住这手同型牌的概率 <b>' +
      (vt.p_beat * 100).toFixed(2) + '%</b>' +
      (vt.certified_locked ? ' <span class="bf-ok">已认证：同型牌必压不住（保牌权）</span>' : '') + '</p>';
  } else if (vt && vt.skipped) {
    html += '<p class="bf-note">' + bfEsc(vt.skipped) + '</p>';
  } else {
    html += '<p class="bf-note">当前无需跟牌（或不在' + seat + '的回合）。</p>';
  }
  const leads = lk.leads || [];
  if (leads.length) {
    html += '<p class="bf-note" style="margin-top:8px">被认证“对方必压不住”的领出（按点数，同点任意花色）：</p><div class="bf-chips">' +
      leads.map(l => '<span class="bf-chip">' + bfEsc(l.cards || '') + '</span>').join('') + '</div>';
  } else if (lk.leads_skipped) {
    html += '<p class="bf-note" style="margin-top:8px">' + bfEsc(lk.leads_skipped) + '</p>';
  } else {
    html += '<p class="bf-note" style="margin-top:8px">暂无被认证的领出（残局构成数很小时才会严格认证）。</p>';
  }
  return '<details class="bf-sec"' + (leads.length || (vt && vt.certified_locked) ? ' open' : '') +
    '><summary>🔒 锁牌认证<span class="tag">' + leads.length + ' 手</span>' +
    '<span class="chev">▶</span></summary><div class="bf-inner">' +
    '<p class="bf-note">“锁牌”= 在全部可能手牌上严格枚举后，对方没有任何牌能压住 —— 所以敢连着领出保牌权。</p>' +
    html + '</div></details>';
}

function bfEsc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

/* 抽屉交互：下拉关闭 / 遮罩关闭 / ESC / 切视角 / 刷新 */
function bfInitSheet() {
  const sheet = $('belief-sheet'), card = $('belief-card'), grip = $('belief-grip');
  if (!sheet) return;
  $('btn-belief').addEventListener('click', bfOpen);
  $('belief-close').addEventListener('click', bfClose);
  $('belief-mask').addEventListener('click', bfClose);
  $('belief-refresh').addEventListener('click', () => bfRefresh(true));
  document.querySelectorAll('#belief-tabs .seg-btn').forEach(b => {
    b.addEventListener('click', () => { BF.persp = b.dataset.persp; bfRefresh(false); });
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && BF.open) bfClose(); });
  let sy = 0, dy = 0, dragging = false;
  const start = e => {
    dragging = true; sy = (e.touches ? e.touches[0].clientY : e.clientY);
    dy = 0; card.style.transition = 'none';
  };
  const move = e => {
    if (!dragging) return;
    dy = Math.max(0, (e.touches ? e.touches[0].clientY : e.clientY) - sy);
    card.style.transform = 'translateY(' + dy + 'px)';
  };
  const end = () => {
    if (!dragging) return;
    dragging = false; card.style.transition = '';
    if (dy > 90) bfClose(); else card.style.transform = '';
  };
  [grip, document.querySelector('.sheet-head')].forEach(el => {
    if (!el) return;
    el.addEventListener('touchstart', start, { passive: true });
    el.addEventListener('touchmove', move, { passive: true });
    el.addEventListener('touchend', end);
    el.addEventListener('mousedown', start);
  });
  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', end);
}

/* ================= 初始化 ================= */
function init() {
  loadReplayArchive();
  initLobby();
  onlineRestore();   // 刷新后恢复进行中的在线对局
  if (S.screen === 'game' && S.mode === 'online') { S.deckSpec = 16; syncDeckSpecUI(); }  // 在线只支持48张副
  refreshAIVersion();
  // AI 机器人回调：只走生产模型（不降级）。
  // 一致性：桥内落什么牌，网页就出什么牌（精确映射 + 全规则校验），保证影子牌局永不失步；
  // 任何异常 -> 抛出，由 aiMove 统一暂停并报错（绝不用内置 AI 代打）。
  pdkRegisterAI(async (ctx) => {
    if (!bridge.ready) return null;
    try {
      const r = await bridgeEnqueue(() => bridgeApi('/act', { sid: bridge.sid }, 30000));   // 排队；超时放宽（CPU 被抢占时更宽容）
      if (!r || r.fallback) throw new Error(r && r.error || 'fallback');
      if (Array.isArray(r.cards) && r.cards.length === 0) {
        // 桥内判定"过"：仅当本地确认无解时才接受（有牌必打硬约束）
        if (ctx.last && ctx.legal.length === 0) return [];
        throw new Error('bridge passed while local has legal moves');   // 失步信号
      }
      const cards = cardsFromBridge(r.cards, ctx.seat);
      if (!cards) throw new Error('cards not in hand (desync)');
      const v = validateSelection(ctx.seat, cards);
      if (!v.ok) throw new Error('bridge move invalid locally: ' + v.err);
      bridge.actPending = true;              // 桥内已落子：applyPlay 镜像时只记历史不重发
      return cards;
    } catch (e) {
      bridge.lastErr = String((e && e.message) || e);
      console.warn('[PdkAI] 生产模型异常（不降级）：', bridge.lastErr);
      throw e;                               // 交给 aiMove 暂停报错
    }
  });
  $('btn-play').addEventListener('click', humanPlay);
  $('btn-pass').addEventListener('click', humanPass);
  $('btn-hint').addEventListener('click', showHint);
  bfInitSheet();                                        // 记牌猜牌抽屉（移动端底部面板）
  $('btn-adopt').addEventListener('click', humanPlay);
  $('btn-handover-ok').addEventListener('click', confirmHandover);
  $('btn-next').addEventListener('click', () => {
    hideModal();
    if (S.mode === 'online') {
      onlineApi('/api/next', { token: ONLINE.token }).then(r => { if (r.error) toast(r.error); onlinePoll(); });
      return;
    }
    nextRound();
  });
  $('btn-rematch').addEventListener('click', () => {
    hideModal();
    if (S.mode === 'online') { quitToLobby(); return; }   // 在线的该按钮已改为“回到大厅”
    startMatch();
  });
  $('btn-back-lobby').addEventListener('click', quitToLobby);
  $('btn-quit').addEventListener('click', quitToLobby);
  $('btn-rules').addEventListener('click', () => $('rules-modal').classList.remove('hidden'));
  $('btn-rules-close').addEventListener('click', () => $('rules-modal').classList.add('hidden'));
  $('btn-score').addEventListener('click', showScoreboard);
  $('btn-score-close').addEventListener('click', () => $('score-modal').classList.add('hidden'));
  $('btn-reveal').addEventListener('click', () => {
    S.revealOpp = !S.revealOpp;
    render();
    if (S.revealOpp && isDeck15()) { toast('15张模式：仅明牌看牌，深度解释待模型接入', 2200); clearOppExplain(); }
    else if (S.revealOpp && S.mode === 'ai' && S.roundMoves.length) {
      const last = S.roundMoves[S.roundMoves.length - 1];
      if (last.seat === 1) showOppExplain(S.roundMoves.length); else clearOppExplain();
    } else clearOppExplain();
  });
  window.addEventListener('resize', relayoutHand);
  window.addEventListener('orientationchange', relayoutHand);
}
init();
