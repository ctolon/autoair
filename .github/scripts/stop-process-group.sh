#!/usr/bin/env bash
# Stop a background server started as the leader of its own session
# (`setsid bash -c 'echo $$ > <pidfile>; exec <server>' &`): SIGTERM to its whole process
# group, then SIGKILL after 30 s. Missing or stale pid files are fine.
set -u
pidfile="${1:?usage: stop-process-group.sh <pidfile>}"
pid="$(cat "$pidfile" 2>/dev/null)" || exit 0
[ -n "$pid" ] || exit 0
kill -TERM -- "-$pid" 2>/dev/null || exit 0
for _ in $(seq 1 30); do
  kill -0 -- "-$pid" 2>/dev/null || exit 0
  sleep 1
done
kill -KILL -- "-$pid" 2>/dev/null || true
