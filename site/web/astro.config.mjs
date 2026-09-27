// @ts-check
// voicegateway.dev: static pages, plus two on-demand endpoints (the waitlist and
// its stats) that run on the Cloudflare Worker. Everything else is prerendered.
import { defineConfig } from 'astro/config';
import cloudflare from '@astrojs/cloudflare';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  site: 'https://voicegateway.dev',
  output: 'static',
  // No image transforms and no sessions here, so no Cloudflare Images or KV
  // bindings get provisioned on deploy.
  adapter: cloudflare({ imageService: 'passthrough' }),
  session: false,
  // Keep source whitespace: compression drops the space between a line of text
  // and a link that starts the next line (same fix as prices.mahimai.ca).
  compressHTML: false,
  vite: {
    // src/styles/global.css imports ../../../theme.css, shared with site/docs.
    server: { fs: { allow: [fileURLToPath(new URL('..', import.meta.url))] } },
  },
});
