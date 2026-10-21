import { Alert, Badge, Box, Button, Checkbox, Field, HStack, Input, NativeSelect, Stack, Text } from "@chakra-ui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuPlugZap, LuSave, LuTrash2 } from "react-icons/lu";

import { aiStatusQueryKey, getMyAiModelOptions } from "../../api/gen/@tanstack/react-query.gen";
import { deleteMyAiModel, setMyAiModel, testMyAiModel } from "../../api/gen/sdk.gen";
import type { UserLlmUpdate, UserLlmView, UserModelState } from "../../api/gen/types.gen";
import { toaster } from "../../ui/toaster";
import { ErrorBox, Pending } from "../components/AiFindings";
import { DialogShell } from "../components/DialogShell";

type Provider = NonNullable<UserLlmView["provider"]>;

function urlProblem(value: string): string | null {
  if (!value.trim()) return null;
  try {
    const url = new URL(value.trim());
    return url.protocol === "http:" || url.protocol === "https:" ? null : "scheme";
  } catch {
    return "invalid";
  }
}

/** The signed-in user's own model (``ai.llm_mode`` per_user / both). Keys are write-only. */
export function AiModelDialog() {
  const { t } = useTranslation("lifecycle");
  const query = useQuery({ ...getMyAiModelOptions(), retry: false });
  return (
    <DialogShell title={t("ai.myModel.title")} description={t("ai.myModel.description")} size="lg">
      {query.isPending ? <Pending label={t("ai.myModel.loading")} /> : null}
      {query.isError ? <ErrorBox error={query.error} /> : null}
      {query.data ? <ModelForm state={query.data} /> : null}
    </DialogShell>
  );
}

/**
 * The provider select's initial value (exported for tests): a connection-based config
 * (``conn_id`` without a provider) keeps "from connection" instead of silently switching
 * to the first allowed provider.
 */
export function initialProvider(settings: UserLlmView, providers: Provider[]): Provider | "" {
  if (settings.provider) return settings.provider;
  if (settings.conn_id) return "";
  return providers[0] ?? "";
}

/** Whether the form differs from the saved settings (exported for tests). */
export function formDirty(
  settings: UserLlmView,
  form: { provider: Provider | ""; model: string; baseUrl: string; connId: string; apiKey: string; clearKey: boolean },
): boolean {
  return (
    (form.provider || null) !== (settings.provider ?? null) ||
    (form.model.trim() || null) !== (settings.model ?? null) ||
    (form.baseUrl.trim() || null) !== (settings.base_url ?? null) ||
    (form.connId.trim() || null) !== (settings.conn_id ?? null) ||
    !!form.apiKey ||
    form.clearKey
  );
}

function ModelForm({ state }: { state: UserModelState }) {
  const { t } = useTranslation("lifecycle");
  const qc = useQueryClient();
  const { settings, policy } = state;
  const central = policy.mode === "central";
  const providers = policy.providers as Provider[];
  const [provider, setProvider] = useState<Provider | "">(() => initialProvider(settings, providers));
  const [model, setModel] = useState(settings.model ?? "");
  const [baseUrl, setBaseUrl] = useState(settings.base_url ?? "");
  const [connId, setConnId] = useState(settings.conn_id ?? "");
  const [apiKey, setApiKey] = useState("");
  const [clearKey, setClearKey] = useState(false);
  const badUrl = urlProblem(baseUrl);
  // With unsaved edits "Test" sends the form itself (validated, never stored).
  const dirty = formDirty(settings, { provider, model, baseUrl, connId, apiKey, clearKey });
  const canSave = !central && !badUrl && (!!provider || !!connId.trim());

  const refresh = (next?: UserModelState) => {
    if (next) qc.setQueryData(getMyAiModelOptions().queryKey, next);
    void qc.invalidateQueries({ queryKey: aiStatusQueryKey() });
  };
  const formBody = (): UserLlmUpdate => {
    const body: UserLlmUpdate = {
      provider: provider || null,
      model: model.trim() || null,
      base_url: policy.base_urls.length ? baseUrl.trim() || null : null,
      conn_id: policy.conn_ids_allowed ? connId.trim() || null : null,
      clear_api_key: clearKey,
    };
    if (apiKey) (body as { api_key?: string }).api_key = apiKey;
    return body;
  };
  const test = useMutation({
    mutationFn: async (useForm: boolean) =>
      (await testMyAiModel({ ...(useForm ? { body: formBody() } : {}), throwOnError: true })).data,
  });
  const save = useMutation({
    mutationFn: async () => (await setMyAiModel({ body: formBody(), throwOnError: true })).data,
    onSuccess: (next) => {
      setApiKey("");
      setClearKey(false);
      refresh(next);
      toaster.create({ type: "success", title: t("ai.myModel.saved") });
    },
  });
  const remove = useMutation({
    mutationFn: async () => (await deleteMyAiModel({ throwOnError: true })).data,
    onSuccess: (next) => {
      refresh(next);
      setModel("");
      setBaseUrl("");
      setConnId("");
      toaster.create({ type: "success", title: t("ai.myModel.removed") });
    },
  });

  const keyPlaceholder = settings.api_key_set
    ? t("ai.myModel.keySet", { last4: settings.api_key_last4 ?? "····" })
    : t("ai.myModel.keyPlaceholder");

  return (
    <Stack gap={4}>
      <HStack gap={2} wrap="wrap">
        <Text fontSize="sm">{t("ai.myModel.mode")}:</Text>
        <Badge colorPalette={central ? "gray" : "blue"}>{t(`ai.myModel.modes.${policy.mode}`)}</Badge>
        {settings.configured ? (
          <Badge colorPalette="green" variant="subtle">
            {t("ai.myModel.configured")}
          </Badge>
        ) : null}
      </HStack>
      {central ? (
        <Alert.Root status="info" size="sm">
          <Alert.Indicator />
          <Alert.Description>{t("ai.myModel.central")}</Alert.Description>
        </Alert.Root>
      ) : null}
      <Stack gap={3} opacity={central ? 0.6 : 1}>
        <HStack gap={3} align="start" wrap="wrap">
          <Field.Root flex="1" minW="12rem">
            <Field.Label fontSize="xs">{t("ai.myModel.provider")}</Field.Label>
            <NativeSelect.Root size="sm" disabled={central}>
              <NativeSelect.Field
                value={provider}
                onChange={(e) => setProvider(e.target.value as Provider | "")}
                aria-label={t("ai.myModel.provider")}
              >
                {policy.conn_ids_allowed || provider === "" ? (
                  <option value="">{t("ai.myModel.fromConnection")}</option>
                ) : null}
                {providers.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </NativeSelect.Field>
              <NativeSelect.Indicator />
            </NativeSelect.Root>
          </Field.Root>
          <Field.Root flex="2" minW="14rem">
            <Field.Label fontSize="xs">{t("ai.myModel.model")}</Field.Label>
            <Input
              size="sm"
              fontFamily="mono"
              value={model}
              disabled={central}
              placeholder={t("ai.myModel.modelPlaceholder")}
              onChange={(e) => setModel(e.target.value)}
            />
            <Field.HelperText fontSize="2xs">
              {t("ai.myModel.allowedModels", { list: policy.models.join(", ") || "–" })}
            </Field.HelperText>
          </Field.Root>
        </HStack>
        {policy.base_urls.length ? (
          <Field.Root invalid={!!badUrl}>
            <Field.Label fontSize="xs">{t("ai.myModel.baseUrl")}</Field.Label>
            <Input
              size="sm"
              fontFamily="mono"
              value={baseUrl}
              disabled={central}
              placeholder={policy.base_urls[0]}
              onChange={(e) => setBaseUrl(e.target.value)}
            />
            {badUrl ? (
              <Field.ErrorText>{t("ai.myModel.badUrl")}</Field.ErrorText>
            ) : (
              <Field.HelperText fontSize="2xs">
                {t("ai.myModel.allowedUrls", { list: policy.base_urls.join(", ") })}
              </Field.HelperText>
            )}
          </Field.Root>
        ) : null}
        <Field.Root>
          <Field.Label fontSize="xs">{t("ai.myModel.apiKey")}</Field.Label>
          <Input
            size="sm"
            type="password"
            autoComplete="off"
            value={apiKey}
            disabled={central || clearKey}
            placeholder={keyPlaceholder}
            onChange={(e) => setApiKey(e.target.value)}
          />
          <Field.HelperText fontSize="2xs">{t("ai.myModel.keyHelp")}</Field.HelperText>
        </Field.Root>
        {settings.api_key_set ? (
          <Checkbox.Root
            size="sm"
            checked={clearKey}
            disabled={central}
            onCheckedChange={(e) => setClearKey(!!e.checked)}
          >
            <Checkbox.HiddenInput />
            <Checkbox.Control />
            <Checkbox.Label fontSize="sm">{t("ai.myModel.clearKey")}</Checkbox.Label>
          </Checkbox.Root>
        ) : null}
        {policy.conn_ids_allowed ? (
          <Field.Root>
            <Field.Label fontSize="xs">{t("ai.myModel.connId")}</Field.Label>
            <Input
              size="sm"
              fontFamily="mono"
              value={connId}
              disabled={central}
              placeholder="ai_my_openai"
              onChange={(e) => setConnId(e.target.value)}
            />
            <Field.HelperText fontSize="2xs">{t("ai.myModel.connHelp")}</Field.HelperText>
          </Field.Root>
        ) : null}
      </Stack>
      {save.isError ? <ErrorBox error={save.error} /> : null}
      {remove.isError ? <ErrorBox error={remove.error} /> : null}
      {test.isError ? <ErrorBox error={test.error} /> : null}
      {test.data ? (
        <Box borderWidth="1px" rounded="md" p={2} fontSize="sm" bg="bg.subtle">
          <Text fontSize="2xs" color="fg.muted" mb={1}>
            {t("ai.myModel.testReply")}
          </Text>
          {test.data.explanation}
        </Box>
      ) : null}
      <HStack gap={2} justify="flex-end" wrap="wrap">
        <Button
          size="sm"
          variant="ghost"
          loading={test.isPending}
          disabled={dirty && !canSave}
          title={dirty ? t("ai.myModel.testUnsaved") : undefined}
          onClick={() => test.mutate(dirty && canSave)}
        >
          <LuPlugZap /> {t("ai.myModel.test")}
        </Button>
        {settings.configured ? (
          <Button
            size="sm"
            variant="outline"
            colorPalette="red"
            loading={remove.isPending}
            onClick={() => remove.mutate()}
          >
            <LuTrash2 /> {t("ai.myModel.remove")}
          </Button>
        ) : null}
        <Button
          size="sm"
          colorPalette="blue"
          disabled={!canSave}
          loading={save.isPending}
          onClick={() => save.mutate()}
        >
          <LuSave /> {t("ai.myModel.save")}
        </Button>
      </HStack>
    </Stack>
  );
}
