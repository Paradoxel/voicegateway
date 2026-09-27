import type { BaseLayoutProps } from 'fumadocs-ui/layouts/shared';
import { appName, gitConfig } from './shared';

// The split gauge, the same mark as the landing page, drawn from the theme.
function Mark() {
  return (
    <svg viewBox="9.2 -2.8 81.5 58.8" width={26} height={19} aria-hidden="true">
      <g fill="none" strokeWidth={5.5} strokeLinecap="round">
        <path d="M14 33.8V41.8M23 16.3V30.3M32 6.5V26.5M41 7.1V19.1M50 0V24M59 5.1V21.1M68 11.5V21.5" stroke="currentColor" />
        <path d="M74.4 20.9A38 38 0 0 1 88 50" stroke="var(--vg-accent)" />
        <path d="M50 50L63.6 29" stroke="currentColor" strokeWidth={4.4} />
        <circle cx={50} cy={50} r={6} fill="currentColor" stroke="none" />
      </g>
    </svg>
  );
}

export function baseOptions(): BaseLayoutProps {
  return {
    nav: {
      title: (
        <>
          <Mark />
          {appName}
        </>
      ),
    },
    githubUrl: `https://github.com/${gitConfig.user}/${gitConfig.repo}`,
  };
}
