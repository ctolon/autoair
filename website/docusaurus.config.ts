import {existsSync} from 'node:fs';

import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

import {BASE_URL, EDIT_BRANCH, GITHUB_OWNER, GITHUB_REPO, REPO_URL, SITE_URL} from './site.config';

/**
 * Algolia DocSearch is prepared but off: the local search plugin is used by default.
 * Set ALGOLIA_APP_ID, ALGOLIA_API_KEY and ALGOLIA_INDEX_NAME to switch to Algolia.
 */
const algolia =
  process.env.ALGOLIA_APP_ID && process.env.ALGOLIA_API_KEY && process.env.ALGOLIA_INDEX_NAME
    ? {
        appId: process.env.ALGOLIA_APP_ID,
        apiKey: process.env.ALGOLIA_API_KEY,
        indexName: process.env.ALGOLIA_INDEX_NAME,
        contextualSearch: true,
      }
    : undefined;

/** The version dropdown appears once `npm run docs:version -- <x.y>` has created versions.json. */
const versioned = existsSync('versions.json');

const config: Config = {
  title: 'AutoAir',
  tagline: 'A visual DAG builder for Apache Airflow 3 that writes readable, validated Python.',
  favicon: 'img/favicon.svg',

  url: SITE_URL,
  baseUrl: BASE_URL,
  organizationName: GITHUB_OWNER,
  projectName: GITHUB_REPO,
  trailingSlash: false,

  onBrokenLinks: 'throw',
  onBrokenAnchors: 'throw',
  onDuplicateRoutes: 'throw',

  future: {
    v4: {removeLegacyPostBuildHeadAttribute: true},
  },

  markdown: {
    // .md files are CommonMark (no JSX parsing of `<state>` or `{...}` in prose);
    // .mdx files may use components such as <Screenshot> and <Video>.
    format: 'detect',
    mermaid: true,
    hooks: {
      onBrokenMarkdownLinks: 'throw',
      onBrokenMarkdownImages: 'throw',
    },
  },
  themes: [
    '@docusaurus/theme-mermaid',
    ...(algolia
      ? []
      : [
          [
            '@easyops-cn/docusaurus-search-local',
            {
              hashed: true,
              language: ['en'],
              docsRouteBasePath: '/docs',
              blogRouteBasePath: '/blog',
              indexBlog: true,
              highlightSearchTermsOnTargetPage: true,
              explicitSearchResultPath: true,
              searchBarShortcutHint: true,
            },
          ] as [string, Record<string, unknown>],
        ]),
  ],

  i18n: {defaultLocale: 'en', locales: ['en']},

  // Writes build/llms.txt and build/llms-full.txt (https://llmstxt.org) after every build.
  plugins: ['./scripts/llms-txt.mjs'],

  customFields: {repoUrl: REPO_URL},

  presets: [
    [
      'classic',
      {
        docs: {
          path: 'docs',
          routeBasePath: 'docs',
          sidebarPath: './sidebars.ts',
          // Generated reference pages set custom_edit_url: null; everything else is edited in place.
          editUrl: ({docPath}) => `${REPO_URL}/edit/${EDIT_BRANCH}/website/docs/${docPath}`,
          showLastUpdateTime: false,
          breadcrumbs: true,
        },
        blog: {
          path: 'blog',
          routeBasePath: 'blog',
          blogTitle: 'Release notes',
          blogDescription: 'AutoAir releases and changes.',
          blogSidebarTitle: 'Releases',
          blogSidebarCount: 'ALL',
          showReadingTime: false,
          feedOptions: {type: ['rss', 'atom'], xslt: true},
          editUrl: `${REPO_URL}/edit/${EDIT_BRANCH}/website/`,
          onInlineTags: 'warn',
          onInlineAuthors: 'warn',
          onUntruncatedBlogPosts: 'ignore',
        },
        theme: {customCss: './src/css/custom.css'},
        sitemap: {changefreq: 'weekly', priority: 0.5},
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    image: 'img/screens/canvas-dag-light.png',
    metadata: [
      {name: 'keywords', content: 'Apache Airflow, DAG builder, no-code, low-code, Airflow plugin, MCP'},
    ],
    colorMode: {defaultMode: 'light', respectPrefersColorScheme: true},
    docs: {sidebar: {hideable: true, autoCollapseCategories: false}},
    announcementBar: undefined,
    navbar: {
      title: 'AutoAir',
      logo: {alt: 'AutoAir', src: 'img/logo.svg', width: 32, height: 32},
      hideOnScroll: false,
      items: [
        {type: 'docSidebar', sidebarId: 'docs', position: 'left', label: 'Docs'},
        {to: '/docs/user-guide/overview', label: 'User guide', position: 'left', activeBaseRegex: '/docs/user-guide/'},
        {to: '/docs/admin/overview', label: 'Administration', position: 'left', activeBaseRegex: '/docs/(admin|deployment)/'},
        {to: '/docs/reference/configuration', label: 'Reference', position: 'left', activeBaseRegex: '/docs/reference/'},
        {to: '/api', label: 'API', position: 'left'},
        {to: '/blog', label: 'Releases', position: 'left'},
        ...(versioned
          ? [{type: 'docsVersionDropdown', position: 'right', dropdownActiveClassDisabled: true} as const]
          : []),
        {
          href: REPO_URL,
          position: 'right',
          className: 'header-github-link',
          'aria-label': 'GitHub repository',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Docs',
          items: [
            {label: 'Getting started', to: '/docs/getting-started/quick-start'},
            {label: 'User guide', to: '/docs/user-guide/overview'},
            {label: 'Deployment guides', to: '/docs/deployment/overview'},
            {label: 'Configuration reference', to: '/docs/reference/configuration'},
          ],
        },
        {
          title: 'Project',
          items: [
            {label: 'Roadmap', to: '/docs/roadmap'},
            {label: 'Release notes', to: '/blog'},
            {label: 'Contributing', to: '/docs/contributing/development'},
            {label: 'Security policy', to: '/docs/security/reporting'},
          ],
        },
        {
          title: 'Source',
          items: [
            {label: 'GitHub', href: REPO_URL},
            {label: 'Issues', href: `${REPO_URL}/issues`},
            {label: 'REST API (OpenAPI)', to: '/api'},
            {label: 'llms.txt', to: 'pathname:///llms.txt'},
            {label: 'llms-full.txt', to: 'pathname:///llms-full.txt'},
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} the AutoAir authors. Licensed under the Apache License 2.0. Apache Airflow and Airflow are trademarks of the Apache Software Foundation; AutoAir is not affiliated with or endorsed by it.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.vsDark,
      additionalLanguages: ['bash', 'diff', 'json', 'yaml', 'ini', 'toml', 'python', 'docker', 'nginx', 'sql'],
    },
    mermaid: {
      theme: {light: 'neutral', dark: 'dark'},
    },
    tableOfContents: {minHeadingLevel: 2, maxHeadingLevel: 4},
    ...(algolia ? {algolia} : {}),
  } satisfies Preset.ThemeConfig,
};

export default config;
