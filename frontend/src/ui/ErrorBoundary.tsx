import { Box, Button, Code, Stack, Text } from "@chakra-ui/react";
import { Component, type ErrorInfo, type ReactNode } from "react";

import { i18n } from "../i18n";

type ErrorArea =
  "app" | "builder" | "toolbar" | "palette" | "canvas" | "codeMode" | "bottomPanel" | "inspector" | "dialog";
type Props = {
  area: ErrorArea;
  children: ReactNode;
  onReset?: () => void;
  /** Clears the error when it changes (e.g. another node is selected). */
  resetKey?: unknown;
  /** Offers "Undo last change": the usual way out when an edit produced a state that cannot render. */
  onUndo?: () => void;
  /** Offers reloading the page (the root boundary: nothing else is left to retry with). */
  reload?: boolean;
};
type State = { error: Error | null };

/**
 * Contains a rendering error to one area (panel, dialog, canvas) instead of unmounting the
 * whole builder; the user can retry without losing the document.
 */
export class ErrorBoundary extends Component<Props, State> {
  override state: State = { error: null };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  override componentDidUpdate(prev: Props) {
    if (this.state.error && !Object.is(prev.resetKey, this.props.resetKey)) this.setState({ error: null });
  }

  override componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`AutoAir: ${this.props.area} crashed`, error, info.componentStack);
  }

  private reset = () => {
    this.props.onReset?.();
    this.setState({ error: null });
  };

  override render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <Box p={3} role="alert" data-testid="error-boundary">
        <Stack gap={2}>
          <Text fontWeight="semibold" color="fg.error" fontSize="sm">
            {this.props.area === "app"
              ? i18n.t("errorBoundary.appTitle")
              : i18n.t("errorBoundary.title", { area: i18n.t(`errorBoundary.areas.${this.props.area}`) })}
          </Text>
          <Code fontSize="xs" whiteSpace="pre-wrap">
            {error.message}
          </Code>
          <Stack direction="row" gap={2}>
            {this.props.onUndo ? (
              <Button
                size="xs"
                colorPalette="blue"
                onClick={() => {
                  this.props.onUndo?.();
                  this.reset();
                }}
              >
                {i18n.t("errorBoundary.undo")}
              </Button>
            ) : null}
            <Button size="xs" variant="outline" onClick={this.reset}>
              {i18n.t("errorBoundary.retry")}
            </Button>
            {this.props.reload ? (
              <Button size="xs" variant="ghost" onClick={() => window.location.reload()}>
                {i18n.t("errorBoundary.reload")}
              </Button>
            ) : null}
          </Stack>
        </Stack>
      </Box>
    );
  }
}

/**
 * Last line of defence at the app roots (plugin and standalone): an error outside every
 * area boundary (providers, login gate, start-up) shows a recoverable message instead of an
 * empty page.
 */
export function RootErrorBoundary({ children }: { children: ReactNode }) {
  return (
    <Box h="100%" w="100%" bg="bg" color="fg">
      <ErrorBoundary area="app" reload>
        {children}
      </ErrorBoundary>
    </Box>
  );
}
