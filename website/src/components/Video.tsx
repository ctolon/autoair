import React from 'react';
import useBaseUrl from '@docusaurus/useBaseUrl';

export interface VideoProps {
  /** Base name in static/video: `<name>.webm` plus an optional `<name>.jpg` poster frame. */
  name: string;
  /** What the recording shows (used as the accessible label). */
  title: string;
  caption?: React.ReactNode;
  poster?: boolean;
}

/** A short, silent screen recording made by `npm run capture:videos`. */
export default function Video({name, title, caption, poster = true}: VideoProps): React.JSX.Element {
  const src = useBaseUrl(`/video/${name}.webm`);
  const posterUrl = useBaseUrl(`/video/${name}.jpg`);
  return (
    <figure className="aa-shot aa-shot--full">
      <div className="aa-shot__frame">
        <video
          controls
          muted
          playsInline
          loop
          preload="metadata"
          poster={poster ? posterUrl : undefined}
          aria-label={title}
          width={1440}
          height={900}>
          <source src={src} type="video/webm" />
          <a href={src}>Download the recording ({title})</a>
        </video>
      </div>
      {caption ? <figcaption>{caption}</figcaption> : null}
    </figure>
  );
}
