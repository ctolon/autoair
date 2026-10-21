/**
 * Component-test helpers (Vitest + Testing Library, jsdom): the providers the app wraps
 * every view in (Chakra, react-query, i18n) and a `/api/meta` fixture so components that
 * read the deployment's features and the user's permissions render without a server.
 */
import { ChakraProvider, defaultSystem } from "@chakra-ui/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type RenderResult, render } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { I18nextProvider } from "react-i18next";

import { getMetaQueryKey } from "../api/gen/@tanstack/react-query.gen";
import type { MetaInfo } from "../api/gen/types.gen";
import { i18n } from "../i18n";

type MetaOverrides = Omit<Partial<MetaInfo>, "iam"> & { iam?: Partial<NonNullable<MetaInfo["iam"]>> | null };

/** A `/api/meta` payload: a local-folder target, every feature on, no IAM (everything allowed). */
export function metaFixture(overrides: MetaOverrides = {}): MetaInfo {
  const { iam, ...rest } = overrides;
  const base = {
    autoair_version: "0.1.0",
    airflow_version: "3.3.2",
    target_airflow_version: null,
    mode: "standalone",
    auth: "bearer",
    allow_python_code: true,
    allow_privileged_operators: true,
    import_check_on_deploy: true,
    output_dir: "/dags/autoair",
    supports_react_apps: true,
    storage: { kind: "local", location: "/dags/autoair", writable: true, details: {} },
    environments: [],
    default_environment: null,
    team: null,
    ai_enabled: false,
    ai_model: null,
    mcp_enabled: false,
    collab_enabled: false,
    features: {},
    policy: {
      allow_python_code: true,
      allow_privileged_operators: true,
      require_owner: false,
      require_description: false,
      min_retries: null,
      required_tags: [],
      forbidden_operators: [],
      max_tasks: null,
      dag_id_prefix: null,
      python_ref_allow: ["*"],
    },
    iam: null,
    ...rest,
  } as unknown as MetaInfo;
  if (iam) {
    base.iam = {
      user: "alice",
      roles: ["Editor"],
      permissions: [],
      auth: "bearer",
      mode: "standalone",
      strategies: [],
      login_enabled: false,
      deploy: {
        git_mode: null,
        can_save: true,
        can_deploy_live: true,
        direct_push: false,
        default_opens_pr: false,
        require_approval: false,
      },
      ...iam,
    } as NonNullable<MetaInfo["iam"]>;
  }
  return base;
}

export function testQueryClient(meta: MetaInfo | null = metaFixture()): QueryClient {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity, gcTime: Infinity } },
  });
  if (meta) client.setQueryData(getMetaQueryKey(), meta);
  return client;
}

function Providers({ children, client }: { children: ReactNode; client: QueryClient }) {
  return (
    <I18nextProvider i18n={i18n}>
      <QueryClientProvider client={client}>
        <ChakraProvider value={defaultSystem}>{children}</ChakraProvider>
      </QueryClientProvider>
    </I18nextProvider>
  );
}

/** Render `ui` inside the app's providers, in English, with the given `/api/meta`. */
export function renderWithProviders(
  ui: ReactElement,
  { meta = metaFixture(), client }: { meta?: MetaInfo | null; client?: QueryClient } = {},
): RenderResult & { client: QueryClient } {
  void i18n.changeLanguage("en");
  const queryClient = client ?? testQueryClient(meta);
  const result = render(<Providers client={queryClient}>{ui}</Providers>);
  return { ...result, client: queryClient };
}
