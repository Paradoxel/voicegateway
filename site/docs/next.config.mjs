import { fileURLToPath } from 'node:url';
import { createMDX } from 'fumadocs-mdx/next';

const withMDX = createMDX();

/** @type {import('next').NextConfig} */
const config = {
  output: 'export',
  reactStrictMode: true,
  // app/global.css imports ../../theme.css, the palette shared with site/web.
  // Turbopack refuses files outside its root, so the root is site/, not site/docs/.
  turbopack: { root: fileURLToPath(new URL('..', import.meta.url)) },
};

export default withMDX(config);
