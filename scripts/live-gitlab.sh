#!/usr/bin/env bash
# A throwaway GitLab CE (Docker, container autoair-gitlab, http://localhost:8929) with a
# personal access token and an initialised project for tests/live/test_gitlab.py.
# Prints the environment variables to export. First boot takes a few minutes.
set -euo pipefail
PORT="${GITLAB_PORT:-8929}"
IMAGE="${GITLAB_IMAGE:-gitlab/gitlab-ce:18.4.1-ce.0}"
TOKEN="${GITLAB_TEST_TOKEN:-glpat-autoair-live-test-0001}"
if ! docker ps --format '{{.Names}}' | grep -qx autoair-gitlab; then
  docker rm -f autoair-gitlab >/dev/null 2>&1 || true
  docker run -d --name autoair-gitlab --shm-size 256m -p "${PORT}:${PORT}" \
    -e GITLAB_ROOT_PASSWORD='AutoAir-live-Passw0rd!' \
    -e GITLAB_OMNIBUS_CONFIG="external_url 'http://localhost:${PORT}'; prometheus_monitoring['enable'] = false; registry['enable'] = false; gitlab_kas['enable'] = false; puma['worker_processes'] = 0; sidekiq['concurrency'] = 5" \
    "$IMAGE" >/dev/null
fi
echo "waiting for GitLab…" >&2
for _ in $(seq 1 180); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${PORT}/users/sign_in")" = 200 ] && break
  sleep 5
done
docker exec autoair-gitlab gitlab-rails runner "
  u = User.find_by_username('root')
  t = u.personal_access_tokens.find_by(name: 'autoair-live') || u.personal_access_tokens.create!(
    name: 'autoair-live', scopes: [:api], expires_at: 30.days.from_now)
  t.set_token('${TOKEN}'); t.save!
" >/dev/null
curl -sf -o /dev/null -H "PRIVATE-TOKEN: ${TOKEN}" "http://localhost:${PORT}/api/v4/projects/root%2Fautoair-dags" ||
  curl -sf -o /dev/null -H "PRIVATE-TOKEN: ${TOKEN}" -X POST "http://localhost:${PORT}/api/v4/projects" \
    -d name=autoair-dags -d initialize_with_readme=true -d default_branch=main
echo "export AUTOAIR_LIVE_GITLAB_URL=http://localhost:${PORT} AUTOAIR_LIVE_GITLAB_TOKEN=${TOKEN} AUTOAIR_LIVE_GITLAB_PROJECT=root/autoair-dags"
