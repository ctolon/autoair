import React from 'react';
import ThemedImage from '@theme/ThemedImage';
import useBaseUrl from '@docusaurus/useBaseUrl';

export interface ScreenshotProps {
  /** Base name in static/img/screens: `<name>-light.png` and `<name>-dark.png` must both exist. */
  name: string;
  /** Text alternative describing what the screenshot shows. */
  alt: string;
  caption?: React.ReactNode;
  /** Narrow screenshots (dialogs, menus) look better at a reduced width. */
  width?: 'full' | 'wide' | 'narrow';
}

/**
 * A screenshot of the AutoAir UI that follows the site's color mode: the light capture in light
 * mode, the dark capture in dark mode. Captures come from `npm run capture`.
 */
export default function Screenshot({name, alt, caption, width = 'full'}: ScreenshotProps): React.JSX.Element {
  const light = useBaseUrl(`/img/screens/${name}-light.png`);
  const dark = useBaseUrl(`/img/screens/${name}-dark.png`);
  return (
    <figure className={`aa-shot aa-shot--${width}`}>
      <a href={light} target="_blank" rel="noopener" className="aa-shot__frame" aria-label={`Open full-size screenshot: ${alt}`}>
        <ThemedImage alt={alt} sources={{light, dark}} loading="lazy" decoding="async" width={1440} height={900} />
      </a>
      {caption ? <figcaption>{caption}</figcaption> : null}
    </figure>
  );
}
