#!/usr/bin/env node
/**
 * Draw a polyline as consecutive trend_line segments on TradingView Desktop.
 * One CDP session for the whole path (much faster than per-segment `tv` CLI).
 *
 * Usage:
 *   node scripts/tv_draw_polyline.mjs <payload.json
 *   node scripts/tv_draw_polyline.mjs path/to/payload.json
 *
 * Payload:
 * {
 *   "points": [{ "time": 123, "price": 100.5 }, ...],
 *   "remove_ids": ["id1", ...],   // optional prior Diviner drawings
 *   "color": "#d4a35c",
 *   "width": 2
 * }
 */
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, '..');
const drawingUrl = pathToFileURL(resolve(root, 'tradingview-mcp/src/core/drawing.js')).href;
const connectionUrl = pathToFileURL(resolve(root, 'tradingview-mcp/src/connection.js')).href;

const { removeOne } = await import(drawingUrl);
const { evaluate, getChartApi, safeString, requireFinite, disconnect } = await import(connectionUrl);

function readPayload() {
  const arg = process.argv[2];
  const raw = arg ? readFileSync(arg, 'utf8') : readFileSync(0, 'utf8');
  return JSON.parse(raw);
}

async function removeIds(ids) {
  const removed = [];
  for (const id of ids || []) {
    if (!id) continue;
    try {
      await removeOne({ entity_id: String(id) });
      removed.push(id);
    } catch {
      // Shape may already be gone
    }
  }
  return removed;
}

async function drawTrendSegment(apiPath, p1, p2, overridesStr) {
  const p1time = requireFinite(p1.time, 'point.time');
  const p1price = requireFinite(p1.price, 'point.price');
  const p2time = requireFinite(p2.time, 'point2.time');
  const p2price = requireFinite(p2.price, 'point2.price');

  const before = await evaluate(`${apiPath}.getAllShapes().map(function(s) { return s.id; })`);
  // createMultipointShape may return a Promise; do not awaitPromise — the shape
  // is still created, and awaiting can hang indefinitely on Desktop.
  await evaluate(`
    ${apiPath}.createMultipointShape(
      [{ time: ${p1time}, price: ${p1price} }, { time: ${p2time}, price: ${p2price} }],
      { shape: ${safeString('trend_line')}, overrides: ${overridesStr}, text: "" }
    )
  `);
  await new Promise((r) => setTimeout(r, 30));
  const after = await evaluate(`${apiPath}.getAllShapes().map(function(s) { return s.id; })`);
  return (after || []).find((id) => !(before || []).includes(id)) || null;
}

async function main() {
  const payload = readPayload();
  const points = payload.points || [];
  if (points.length < 2) {
    console.log(JSON.stringify({ success: false, error: 'Need at least 2 points' }));
    process.exit(1);
  }

  const color = payload.color || '#d4a35c';
  const width = payload.width || 2;
  const overrides = {
    linecolor: color,
    linewidth: width,
    linestyle: 0,
    extendLeft: false,
    extendRight: false,
  };
  const overridesStr = JSON.stringify(overrides);

  const removed = await removeIds(payload.remove_ids || []);
  const apiPath = await getChartApi();
  const entity_ids = [];

  for (let i = 0; i < points.length - 1; i++) {
    const id = await drawTrendSegment(apiPath, points[i], points[i + 1], overridesStr);
    if (id) entity_ids.push(id);
  }

  console.log(
    JSON.stringify({
      success: true,
      segments: points.length - 1,
      entity_ids,
      removed_ids: removed,
      color,
    })
  );
}

async function shutdown(code) {
  try {
    await disconnect();
  } catch {
    // ignore
  }
  process.exit(code);
}

main()
  .then(() => shutdown(0))
  .catch(async (err) => {
    console.log(JSON.stringify({ success: false, error: err.message || String(err) }));
    await shutdown(1);
  });
