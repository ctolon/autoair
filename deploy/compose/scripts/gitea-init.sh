#!/bin/sh
# One-shot (Gitea image, next to Gitea's data): create the demo admin user if missing.
set -eu
if gitea admin user list --admin 2>/dev/null | awk 'NR>1 {print $2}' | grep -qx "$GITEA_ADMIN_USER"; then
  echo "gitea user $GITEA_ADMIN_USER exists"
  exit 0
fi
gitea admin user create --admin --username "$GITEA_ADMIN_USER" \
  --password "$GITEA_ADMIN_PASSWORD" --email "$GITEA_ADMIN_USER@demo.invalid" \
  --must-change-password=false
echo "gitea user $GITEA_ADMIN_USER created"
