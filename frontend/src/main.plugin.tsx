/**
 * Entry point of the Airflow >= 3.1 `react_apps` bundle (ES module; sets `AirflowPlugin`).
 * Airflow's UI provides React, ReactDOM, Chakra and Emotion as globals and renders the
 * default export inside its own layout.
 */
import { Box, ChakraProvider, type SystemContext, defaultSystem } from "@chakra-ui/react";
import { useLayoutEffect, useRef, useState } from "react";

import { App } from "./App";
import { RootErrorBoundary } from "./ui/ErrorBoundary";

declare global {
  // Set by the Airflow UI so plugins inherit its theme (light/dark, tokens).
  var ChakraUISystem: SystemContext | undefined;
}

export type PluginProps = { dagId?: string; runId?: string; taskId?: string };

/** The host config is module-level state read once by the app's deep-link handling. */
function setInitialDag(dagId: string): void {
  window.__AUTOAIR_CONFIG__ = { ...window.__AUTOAIR_CONFIG__, initialDagId: dagId };
}

/**
 * Height that fills the window below wherever Airflow places the plugin (nav page, DAG tab,
 * with or without a header): measured instead of guessed, so no empty strip is left below.
 */
function useFillHeight() {
  const ref = useRef<HTMLDivElement>(null);
  const [top, setTop] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setTop(Math.max(0, Math.round(el.getBoundingClientRect().top + window.scrollY)));
    measure();
    window.addEventListener("resize", measure);
    const observer = new ResizeObserver(measure);
    if (el.parentElement) observer.observe(el.parentElement);
    return () => {
      window.removeEventListener("resize", measure);
      observer.disconnect();
    };
  }, []);
  return { ref, height: `calc(100dvh - ${top}px)` };
}

export default function AutoAirPlugin(props: PluginProps) {
  const { ref, height } = useFillHeight();
  const system = globalThis.ChakraUISystem ?? defaultSystem;
  // Rendered as the "AutoAir" tab of a DAG page: open that DAG in the builder.
  if (props.dagId) setInitialDag(props.dagId);
  return (
    <ChakraProvider value={system}>
      <Box ref={ref} h={height} minH="600px">
        <RootErrorBoundary>
          <App />
        </RootErrorBoundary>
      </Box>
    </ChakraProvider>
  );
}

// Airflow's loader reads `globalThis[<app name>] ?? globalThis.AirflowPlugin` after import().
// The module is evaluated only once even though two react_apps share it, so register the
// component under every app name (see autoair/plugin.py) as well.
const g = globalThis as Record<string, unknown>;
g.AirflowPlugin = AutoAirPlugin;
g["DAG Builder"] = AutoAirPlugin;
g["AutoAir"] = AutoAirPlugin;
