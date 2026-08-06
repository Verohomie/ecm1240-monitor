#!/usr/bin/env node
/* Chart gap test — no hardware required.
 *
 * When a meter stops recording, the chart must show that it stopped.
 *
 * The dashboard draws its own SVG paths, and a path joins each point to the
 * next with a straight line. So a stretch with no readings came out as a
 * confident, dead-level trace across however many hours were missing. On the
 * line-voltage chart — the one whose whole job is to catch a meter's voltage
 * electronics starting to fail — that drew a dead meter as an unusually steady
 * supply. The navigator strip underneath did the same, which is worse: a hole
 * is what you drag the window onto to find out what happened, and it can only
 * be aimed at if it is visible.
 *
 * Two different things leave a hole and both must draw as one:
 *
 *   - /api/history buckets with GROUP BY, so a stretch with no rows produces
 *     no points at all (a power cut, a crashed collector, an unplugged meter);
 *   - it also returns an explicit null for a bucket in which every sample was
 *     scrubbed as dead. The dashboard drops those on the way in, which simply
 *     widens the spacing — so testing the TIME between surviving readings
 *     catches both without caring which happened.
 *
 * The trap this guards is the fix's own shortcut: measuring "one bucket" off
 * the FIRST gap. One outage at the start of a range then redefines normal for
 * the whole range, and every ordinary point after it looks adjacent. The
 * median cannot be dragged that way.
 *
 * splitRuns() is lifted out of the shipped web/index.html rather than copied
 * here, so this tests the code the dashboard actually runs.
 *
 *   node tools/test_chart_gaps.js
 */
const fs = require('fs');
const path = require('path');

const HTML = path.join(__dirname, '..', 'web', 'index.html');
const BUCKET_S = 60;
const OUTAGE_S = 3 * 3600;
const T0 = 1754400000;

function loadSplitRuns() {
  const src = fs.readFileSync(HTML, 'utf8');
  const head = src.indexOf('const GAP_FACTOR');
  if (head < 0) throw new Error('GAP_FACTOR is no longer in web/index.html — renamed?');
  const end = src.indexOf('\n}\n', head);
  if (end < 0) throw new Error('could not find the end of splitRuns() in web/index.html');
  return new Function(src.slice(head, end + 2) + '\nreturn splitRuns;')();
}

/* Evenly spaced readings, with an outage dropped in after point `gapAt`. */
function series(n, gapAt) {
  const pts = [];
  let ts = T0;
  for (let i = 0; i < n; i++) {
    pts.push({ ts, w: 121 + (i % 4) * 0.1 });
    ts += (i === gapAt ? OUTAGE_S : BUCKET_S);
  }
  return pts;
}
const total = runs => runs.reduce((a, r) => a + r.length, 0);

const fails = [];
const splitRuns = loadSplitRuns();

// ── an outage must split the line, wherever it falls ─────────────────────────
for (const [where, gapAt] of [['at the very start', 0], ['mid-range', 120], ['near the end', 236]]) {
  const input = series(240, gapAt);
  const runs = splitRuns(input);
  console.log(`3 h outage ${where.padEnd(16)} -> ${runs.length} runs,`
              + ` ${total(runs)} of ${input.length} readings kept`);
  if (runs.length !== 2) fails.push(`an outage ${where} produced ${runs.length} runs, not 2`);
  if (total(runs) !== input.length) fails.push(`an outage ${where} lost or duplicated readings`);
  // the split must land ON the outage, not somewhere convenient
  if (runs.length === 2 && runs[0][runs[0].length - 1].ts !== input[gapAt].ts) {
    fails.push(`the break for an outage ${where} landed at the wrong reading`);
  }
}

// ── an unbroken run must stay one line ───────────────────────────────────────
{
  const runs = splitRuns(series(240, -1));
  console.log(`no outage                        -> ${runs.length} run`);
  if (runs.length !== 1) fails.push(`a gap-free series was split into ${runs.length} runs`);
}

// ── several outages must each show ───────────────────────────────────────────
{
  const pts = [];
  let ts = T0;
  for (let i = 0; i < 300; i++) {
    pts.push({ ts, w: 121 });
    ts += ([50, 150, 250].includes(i) ? OUTAGE_S : BUCKET_S);
  }
  const runs = splitRuns(pts);
  console.log(`three outages                    -> ${runs.length} runs`);
  if (runs.length !== 4) fails.push(`three outages produced ${runs.length} runs, not 4`);
}

// ── the ordinary jitter of real buckets must NOT split ───────────────────────
/* Buckets are whole seconds and readings do not land on them evenly, so real
   spacing wobbles by a bucket. Splitting on that would shatter every chart. */
{
  const pts = [];
  let ts = T0;
  for (let i = 0; i < 240; i++) { pts.push({ ts, w: 121 }); ts += BUCKET_S + (i % 3 === 0 ? 1 : 0); }
  const runs = splitRuns(pts);
  console.log(`ordinary bucket jitter           -> ${runs.length} run`);
  if (runs.length !== 1) fails.push(`normal bucket jitter split the line into ${runs.length} runs`);
}

// ── too few readings to judge spacing must not throw ─────────────────────────
{
  const empty = splitRuns([]);
  const one = splitRuns([{ ts: T0, w: 121 }]);
  console.log(`0 and 1 readings                 -> ${empty.length} and ${one.length} runs`);
  if (empty.length !== 0) fails.push('an empty series produced a run');
  if (one.length !== 1 || one[0].length !== 1) fails.push('a single reading was not drawn');
}

console.log('=== RESULT:', fails.length ? 'FAIL\n  - ' + fails.join('\n  - ') : 'PASS');
process.exit(fails.length ? 1 : 0);
