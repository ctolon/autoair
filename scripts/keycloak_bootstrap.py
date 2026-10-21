"""Create the realm/client/roles/users the Keycloak auth manager live test expects.

realm "airflow", confidential client "airflow" (authorization services + password grant),
realm roles Viewer/User/Op/Admin/SuperAdmin, users admin (Admin), editor (User),
viewer (Viewer) — each user's password is its name. Prints the client secret.
"""

from __future__ import annotations

import json
import sys

from keycloak import KeycloakAdmin

server, realm = sys.argv[1], sys.argv[2]
admin = KeycloakAdmin(
    server_url=server,
    username="admin",
    password="admin",  # noqa: S106 - throwaway local test Keycloak
    realm_name="master",
)
if realm not in [r["realm"] for r in admin.get_realms()]:
    admin.create_realm({"realm": realm, "enabled": True})
admin.change_current_realm(realm)

if not admin.get_client_id("airflow"):
    admin.create_client(
        {
            "clientId": "airflow",
            "enabled": True,
            "publicClient": False,
            "serviceAccountsEnabled": True,
            "authorizationServicesEnabled": True,
            "directAccessGrantsEnabled": True,
            "standardFlowEnabled": True,
            "redirectUris": ["*"],
        }
    )
client_uuid = admin.get_client_id("airflow")
# A request is allowed when ANY matching permission grants it (role permissions overlap).
settings = admin.get_client_authz_settings(client_uuid)
settings["decisionStrategy"] = "AFFIRMATIVE"
admin.connection.raw_put(
    f"admin/realms/{realm}/clients/{client_uuid}/authz/resource-server", data=json.dumps(settings)
)

for role in ("Viewer", "User", "Op", "Admin", "SuperAdmin"):
    admin.create_realm_role({"name": role}, skip_exists=True)

for user, role in (("admin", "Admin"), ("editor", "User"), ("viewer", "Viewer")):
    user_id = admin.get_user_id(user) or admin.create_user(
        {
            "username": user,
            "email": f"{user}@example.com",
            "firstName": user,
            "lastName": "live",
            "enabled": True,
            "emailVerified": True,
            "credentials": [{"type": "password", "value": user, "temporary": False}],
        }
    )
    admin.assign_realm_roles(user_id, [admin.get_realm_role(role)])

print(admin.get_client_secrets(client_uuid)["value"])
