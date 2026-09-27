import { env } from 'cloudflare:workers';

// Signups live in Workers KV: key is the email, metadata holds source and time,
// so a single list() pass returns everything the stats page needs.
export interface SignupMeta {
  source: string;
  ts: string;
}

export interface Signup extends SignupMeta {
  email: string;
}

export async function listSignups(): Promise<Signup[]> {
  const out: Signup[] = [];
  let cursor: string | undefined;
  // ponytail: full scan on every stats call, fine for thousands of keys; move to D1 if it grows.
  do {
    const page = await env.WAITLIST.list<SignupMeta>({ cursor });
    for (const k of page.keys) out.push({ email: k.name, source: k.metadata?.source ?? 'unknown', ts: k.metadata?.ts ?? '' });
    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor);
  return out;
}

export function json(body: unknown, status = 200, extra: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...extra },
  });
}
