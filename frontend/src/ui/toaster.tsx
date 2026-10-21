import { Portal, Spinner, Stack, Toast, Toaster as ChakraToaster, createToaster } from "@chakra-ui/react";

export const toaster = createToaster({ placement: "bottom-end", pauseOnPageIdle: true });

export function Toaster() {
  return (
    <Portal>
      <ChakraToaster toaster={toaster} insetInline={{ mdDown: "4" }}>
        {(toast) => (
          <Toast.Root width={{ md: "sm" }}>
            {toast.type === "loading" ? <Spinner size="sm" color="blue.solid" /> : <Toast.Indicator />}
            <Stack gap="1" flex="1" maxWidth="100%">
              {toast.title ? <Toast.Title>{toast.title}</Toast.Title> : null}
              {toast.description ? (
                <Toast.Description whiteSpace="pre-wrap">{toast.description}</Toast.Description>
              ) : null}
            </Stack>
            {toast.action ? <Toast.ActionTrigger>{toast.action.label}</Toast.ActionTrigger> : null}
            {toast.closable !== false ? <Toast.CloseTrigger /> : null}
          </Toast.Root>
        )}
      </ChakraToaster>
    </Portal>
  );
}
