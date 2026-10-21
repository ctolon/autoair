import React, {useEffect, useRef, useState} from 'react';
import Layout from '@theme/Layout';
import Link from '@docusaurus/Link';
import useBaseUrl from '@docusaurus/useBaseUrl';
import BrowserOnly from '@docusaurus/BrowserOnly';
import {useColorMode} from '@docusaurus/theme-common';

/**
 * REST API reference: renders static/openapi.json (exported with `npm run export-api`, which
 * runs `uv run autoair export-openapi`) with Redoc, loaded from a pinned CDN build so the
 * site bundle stays small. Without network access the downloadable schemas still work.
 */
const REDOC_URL = 'https://cdn.jsdelivr.net/npm/redoc@2.1.5/bundles/redoc.standalone.js';

declare global {
  interface Window {
    Redoc?: {init: (spec: string, options: object, el: HTMLElement, cb?: (err?: unknown) => void) => void};
  }
}

function loadRedoc(): Promise<void> {
  if (window.Redoc) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = REDOC_URL;
    s.async = true;
    s.crossOrigin = 'anonymous';
    s.onload = () => resolve();
    s.onerror = () => reject(new Error('Could not load Redoc'));
    document.head.appendChild(s);
  });
}

function theme(dark: boolean): object {
  const text = dark ? '#dbe6ec' : '#1c2b33';
  return {
    colors: {
      primary: {main: dark ? '#4fd1c5' : '#0b7a7a'},
      text: {primary: text, secondary: dark ? '#9fb0bb' : '#556673'},
      border: {dark: dark ? '#26363f' : '#dde5ea', light: dark ? '#1d2b34' : '#eef3f6'},
      http: {get: '#2f8f5b', post: '#017cee', put: '#9b6a00', patch: '#7a4fd1', delete: '#c0392b'},
    },
    typography: {
      fontFamily: 'Inter, system-ui, sans-serif',
      headings: {fontFamily: 'Inter, system-ui, sans-serif', fontWeight: '650'},
      code: {fontFamily: 'JetBrains Mono, ui-monospace, monospace', color: dark ? '#9de6df' : '#075555', backgroundColor: dark ? '#111b23' : '#f5f8fa'},
    },
    sidebar: {backgroundColor: dark ? '#111b23' : '#f5f8fa', textColor: text, activeTextColor: dark ? '#4fd1c5' : '#0b7a7a'},
    rightPanel: {backgroundColor: dark ? '#0a1117' : '#0f1d26'},
    schema: {nestedBackground: dark ? '#16222c' : '#f5f8fa'},
  };
}

function RedocView({specUrl}: {specUrl: string}): React.JSX.Element {
  const ref = useRef<HTMLDivElement>(null);
  const {colorMode} = useColorMode();
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading');
  useEffect(() => {
    let cancelled = false;
    loadRedoc()
      .then(() => {
        if (cancelled || !ref.current || !window.Redoc) return;
        ref.current.innerHTML = '';
        window.Redoc.init(
          specUrl,
          {
            theme: theme(colorMode === 'dark'),
            hideDownloadButton: false,
            expandResponses: '200,201',
            jsonSampleExpandLevel: 2,
            scrollYOffset: 60,
            nativeScrollbars: true,
          },
          ref.current,
          () => !cancelled && setState('ready'),
        );
      })
      .catch(() => !cancelled && setState('error'));
    return () => {
      cancelled = true;
    };
  }, [specUrl, colorMode]);
  return (
    <>
      {state === 'loading' && <p className="container margin-vert--lg">Loading the API reference…</p>}
      {state === 'error' && (
        <p className="container margin-vert--lg">
          The interactive reference could not be loaded (it needs access to cdn.jsdelivr.net). Download the{' '}
          <a href={specUrl}>OpenAPI document</a> and open it in any OpenAPI viewer.
        </p>
      )}
      <div ref={ref} className="aa-redoc" />
    </>
  );
}

export default function Api(): React.JSX.Element {
  const spec = useBaseUrl('/openapi.json');
  const schema = useBaseUrl('/schemas/dag-spec.schema.json');
  return (
    <Layout title="REST API reference" description="The AutoAir REST API (OpenAPI 3) and the DagSpec JSON Schema.">
      <main>
        <div className="container margin-top--lg">
          <h1>REST API reference</h1>
          <p>
            Generated from the server code with <code>uv run autoair export-openapi</code>. A running server also serves
            the document of the version it runs, with an interactive Swagger UI, at <code>/api/docs</code> (standalone
            and local) or <code>/autoair/api/docs</code> (plugin).
            Authentication, base paths and examples are in the <Link to="/docs/extending/rest-api">REST API guide</Link>.
          </p>
          <p>
            Downloads: <a href={spec}>openapi.json</a> · <a href={schema}>dag-spec.schema.json</a> (
            <Link to="/docs/extending/spec-format">spec format</Link>)
          </p>
        </div>
        <BrowserOnly fallback={<p className="container">Loading the API reference…</p>}>
          {() => <RedocView specUrl={spec} />}
        </BrowserOnly>
      </main>
    </Layout>
  );
}
