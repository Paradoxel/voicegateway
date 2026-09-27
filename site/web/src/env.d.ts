// Secrets and optional vars the Worker reads, set with `wrangler secret put` or
// in the Cloudflare dashboard. `wrangler types` only sees what wrangler.jsonc
// declares, so these are declared here.
declare namespace Cloudflare {
  interface Env {
    STATS_KEY?: string;
    CF_ACCOUNT_ID?: string;
    CF_EMAIL_API_TOKEN?: string;
    LEAD_NOTIFY_TO?: string;
    LEAD_FROM?: string;
  }
}

interface ImportMetaEnv {
  readonly PUBLIC_POSTHOG_KEY?: string;
  readonly PUBLIC_POSTHOG_HOST?: string;
}
