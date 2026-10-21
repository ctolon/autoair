import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

/**
 * One sidebar for the whole manual. Every page is listed explicitly so the reading order is
 * deliberate; a page that exists on disk but is missing here still builds, but is unreachable
 * from the navigation.
 */
const sidebars: SidebarsConfig = {
  docs: [
    {
      type: 'category',
      label: 'Getting started',
      collapsed: false,
      items: ['intro', 'getting-started/quick-start', 'getting-started/first-dag', 'getting-started/concepts'],
    },
    {
      type: 'category',
      label: 'Installation',
      link: {type: 'doc', id: 'installation/overview'},
      items: [
        'installation/extras',
        'installation/compatibility',
        'installation/local',
        'installation/plugin',
        'installation/standalone',
        'installation/docker-compose',
        'installation/helm',
        'installation/upgrading',
      ],
    },
    {
      type: 'category',
      label: 'User guide',
      link: {type: 'doc', id: 'user-guide/overview'},
      items: [
        {
          type: 'category',
          label: 'The editor',
          items: ['user-guide/canvas', 'user-guide/shortcuts', 'user-guide/code-mode', 'user-guide/editors'],
        },
        {
          type: 'category',
          label: 'Building DAGs',
          items: [
            'user-guide/operators-and-parameters',
            'user-guide/jinja-xcom-python',
            'user-guide/taskflow-and-loops',
            'user-guide/scheduling-and-assets',
            'user-guide/params-and-trigger-forms',
            'user-guide/human-in-the-loop',
            'user-guide/callbacks-retries-deadlines',
            'user-guide/connections',
            'user-guide/validation',
          ],
        },
        {
          type: 'category',
          label: 'Reuse',
          items: ['user-guide/operator-templates', 'user-guide/templates-and-fragments', 'user-guide/marketplace'],
        },
        {
          type: 'category',
          label: 'Ship and operate',
          items: [
            'user-guide/versions-and-diff',
            'user-guide/deploy',
            'user-guide/reviews-and-approvals',
            'user-guide/runs',
            'user-guide/environments',
            'user-guide/import-export',
            'user-guide/collaboration',
            'user-guide/multi-team',
          ],
        },
      ],
    },
    {
      type: 'category',
      label: 'AI',
      link: {type: 'doc', id: 'ai/introduction'},
      items: [
        'ai/providers',
        'ai/assist',
        'ai/writers',
        'ai/research-and-knowledge',
        {
          type: 'category',
          label: 'Documentation knowledge base',
          link: {type: 'doc', id: 'ai/knowledge-base'},
          items: [
            'ai/knowledge-base-sources',
            'ai/knowledge-base-air-gapped',
            'ai/knowledge-base-stores',
            'ai/knowledge-base-tuning',
            'ai/knowledge-base-security',
          ],
        },
        'ai/guardrails',
        'ai/ai-service',
        'ai/mcp',
        'ai/assistants',
        'ai/troubleshooting',
        'ai/overview',
      ],
    },
    {
      type: 'category',
      label: 'Administration',
      link: {type: 'doc', id: 'admin/overview'},
      items: [
        'admin/configuration',
        'admin/feature-flags',
        'admin/iam',
        'admin/sso',
        'admin/tls-and-headers',
        'admin/database',
        'admin/scaling',
        'admin/admin-panel',
        'admin/policy',
        'admin/audit',
        'admin/observability',
        'admin/backup-restore',
      ],
    },
    {
      type: 'category',
      label: 'Deployment guides',
      link: {type: 'doc', id: 'deployment/overview'},
      items: [
        'deployment/kubernetes',
        'deployment/helm-plugin',
        'deployment/git-bundles',
        'deployment/object-storage',
        'deployment/standalone-proxy',
        'deployment/postgres',
        'deployment/ai-service',
        'deployment/air-gapped',
      ],
    },
    {
      type: 'category',
      label: 'Extending',
      link: {type: 'doc', id: 'extending/overview'},
      items: [
        'extending/custom-operators',
        'extending/operator-metadata',
        'extending/catalog-sources',
        'extending/node-sdk',
        'extending/marketplace',
        'extending/rest-api',
        'extending/spec-format',
        'extending/mcp-integration',
      ],
    },
    {
      type: 'category',
      label: 'Reference',
      items: ['reference/configuration', 'reference/cli'],
    },
    {
      type: 'category',
      label: 'Project',
      items: [
        'security/overview',
        'security/reporting',
        'troubleshooting/faq',
        'contributing/development',
        'contributing/documentation',
        'roadmap',
      ],
    },
  ],
};
export default sidebars;
