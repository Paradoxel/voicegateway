import { env } from 'cloudflare:workers';

// Signups live in D1. The email primary key is what makes a signup count once:
// INSERT ... ON CONFLICT DO NOTHING is atomic, which a KV read-then-write is not.
let tableReady = false;

export async function signups(): Promise<D1Database> {
  if (!tableReady) {
    // Idempotent and cheap once the table exists; the flag skips it on warm isolates.
    await env.DB.prepare(
      `CREATE TABLE IF NOT EXISTS signups (
        email TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
      )`,
    ).run();
    tableReady = true;
  }
  return env.DB;
}

export function json(body: unknown, status = 200, extra: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...extra },
  });
}
