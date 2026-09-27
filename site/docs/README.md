# VoiceGateway docs

The site at <https://docs.voicegateway.dev>: a [Fumadocs](https://fumadocs.dev) app, statically exported and served from Cloudflare Workers static assets.

```bash
npm ci
npm run dev     # http://localhost:3000/docs
npm run build   # static export in ./out
npx wrangler deploy   # serves ./out at docs.voicegateway.dev (wrangler.jsonc)
```

- Pages: MDX in `content/docs/`, ordered by `content/docs/meta.json`. Every page needs `title` and `description` frontmatter.
- Site name and GitHub links: `lib/shared.ts`. Palette: `../theme.css`, shared with `site/web`; `app/global.css` maps it onto Fumadocs, and `next.config.mjs` widens the Turbopack root to `site/` so the import resolves.
- Cloudflare: the project root is `site/docs`, but the build reads `../theme.css`, so deploy from a full checkout.
- Brand assets: `public/assets/`. The repo README links them by raw URL, so moving one breaks it.
- Don't edit `.source/`; Fumadocs MDX generates it.

Keep it small. Short declarative sentences, sentence-case headings, no em dashes: CI fails on one.
