import type { APIRoute } from 'astro';
import { env } from 'cloudflare:workers';

// The demo dashboard is a single-page app in public/demo. Real files there
// (/demo/assets/*.js) are served by the asset layer before the Worker runs;
// anything else under /demo is a client route, so it gets the app shell.
export const prerender = false;

export const GET: APIRoute = ({ request }) => env.ASSETS.fetch(new URL('/demo/index.html', request.url));
