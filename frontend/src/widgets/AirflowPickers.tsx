import { Button, Text } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { type ReactNode, Suspense, lazy, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuPlus } from "react-icons/lu";

import {
  listConnectionTypesOptions,
  listConnectionsOptions,
  listPoolsOptions,
  listVariablesOptions,
} from "../api/gen/@tanstack/react-query.gen";
import { Autocomplete, type AutocompleteOption } from "./Autocomplete";
import { useAccess } from "../lib/access";
import { useMeta } from "../lifecycle/hooks";
import { useTeamQuery } from "../store/team";

const NewConnectionDialog = lazy(() =>
  import("./NewConnectionDialog").then((m) => ({ default: m.NewConnectionDialog })),
);

/**
 * Airflow objects exposed as <datalist>s: inputs get native autocompletion but still
 * accept free text (e.g. connections defined via env vars or secrets backends).
 * Fetch failures (e.g. no Airflow reachable) yield no suggestions.
 */
export function AirflowDatalists() {
  const connections = useQuery({
    ...listConnectionsOptions(useTeamQuery()),
    retry: false,
    enabled: useAccess().feature("airflow"),
    staleTime: 60_000,
  });
  const pools = useQuery({
    ...listPoolsOptions(useTeamQuery()),
    retry: false,
    enabled: useAccess().feature("airflow"),
    staleTime: 60_000,
  });
  const variables = useQuery({
    ...listVariablesOptions(useTeamQuery()),
    retry: false,
    enabled: useAccess().feature("airflow"),
    staleTime: 60_000,
  });
  return (
    <>
      <datalist id="autoair-connections">
        {(connections.data ?? []).map((c) => (
          <option key={c.connection_id} value={c.connection_id}>
            {c.conn_type ?? ""}
          </option>
        ))}
      </datalist>
      <datalist id="autoair-pools">
        {(pools.data ?? []).map((p) => (
          <option key={p.name} value={p.name}>
            {p.slots != null ? `${p.slots} slots` : ""}
          </option>
        ))}
      </datalist>
      <datalist id="autoair-variables">
        {(variables.data ?? []).map((v) => (
          <option key={v.key} value={`{{ var.value.${v.key} }}`}>
            {v.key}
          </option>
        ))}
      </datalist>
    </>
  );
}

/**
 * True when a typed id is not among the ids Airflow's metastore lists. Only a hint: the id may
 * come from a secrets backend or environment variables, which the REST API cannot list. Jinja
 * values and unloaded lists (Airflow unreachable, no permission) never count as missing.
 */
export function missingFromMetastore(value: string | null | undefined, ids: readonly string[] | undefined): boolean {
  const v = (value ?? "").trim();
  if (!v || !ids || v.includes("{{") || v.includes("{%")) return false;
  return !ids.includes(v);
}

function MissingHint({ id, kind }: { id: string; kind: "connection" | "pool" }) {
  const { t } = useTranslation("inspector");
  return (
    <Text as="span" display="block" color="fg.warning" data-testid="metastore-hint">
      {t(`pickers.missing.${kind}`, { id })}
    </Text>
  );
}

export const DATALIST = {
  connection: "autoair-connections",
  pool: "autoair-pools",
  variable: "autoair-variables",
} as const;

type PickerProps = {
  label: string;
  value: string | null | undefined;
  onChange: (v: string | null) => void;
  help?: ReactNode;
  error?: string | undefined;
  required?: boolean;
  field?: string | undefined;
  hideLabel?: boolean;
  placeholder?: string | undefined;
};

/**
 * Connection id picker. Connections of the accepted `connTypes` are offered first (plus the
 * hooks' default connection names); free text stays valid (secrets backends, env vars).
 */
export function ConnectionPicker({ connTypes = [], ...props }: PickerProps & { connTypes?: string[] }) {
  const { t } = useTranslation("inspector");
  const connections = useQuery({
    ...listConnectionsOptions(useTeamQuery()),
    retry: false,
    enabled: useAccess().feature("airflow"),
    staleTime: 60_000,
  });
  const types = useQuery({
    ...listConnectionTypesOptions(),
    retry: false,
    enabled: useAccess().feature("airflow"),
    staleTime: 300_000,
  });
  const options = useMemo(() => {
    const wanted = new Set(connTypes);
    const all = connections.data ?? [];
    const matching = wanted.size ? all.filter((c) => c.conn_type && wanted.has(c.conn_type)) : all;
    const opts: AutocompleteOption[] = matching.map((c) => ({
      value: c.connection_id,
      description: [c.conn_type, c.host, c.description].filter(Boolean).join(" · "),
    }));
    const seen = new Set(opts.map((o) => o.value));
    for (const ct of types.data ?? []) {
      if (!wanted.has(ct.conn_type) || !ct.default_conn_name || seen.has(ct.default_conn_name)) continue;
      seen.add(ct.default_conn_name);
      opts.push({
        value: ct.default_conn_name,
        description: t("pickers.defaultConnection", { type: ct.hook_name ?? ct.conn_type }),
      });
    }
    return opts;
  }, [connections.data, types.data, connTypes, t]);
  const [creating, setCreating] = useState(false);
  const canCreate = useAccess().can("connection:write");
  // airflow_secrets_backend: ids from Vault & co. are expected to be missing -> no hint.
  const secretsBackend = useMeta()?.secrets_backend === true;
  const missing =
    !secretsBackend &&
    missingFromMetastore(props.value, connections.isSuccess ? connections.data.map((c) => c.connection_id) : undefined);
  const typeLabel = connTypes.length ? t("pickers.acceptsTypes", { types: connTypes.join(", ") }) : undefined;
  const placeholder = props.placeholder ?? (connTypes[0] ? `${connTypes[0]}_default` : undefined);
  const help = (
    <>
      {props.help ?? typeLabel}{" "}
      {canCreate ? (
        <Button
          variant="plain"
          size="2xs"
          h="auto"
          p={0}
          minW={0}
          color="fg.info"
          // Keep focus where it is: focusing the button would make the parameter form insert
          // its "Insert into" line above, shifting the button away before mouseup.
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => setCreating(true)}
        >
          <LuPlus /> {t("newConnection.open")}
        </Button>
      ) : null}
      {missing ? <MissingHint id={(props.value ?? "").trim()} kind="connection" /> : null}
    </>
  );
  return (
    <>
      <Autocomplete {...props} mono options={options} help={help} placeholder={placeholder} />
      {creating ? (
        <Suspense fallback={null}>
          <NewConnectionDialog
            connTypes={connTypes}
            suggestedId={props.value || placeholder}
            onCreated={(id) => props.onChange(id)}
            onClose={() => setCreating(false)}
          />
        </Suspense>
      ) : null}
    </>
  );
}

export function PoolPicker(props: PickerProps) {
  const { t } = useTranslation("inspector");
  const pools = useQuery({
    ...listPoolsOptions(useTeamQuery()),
    retry: false,
    enabled: useAccess().feature("airflow"),
    staleTime: 60_000,
  });
  const missing = missingFromMetastore(props.value, pools.isSuccess ? pools.data.map((p) => p.name) : undefined);
  const help = missing ? (
    <>
      {props.help}
      <MissingHint id={(props.value ?? "").trim()} kind="pool" />
    </>
  ) : (
    props.help
  );
  const options = (pools.data ?? []).map((p) => ({
    value: p.name,
    description: [p.slots != null ? t("pickers.slots", { count: p.slots }) : null, p.description]
      .filter(Boolean)
      .join(" · "),
  }));
  return (
    <Autocomplete {...props} help={help} mono options={options} placeholder={props.placeholder ?? "default_pool"} />
  );
}
