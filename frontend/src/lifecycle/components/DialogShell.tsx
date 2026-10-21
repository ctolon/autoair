import { CloseButton, Dialog, Portal } from "@chakra-ui/react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { useLifecycle } from "../store";

/** Accessible modal used by every lifecycle dialog (title, description, close button). */
export function DialogShell({
  title,
  description,
  children,
  footer,
  size = "lg",
  onClose,
}: {
  title: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  size?: "sm" | "md" | "lg" | "xl" | "cover";
  onClose?: () => void;
}) {
  const { t } = useTranslation("lifecycle");
  const close = onClose ?? useLifecycle.getState().closeDialog;
  return (
    <Dialog.Root open onOpenChange={(e) => !e.open && close()} size={size} scrollBehavior="inside" lazyMount>
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content css={{ colorScheme: "light", _dark: { colorScheme: "dark" } }}>
            <Dialog.Header flexDirection="column" alignItems="start" gap={1}>
              <Dialog.Title>{title}</Dialog.Title>
              {description ? (
                <Dialog.Description fontSize="sm" color="fg.muted">
                  {description}
                </Dialog.Description>
              ) : null}
            </Dialog.Header>
            <Dialog.Body>{children}</Dialog.Body>
            {footer ? <Dialog.Footer>{footer}</Dialog.Footer> : null}
            <Dialog.CloseTrigger asChild>
              <CloseButton size="sm" aria-label={t("common.close")} />
            </Dialog.CloseTrigger>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}
