# voicegateway.dev

The landing page: [Astro](https://astro.build) on Cloudflare Workers. Pages are prerendered; three routes run on the Worker: `/api/waitlist`, `/api/stats` and `/demo/*`.

```bash
npm ci
npm run dev       # http://localhost:4321
npm run build     # dist/client (static) + dist/server (Worker)
npm run deploy    # build, then wrangler deploy to voicegateway.dev
```

- Palette: `../theme.css`, shared with `site/docs`. Page styles live next to each page.
- `collector.sh` and `install.sh` are copied from the repo root on every build, so `voicegateway.dev/*.sh` can never drift from what the repo ships. Don't commit copies.
- `public/demo` is the dashboard's demo build. Refresh it with `npm run sync-demo`, which runs `build:demo` in `src/dashboard/frontend`.
- Waitlist: signups go to the `voicegateway-waitlist` D1 database (bound in `wrangler.jsonc`). `/api/stats?key=STATS_KEY` shows them. Secrets and optional vars are listed in `wrangler.jsonc`; set them with `npx wrangler secret put <NAME>`.
- Analytics: PostHog, only when `PUBLIC_POSTHOG_KEY` is set at build time.
- Types: `npm run check` generates `worker-configuration.d.ts` with `wrangler types`, then runs `astro check`. The secrets are typed in `src/env.d.ts`.

Voice: short declarative sentences, sentence-case headings, no em dashes (CI fails on one). The example call on the home page is priced with voice-prices; re-price it if you change the models.
