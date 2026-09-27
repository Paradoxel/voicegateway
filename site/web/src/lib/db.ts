import { neon } from '@neondatabase/serverless';
import { env } from 'cloudflare:workers';

type Sql = ReturnType<typeof neon>;

let client: Sql | null = null;

// A lazily created SQL tag, or null when no database is configured (local dev,
// a preview without the secret). Callers handle null so nothing crashes.
// DATABASE_URL is the Neon name; POSTGRES_URL is kept because that is what the
// old Vercel integration set, and the database is the same one.
export function getSql(): Sql | null {
  const url = env.DATABASE_URL || env.POSTGRES_URL || '';
  if (!url) return null;
  if (!client) client = neon(url);
  return client;
}

let schemaReady = false;

// Idempotent. CREATE TABLE IF NOT EXISTS is a cheap catalog check once the table
// exists; the module-level guard skips it on warm invocations.
export async function ensureSignupsTable(sql: Sql): Promise<void> {
  if (schemaReady) return;
  await sql`
    CREATE TABLE IF NOT EXISTS signups (
      email      TEXT PRIMARY KEY,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      source     TEXT
    )
  `;
  schemaReady = true;
}

export function json(body: unknown, status = 200, extra: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...extra },
  });
}
