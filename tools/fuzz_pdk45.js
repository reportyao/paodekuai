#!/usr/bin/env node
/* pdk45 集成差分模糊测试：从线上 app.js 抽取前端纯规则函数，与 /api/decide 打整局，
 * 逐手断言 AI 返回在前端 legalPlays 内；不一致即 dump 现场。 */
'use strict';
const fs = require('fs');
const http = require('http');

const APP = fs.readFileSync('/home/ubuntu/paodekuai/app.js', 'utf8');

function extract(name) {
  const key = 'function ' + name + '(';
  const i = APP.indexOf(key);
  if (i < 0) throw new Error('not found: ' + name);
  let depth = 0;
  const j = APP.indexOf('{', i);
  for (let k = j; k < APP.length; k++) {
    if (APP[k] === '{') depth++;
    else if (APP[k] === '}') { depth--; if (!depth) return APP.slice(i, k + 1); }
  }
  throw new Error('unbalanced: ' + name);
}
const FN = ['isConsecutiveRun', 'rankCounts', 'analyzeShape', 'canBeat', 'genLeads',
  'genBeats', 'comboContextOK', 'breaksBomb', 'violatesBaodan', 'legalPlays',
  'comboToTrick', 'buildDeck', 'buildDeck15'];
const NL = String.fromCharCode(10);
const OPTS = { sanzhang: false, nobomb: false, red10: false, four3: false };

function buildApi(names) {
  const src = 'const DECK15_SKIP = new Set([43,45,46]);' + NL
    + names.map(extract).join(NL + NL) + NL + 'return {' + names.join(',') + '};';
  return new Function(src)();
}
function miss(e) {
  const m = /([^\s]+) is not defined/.exec(String(e.message));
  if (m && !FN.includes(m[1]) && /^\w+$/.test(m[1])) return m[1];
  throw e;
}
let API = null;
for (;;) {
  API = buildApi(FN);
  try {                                   // 预热探针：把生成路径真正调一遍，逼出懒绑定的缺失依赖
    const dk = API.buildDeck15();
    API.legalPlays(dk.slice(0, 15), { last: null, oppCount: 14 }, OPTS);
    const cb = API.analyzeShape([dk[0]]);
    API.legalPlays(dk.slice(0, 15), { last: cb, oppCount: 14 }, OPTS);
    const b2 = API.analyzeShape(dk.slice(0, 5));
    if (b2) API.legalPlays(dk.slice(0, 15), { last: b2, oppCount: 14 }, OPTS);
    break;
  } catch (e) { FN.push(miss(e)); }
}
const analyzeShape = API.analyzeShape, legalPlays = API.legalPlays, comboToTrick = API.comboToTrick;
const comboContextOK = API.comboContextOK, canBeat = API.canBeat;
const breaksBomb = API.breaksBomb, violatesBaodan = API.violatesBaodan;
const buildDeck15 = API.buildDeck15;

const F2P = {}; for (let i = 0; i <= 42; i++) F2P[i] = i; F2P[44] = 44; F2P[47] = 48;

function decide(payload) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify(payload);
    const req = http.request({ host: '127.0.0.1', port: 8310, path: '/ai15/api/decide',
      method: 'POST', headers: { 'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body) }, timeout: 60000 },
      res => { let d = ''; res.on('data', c => d += c);
        res.on('end', () => { try { resolve(JSON.parse(d)); } catch (e) { reject(e); } }); });
    req.on('error', reject); req.write(body); req.end();
  });
}

function deal() {
  const deck = buildDeck15().slice();
  for (let i = deck.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    const t = deck[i]; deck[i] = deck[j]; deck[j] = t;
  }
  return { h0: deck.slice(0, 15), h1: deck.slice(15, 30) };
}

const R = ['♠', '♥', '♣', '♦'];
function ctext(cards) { return cards.map(c => c.r + R[c.s]).join(' '); }
function trickOf(last) {
  return comboToTrick(last.combo);   // 与修复后的前端同源（len/nc 语义已修正）
}

(async () => {
  const games = parseInt(process.argv[2] || '30', 10);
  let mismatches = 0, aiMoves = 0, decidedErr = 0, bail = 0, miscountB = 0;
  global.TYPES = {};
  for (let g = 0; g < games && mismatches < 3; g++) {
    const { h0, h1 } = deal();
    const st = { hands: [h0, h1], last: null, turn: 0, history: [] };
    let steps = 0;
    while (st.hands[0].length && st.hands[1].length && steps++ < 400) {
      const seat = st.turn, hand = st.hands[seat], oppN = st.hands[1 - seat].length;
      const legal = legalPlays(hand, { last: st.last ? st.last.combo : null, oppCount: oppN }, OPTS);
      let moveCards;
      if (seat === 0) {
        moveCards = legal.length ? legal[Math.floor(Math.random() * legal.length)].cards : [];
      } else {
        const payload = {
          my_hand: hand.map(c => F2P[c.i]),
          opp_n: oppN,
          trick: (st.last && st.last.by !== seat) ? trickOf(st.last) : null,
          history: st.history.map(m => ({ seat: (m.seat === seat) ? 0 : 1, move: m.move })),
        };
        const r = await decide(payload);
        if (r.error) { decidedErr++; if (decidedErr <= 2) console.log('GAME', g, 'decide error:', r.error); break; }
        aiMoves++;
        moveCards = (r.pass || !r.move || !r.move.length)
          ? [] : r.move.map(x => hand.find(c => F2P[c.i] === x)).filter(Boolean);
        /* 规则校验（与前端 ai15RuleCheck 同一套判定） */
        let type = 'OK';
        if (moveCards.length !== (moveCards.length ? hand.filter(c => moveCards.indexOf(c) >= 0).length : 0)) type = 'E-手牌外';
        if (type === 'OK' && moveCards.length) {
          const combo = analyzeShape(moveCards);
          const seatOpp = st.hands[1 - seat].length;
          if (!combo) type = 'F-无效牌型';
          else if (!comboContextOK(combo, hand.length, OPTS)) type = 'G-时机不合法';
          else if (st.last && st.last.by !== seat && !canBeat(combo, st.last.combo, OPTS)) type = 'H-管不上';
          else if (violatesBaodan(hand, combo, seatOpp)) type = 'I-报单违规';
        }
        if (type === 'OK' && !moveCards.length && legal.length) type = 'C-能压却过牌';
        if (type !== 'OK') {
          mismatches++;
          const tally = (TYPES[type] = (TYPES[type] || 0) + 1);
          console.log('');
          console.log('!!! [' + type + '] GAME', g, 'step', steps, '(该型累计', tally + ')',
            '| 引擎legal_count=', r.legal_count, '| 引擎path=', r.decision_path || r.mode);
          console.log('AI手牌:', ctext(hand));
          console.log('待跟:', st.last ? (JSON.stringify(st.last.combo) + ' ' + ctext(st.last.cards)) : '(领出)');
          console.log('AI返回:', moveCards.length ? ctext(moveCards) : '(过牌)');
          if (type === 'B-规则分歧')
            console.log('前端合法集:', legal.map(m => m.combo.t + '[' + ctext(m.cards) + ']').join(' | '));
          if (mismatches <= 4) {
            const r2 = await decide(Object.assign({}, payload, { pattern_map: 1, explain: 1 }));
            const cands = (r2 && (r2.candidates || (r2.pattern_map && r2.pattern_map.candidates))) || [];
            console.log('带pattern_map重取: move=', JSON.stringify(r2.move), 'cands=',
              JSON.stringify(cands).slice(0, 600));
          }
          if (type !== 'A-同点数异花色' && !moveCards.length === false && type === 'C-能压却过牌') {
            /* C 型也继续收集，不中断游戏（人类视角继续） */
          }
          if (type === 'B-规则分歧' && miscountB++ >= 2) { /* 收集两类各几例即可 */ }
        }
        var TYPES = TYPES || (global.TYPES = global.TYPES || {});
        if (type !== 'OK' && type !== 'A-同点数异花色' && bail++ > 8) break;
        if (type === 'A-同点数异花色') { /* 用本地同点数代表牌继续 */ }
      }
      const combo = moveCards.length ? analyzeShape(moveCards) : null;
      st.history.push({ seat, move: moveCards.map(c => F2P[c.i]) });
      for (const c of moveCards) { const ix = hand.indexOf(c); if (ix >= 0) hand.splice(ix, 1); }
      st.last = moveCards.length ? { combo, cards: moveCards.slice(), by: seat } : null;
      st.turn = 1 - seat;
    }
  }
  console.log('');
  console.log('完成: aiMoves=' + aiMoves + ' decide错误=' + decidedErr + ' 不一致=' + mismatches);
  process.exit(mismatches ? 1 : 0);
})().catch(e => { console.error('FATAL', e); process.exit(2); });
