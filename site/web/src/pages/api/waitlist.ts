import type { APIRoute } from 'astro';
import { env } from 'cloudflare:workers';
import { json, signups } from '../../lib/waitlist';

// On-demand on the Worker: this writes to D1 on every request.
export const prerender = false;

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;
const MAX_EMAIL_LENGTH = 254;

function validateEmail(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const email = value.trim().toLowerCase();
  if (!email || email.length > MAX_EMAIL_LENGTH || !EMAIL_RE.test(email)) return null;
  return email;
}

// Notify through the Cloudflare Email Sending REST API: a voicegateway.dev
// routing address to the verified destination. Skips cleanly when unconfigured
// and never throws into the request path; the signup is already stored.
async function notify(email: string, source: string): Promise<void> {
  const account = env.CF_ACCOUNT_ID;
  const token = env.CF_EMAIL_API_TOKEN;
  if (!account || !token) return;

  const to = env.LEAD_NOTIFY_TO || 'mahi@mahimai.ca';
  const from = env.LEAD_FROM || 'waitlist@voicegateway.dev';
  const text = [`Email: ${email}`, `Source: ${source}`, `When: ${new Date().toISOString()}`].join('\n');

  try {
    const resp = await fetch(
      `https://api.cloudflare.com/client/v4/accounts/${account}/email/sending/send`,
      {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({
          to,
          from: { address: from, name: 'VoiceGateway' },
          reply_to: email,
          subject: `New VoiceGateway waitlist signup: ${email}`,
          text,
        }),
        // Bounded, so a slow email API can never stall the request.
        signal: AbortSignal.timeout(5000),
      },
    );
    if (!resp.ok) console.error('lead email send failed', resp.status, await resp.text());
  } catch (err) {
    console.error('lead email error', err);
  }
}

export const POST: APIRoute = async ({ request }) => {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return json({ ok: false, error: 'invalid json' }, 400);
  }

  const record = body as Record<string, unknown>;
  const email = validateEmail(record?.email);
  if (!email) return json({ ok: false, error: 'invalid email' }, 400);

  // source is caller-controlled: reduce it to a safe slug so junk or newlines
  // never reach the notification email or the stats page.
  const rawSource = typeof record?.source === 'string' ? record.source : '';
  const source = rawSource.toLowerCase().replace(/[^a-z0-9_-]/g, '').slice(0, 64) || 'landing';

  let isNew = false;
  try {
    const db = await signups();
    const result = await db
      .prepare('INSERT INTO signups (email, source) VALUES (?, ?) ON CONFLICT (email) DO NOTHING')
      .bind(email, source)
      .run();
    isNew = result.meta.changes > 0;
  } catch (err) {
    console.error('waitlist: db insert failed', err);
    return json({ ok: false, error: 'storage error' }, 500);
  }

  // First-time emails only; a re-submit is a silent no-op.
  if (isNew) await notify(email, source);

  return json({ ok: true });
};
