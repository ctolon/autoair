import { Menu } from "@chakra-ui/react";
import { Suspense, lazy } from "react";
import { useTranslation } from "react-i18next";
import { LuShieldCheck } from "react-icons/lu";

import { useMeta } from "../lifecycle/hooks";
import { canOpenAdmin, useAdmin } from "./store";

const AdminPanel = lazy(() => import("./AdminPanel"));

/** "Admin panel" item of the user menu (only for holders of `admin:panel`). */
export function AdminMenuItem() {
  const { t } = useTranslation("admin");
  const meta = useMeta();
  if (!canOpenAdmin(meta?.iam?.permissions)) return null;
  return (
    <>
      <Menu.Separator />
      <Menu.Item value="admin-panel" data-testid="open-admin-panel" onClick={() => useAdmin.getState().openPanel()}>
        <LuShieldCheck /> {t("menu")}
      </Menu.Item>
    </>
  );
}

/** Renders the (code-split) admin panel while it is open. */
export function AdminPanelHost() {
  const open = useAdmin((s) => s.open);
  if (!open) return null;
  return (
    <Suspense fallback={null}>
      <AdminPanel />
    </Suspense>
  );
}
