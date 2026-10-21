import { Box, Button, Field, HStack, Image, Input, Link, Separator, Stack, Text } from "@chakra-ui/react";
import { type FormEvent, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { errorMessage, resolveApiBase, setToken } from "../api/setup";
import { type AuthConfig, fetchAuthConfig, signInWithAirflow, signOutEverywhere } from "./session";

interface LoginAnswer {
  access_token?: string;
  detail?: unknown;
}

/** POST /api/auth/login (standalone): Airflow username/password → Airflow access token. */
async function signIn(username: string, password: string): Promise<Response> {
  return fetch(`${resolveApiBase()}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-AutoAir-CSRF": "1" },
    credentials: "same-origin",
    body: JSON.stringify({ username, password }),
  });
}

/** Forget the token here and sign out on the server (also ends a shared Airflow session). */
export async function signOut(): Promise<void> {
  await signOutEverywhere();
}

/** Inside Airflow (plugin): Airflow's own login page, returning to this page. */
function airflowLoginHref(): string {
  const login = new URL("auth/login", document.baseURI);
  login.searchParams.set("next", window.location.href);
  return login.toString();
}

/**
 * Sign-in page. What it offers comes from `GET /auth/config`:
 *  - plugin mode: a link back to Airflow's login (the Airflow session is shared);
 *  - "Sign in with Airflow" (the Airflow UI session cookie, docs/sso.md);
 *  - the Airflow username/password form (proxied to Airflow, never stored);
 *  - pasting an access token (Airflow API token, or `autoair local --token`).
 */
export function LoginGate({ message }: { message: string }) {
  const { t } = useTranslation();
  const [config, setConfig] = useState<AuthConfig | null | undefined>(undefined);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [token, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const expired = /expired/i.test(message);
  const reason = message.replace(/\.$/, "");

  useEffect(() => {
    let alive = true;
    void fetchAuthConfig().then((c) => {
      if (alive) setConfig(c);
    });
    return () => {
      alive = false;
    };
  }, []);

  const applyToken = (value: string) => {
    setToken(value.trim());
    window.location.reload();
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!username || !password || busy) return;
    setBusy(true);
    setError(null);
    try {
      const resp = await signIn(username, password);
      const body = (await resp.json().catch(() => ({}))) as LoginAnswer;
      if (resp.ok && body.access_token) {
        setPassword("");
        applyToken(body.access_token);
        return;
      }
      if (resp.status === 401) setError(t("login.failed"));
      else if (resp.status === 429)
        setError(t("login.rateLimited", { seconds: resp.headers.get("Retry-After") ?? "60" }));
      else if (resp.status === 404) setError(t("login.unavailable"));
      else setError(t("login.error", { message: errorMessage(body.detail !== undefined ? body : resp.status) }));
    } catch (err) {
      setError(t("login.error", { message: err instanceof Error ? err.message : String(err) }));
    } finally {
      setBusy(false);
    }
  };

  const withAirflow = async () => {
    if (!config) return;
    setError(null);
    setWaiting(true);
    const result = await signInWithAirflow(config);
    setWaiting(false);
    if (result === "signed-in") window.location.reload();
    else if (result === "blocked") setError(t("login.popupBlocked"));
  };

  const plugin = config?.mode === "plugin";
  const local = config?.mode === "local";
  // Older servers (no /auth/config): keep the previous form.
  const showPassword = config === null || (!!config && config.login_enabled);
  const showSso = !!config?.sso.enabled && !!config.sso.airflow_login_url && !plugin;
  const brand = config?.ui;

  return (
    <Stack
      maxW="md"
      mx="auto"
      mt={16}
      gap={4}
      p={6}
      borderWidth="1px"
      rounded="lg"
      bg="bg.panel"
      data-testid="login-gate"
    >
      {brand?.logo_url ? <Image src={brand.logo_url} alt={brand.title} maxH="40px" objectFit="contain" /> : null}
      <Text fontWeight="bold">{t("signInRequired")}</Text>
      <Text fontSize="sm" color="fg.muted">
        {plugin
          ? t("login.pluginHelp")
          : local
            ? t("login.localToken")
            : `${expired ? t("login.expired") : `${reason}.`} ${t("signInHelp")}`}
      </Text>
      {plugin ? (
        <Button asChild size="sm" colorPalette="blue">
          <Link href={airflowLoginHref()} data-testid="airflow-login">
            {t("login.openAirflow")}
          </Link>
        </Button>
      ) : null}
      {showSso ? (
        <Stack gap={1}>
          <Button
            size="sm"
            colorPalette="blue"
            loading={waiting}
            loadingText={t("login.waitingForAirflow")}
            onClick={() => void withAirflow()}
            data-testid="sso-login"
          >
            {t("login.withAirflow")}
          </Button>
          <Text fontSize="xs" color="fg.muted">
            {t("login.withAirflowHelp")}
          </Text>
        </Stack>
      ) : null}
      {showPassword && !plugin ? (
        <>
          {showSso ? <Divider label={t("login.orPassword")} /> : null}
          <form onSubmit={(e) => void submit(e)} aria-label={t("login.submit")}>
            <Stack gap={3}>
              <Field.Root>
                <Field.Label>{t("login.username")}</Field.Label>
                <Input
                  size="sm"
                  name="username"
                  autoComplete="username"
                  autoFocus
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                />
              </Field.Root>
              <Field.Root>
                <Field.Label>{t("login.password")}</Field.Label>
                <Input
                  size="sm"
                  name="password"
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </Field.Root>
              <Button size="sm" type="submit" loading={busy} disabled={!username || !password}>
                {busy ? t("login.submitting") : t("login.submit")}
              </Button>
            </Stack>
          </form>
        </>
      ) : null}
      {error && (
        <Text role="alert" fontSize="sm" color="fg.error">
          {error}
        </Text>
      )}
      {!plugin ? (
        <>
          <Divider label={t("login.orToken")} />
          <HStack>
            <Input
              size="sm"
              type="password"
              aria-label={t("login.token")}
              value={token}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && token.trim()) applyToken(token);
              }}
              placeholder="eyJhbGciOi…"
            />
            <Button size="sm" variant="outline" disabled={!token.trim()} onClick={() => applyToken(token)}>
              {t("continue")}
            </Button>
          </HStack>
        </>
      ) : null}
    </Stack>
  );
}

function Divider({ label }: { label: string }) {
  return (
    <HStack gap={2}>
      <Separator flex="1" />
      <Box fontSize="xs" color="fg.muted" flexShrink={0}>
        {label}
      </Box>
      <Separator flex="1" />
    </HStack>
  );
}
