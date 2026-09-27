import type { APIRoute } from 'astro';
import { env } from 'cloudflare:workers';
import { json, signups } from '../../lib/waitlist';

// GET /api/stats?key=STATS_KEY
// Waitlist signups: total, by source, by day, and the recent list, emails
// included. Key-protected because the data is email-linked. JSON by default, or
// a small HTML page in a browser.
export const prerender = false;

const NO_STORE = { 'Cache-Control': 'no-store' };

function esc(s: unknown): string {
  return String(s).replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] as string,
  );
}

interface Stats {
  total: number;
  bySource: Record<string, number>;
  byDay: Record<string, number>;
  recent: { email: string; ts: string; source: string }[];
}

function htmlPage(stats: Stats): string {
  const countRows = (obj: Record<string, number>) =>
    Object.entries(obj)
      .map(([k, v]) => `<tr><td>${esc(k)}</td><td class="n">${esc(v)}</td></tr>`)
      .join('') || '<tr><td>(none yet)</td></tr>';
  const recent =
    stats.recent
      .map((r) => `<tr><td>${esc(r.email)}</td><td>${esc(r.source)}</td><td class="n">${esc(r.ts)}</td></tr>`)
      .join('') || '<tr><td>(none yet)</td></tr>';
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="robots" content="noindex">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<title>VoiceGateway waitlist</title>
<style>body{font:14px/1.5 ui-monospace,Menlo,monospace;background:#0a0a0a;color:#fafafa;max-width:720px;margin:40px auto;padding:0 20px;font-variant-numeric:tabular-nums}
h1{font-size:20px;font-weight:500}h2{font-size:13px;font-weight:500;color:#a3a3a3;margin-top:28px}
table{width:100%;border-collapse:collapse}td{padding:4px 8px 4px 0;border-bottom:1px solid #262626}.n{text-align:right}
.big{font-size:40px;font-weight:500;color:#cba6f7}</style></head><body>
<h1>VoiceGateway waitlist</h1>
<div class="big">${esc(stats.total)}</div><div>total signups</div>
<h2>By source</h2><table>${countRows(stats.bySource)}</table>
<h2>By day, last 30</h2><table>${countRows(stats.byDay)}</table>
<h2>Recent, last 200</h2><table>${recent}</table>
</body></html>`;
}

export const GET: APIRoute = async ({ request }) => {
  const url = new URL(request.url);
  const key = env.STATS_KEY;
  const provided =
    url.searchParams.get('key') || (request.headers.get('authorization') || '').replace(/^Bearer\s+/i, '');
  // The same 401 whether the key is unset or wrong, so the endpoint never
  // reveals its configuration to an unauthenticated caller.
  if (!key || provided !== key) return json({ ok: false, error: 'unauthorized' }, 401, NO_STORE);

  let stats: Stats;
  try {
    const db = await signups();
    const [total, bySource, byDay, recent] = await db.batch([
      db.prepare('SELECT count(*) AS n FROM signups'),
      db.prepare('SELECT source, count(*) AS n FROM signups GROUP BY source ORDER BY n DESC'),
      db.prepare('SELECT substr(created_at, 1, 10) AS day, count(*) AS n FROM signups GROUP BY day ORDER BY day DESC LIMIT 30'),
      db.prepare('SELECT email, source, created_at AS ts FROM signups ORDER BY created_at DESC LIMIT 200'),
    ]);
    const counts = (rows: unknown[], k: string) =>
      Object.fromEntries((rows as Record<string, string | number>[]).map((r) => [r[k], Number(r.n)]));
    stats = {
      total: Number((total.results[0] as { n: number } | undefined)?.n ?? 0),
      bySource: counts(bySource.results, 'source'),
      byDay: counts(byDay.results, 'day'),
      recent: recent.results as Stats['recent'],
    };
  } catch (err) {
    console.error('stats: query failed', err);
    return json({ ok: false, error: 'query failed' }, 500, NO_STORE);
  }

  const wantsHtml =
    url.searchParams.get('format') !== 'json' && (request.headers.get('accept') || '').includes('text/html');
  if (wantsHtml) {
    return new Response(htmlPage(stats), {
      headers: { 'Content-Type': 'text/html; charset=utf-8', 'X-Content-Type-Options': 'nosniff', ...NO_STORE },
    });
  }
  return json({ ok: true, ...stats }, 200, NO_STORE);
};
