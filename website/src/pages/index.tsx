import React, {useState} from 'react';
import Layout from '@theme/Layout';
import Link from '@docusaurus/Link';
import useBaseUrl from '@docusaurus/useBaseUrl';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import ThemedImage from '@theme/ThemedImage';
import CodeBlock from '@theme/CodeBlock';

import styles from './index.module.css';

type Feature = {title: string; body: string; to: string};

const FEATURES: Feature[] = [
  {
    title: 'Every installed operator',
    body: 'A static catalog of all providers, Airflow libraries and your own operators, with typed parameter forms, required markers and rendered docstrings.',
    to: '/docs/user-guide/operators-and-parameters',
  },
  {
    title: 'Readable Python out',
    body: 'The canvas edits a versioned DagSpec; code generation goes through the Python AST and ruff. Switch to code mode and edits flow back into the graph.',
    to: '/docs/user-guide/code-mode',
  },
  {
    title: 'Airflow 3 scheduling',
    body: 'Timezones, cron and multi-cron, asset expressions, event-driven assets, assets-and-time and a live "next runs" preview from Airflow’s own timetables.',
    to: '/docs/user-guide/scheduling-and-assets',
  },
  {
    title: 'Validated before deploy',
    body: 'Graph, catalog, Jinja, policy and version checks run as you type; a warm DagBag pool imports the generated file the way Airflow will.',
    to: '/docs/user-guide/validation',
  },
  {
    title: 'Git, review and approvals',
    body: 'Deploy to a DAG folder, object storage or Git. Pull requests carry the author’s identity; four-eyes approval and direct-push rights come from IAM.',
    to: '/docs/user-guide/deploy',
  },
  {
    title: 'Runs and logs in context',
    body: 'Trigger, backfill and clear from the editor. Task states, logs and XComs appear on the canvas next to the node that produced them.',
    to: '/docs/user-guide/runs',
  },
  {
    title: 'Live collaboration',
    body: 'Several authors edit the same DAG with presence, remote cursors and per-user undo, authorised by short-lived tickets.',
    to: '/docs/user-guide/collaboration',
  },
  {
    title: 'AI with guardrails',
    body: 'Generate, edit and explain DAGs with your own LLM provider. Proposals arrive as diffs; guardrails, audit and IAM apply to every call.',
    to: '/docs/ai/introduction',
  },
  {
    title: 'MCP for agents',
    body: 'Claude Code, Claude Desktop and other MCP clients author and validate DAGs through the same API, under the caller’s permissions.',
    to: '/docs/ai/mcp',
  },
];

const STEPS = [
  {
    n: '1',
    title: 'Compose',
    body: 'Drag operators onto the canvas or start from a template. Fill typed parameter forms, map XComs, write inline functions and scripts.',
  },
  {
    n: '2',
    title: 'Validate',
    body: 'Problems update live. Each finding links to the exact task and field; the server re-checks with Airflow’s DagBag and your policy rules.',
  },
  {
    n: '3',
    title: 'Review',
    body: 'Inspect the generated Python and the visual diff against the last version. Open a pull request or request an approval.',
  },
  {
    n: '4',
    title: 'Deploy and run',
    body: 'AutoAir writes where Airflow actually reads DAGs, then lets you unpause, trigger and debug the run without leaving the editor.',
  },
];

const MODES = [
  {
    title: 'Airflow plugin',
    tag: 'Production',
    body: 'Installs into the Airflow API server. Users sign in with Airflow, and Airflow roles decide what they may do. Adds Browse → DAG Builder and an AutoAir tab on each DAG.',
    cmd: 'pip install autoair',
    to: '/docs/installation/plugin',
  },
  {
    title: 'Standalone server',
    tag: 'Separate service',
    body: 'Runs next to Airflow as its own process or pod and forwards each caller’s Airflow token, so Airflow RBAC still applies to every call.',
    cmd: 'autoair serve --airflow-url https://airflow.example.com',
    to: '/docs/installation/standalone',
  },
  {
    title: 'Local mode',
    tag: 'Workstation',
    body: 'A single-user editor bound to loopback. Author, validate and export DAGs with no Airflow server; connect one later to run them.',
    cmd: "uvx --from 'autoair[standalone,airflow]' autoair local --open",
    to: '/docs/installation/local',
  },
];

function Hero(): React.JSX.Element {
  const light = useBaseUrl('/img/screens/run-overlay-light.png');
  const dark = useBaseUrl('/img/screens/run-overlay-dark.png');
  const video = useBaseUrl('/video/build-dag.webm');
  const poster = useBaseUrl('/video/build-dag.jpg');
  const [playing, setPlaying] = useState(false);
  return (
    <header className={styles.hero}>
      <div className="container">
        <div className={styles.heroText}>
          <p className={styles.eyebrow}>For Apache Airflow 3 · Apache-2.0</p>
          <h1 className={styles.title}>
            Build Airflow DAGs visually.
            <br />
            <span className={styles.titleAccent}>Ship reviewed Python.</span>
          </h1>
          <p className={styles.subtitle}>
            AutoAir is a no-code and low-code DAG builder that runs inside Airflow or next to it. Authors compose
            DAGs on a canvas; reviewers get readable, validated code, a diff and a pull request; operators keep
            Airflow&apos;s permissions in charge.
          </p>
          <div className={styles.ctas}>
            <Link className="button button--primary button--lg" to="/docs/getting-started/quick-start">
              Get started
            </Link>
            <Link className="button button--outline button--secondary button--lg" to="/docs/getting-started/first-dag">
              Build your first DAG
            </Link>
          </div>
          <div className={styles.install}>
            <CodeBlock language="bash">{"uvx --from 'autoair[standalone,airflow]' autoair local --open"}</CodeBlock>
          </div>
        </div>
        <div className={styles.media}>
          <div className={styles.browserBar} aria-hidden="true">
            <span />
            <span />
            <span />
          </div>
          {playing ? (
            <video
              className={styles.mediaContent}
              src={video}
              poster={poster}
              autoPlay
              controls
              muted
              playsInline
              loop
              aria-label="Recording: building a DAG on the AutoAir canvas"
            />
          ) : (
            <button type="button" className={styles.playButton} onClick={() => setPlaying(true)} aria-label="Play a 30-second recording of building a DAG">
              <ThemedImage
                className={styles.mediaContent}
                alt="The AutoAir editor showing a DAG with task states from its latest run on the canvas, the Runs tab below and DAG settings on the right"
                sources={{light, dark}}
                width={1440}
                height={900}
              />
              <span className={styles.playBadge}>
                <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
                  <path d="M8 5v14l11-7z" fill="currentColor" />
                </svg>
                Watch the editor
              </span>
            </button>
          )}
        </div>
      </div>
    </header>
  );
}

function Features(): React.JSX.Element {
  return (
    <section className={styles.section} aria-labelledby="features-title">
      <div className="container">
        <h2 id="features-title" className={styles.sectionTitle}>
          The whole DAG lifecycle in one editor
        </h2>
        <p className={styles.sectionLead}>
          AutoAir covers authoring, validation, review, deployment and debugging, and leaves execution to Airflow.
        </p>
        <div className={styles.grid}>
          {FEATURES.map((f) => (
            <Link key={f.title} to={f.to} className={styles.card}>
              <h3>{f.title}</h3>
              <p>{f.body}</p>
              <span className={styles.more}>Read more →</span>
            </Link>
          ))}
        </div>
      </div>
    </section>
  );
}

function HowItWorks(): React.JSX.Element {
  return (
    <section className={`${styles.section} ${styles.alt}`} aria-labelledby="how-title">
      <div className="container">
        <h2 id="how-title" className={styles.sectionTitle}>
          How it works
        </h2>
        <p className={styles.sectionLead}>
          The canvas, the code editor, validation and code generation all read and write one model: the DagSpec. The
          Python file Airflow parses is derived from it, never the other way round.
        </p>
        <ol className={styles.steps}>
          {STEPS.map((s) => (
            <li key={s.n} className={styles.step}>
              <span className={styles.stepNumber} aria-hidden="true">
                {s.n}
              </span>
              <h3>{s.title}</h3>
              <p>{s.body}</p>
            </li>
          ))}
        </ol>
        <div className={styles.flow} aria-hidden="true">
          <span>Canvas / code mode</span>
          <span className={styles.arrow}>→</span>
          <span>DagSpec</span>
          <span className={styles.arrow}>→</span>
          <span>Python · YAML · JSON</span>
          <span className={styles.arrow}>→</span>
          <span>Folder · object store · Git PR</span>
          <span className={styles.arrow}>→</span>
          <span>Airflow</span>
        </div>
      </div>
    </section>
  );
}

function Modes(): React.JSX.Element {
  return (
    <section className={styles.section} aria-labelledby="modes-title">
      <div className="container">
        <h2 id="modes-title" className={styles.sectionTitle}>
          Deploy it the way your platform runs
        </h2>
        <p className={styles.sectionLead}>
          One Python package, three ways to run it. Configuration, IAM and storage behave the same in each.
        </p>
        <div className={styles.modes}>
          {MODES.map((m) => (
            <article key={m.title} className={styles.mode}>
              <span className={styles.tag}>{m.tag}</span>
              <h3>{m.title}</h3>
              <p>{m.body}</p>
              <CodeBlock language="bash">{m.cmd}</CodeBlock>
              <Link to={m.to}>Installation guide →</Link>
            </article>
          ))}
        </div>
        <p className={styles.modesFoot}>
          Production guides: <Link to="/docs/deployment/kubernetes">Kubernetes reference architecture</Link> ·{' '}
          <Link to="/docs/deployment/helm-plugin">Helm</Link> · <Link to="/docs/deployment/git-bundles">GitDagBundle and git-sync</Link> ·{' '}
          <Link to="/docs/deployment/postgres">PostgreSQL</Link> · <Link to="/docs/deployment/air-gapped">Air-gapped</Link>
        </p>
      </div>
    </section>
  );
}

function CallToAction(): React.JSX.Element {
  const {siteConfig} = useDocusaurusContext();
  const repoUrl = String(siteConfig.customFields?.repoUrl ?? '');
  return (
    <section className={styles.cta} aria-labelledby="cta-title">
      <div className="container">
        <h2 id="cta-title">Try it on your laptop in a minute</h2>
        <p>Local mode needs no Airflow server. Point it at a real Airflow when you want to run what you built.</p>
        <div className={styles.ctas}>
          <Link className="button button--primary button--lg" to="/docs/getting-started/quick-start">
            Quick start
          </Link>
          <Link className="button button--outline button--secondary button--lg" href={repoUrl}>
            Source on GitHub
          </Link>
        </div>
      </div>
    </section>
  );
}

export default function Home(): React.JSX.Element {
  return (
    <Layout
      title="Visual DAG builder for Apache Airflow 3"
      description="AutoAir is an open-source no-code/low-code DAG builder for Apache Airflow 3: visual authoring, readable generated Python, validation, Git review and deployment, runs, collaboration and AI assistance.">
      <Hero />
      <main>
        <Features />
        <HowItWorks />
        <Modes />
        <CallToAction />
      </main>
    </Layout>
  );
}
