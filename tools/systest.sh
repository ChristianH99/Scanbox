#!/usr/bin/env bash
# System test of a Scanbox image: real container + simulator + networked mocks (docker-compose.systest.yml).
#
#   tools/systest.sh                                  # image scanbox:dev (docker compose -f docker-compose.dev.yml build)
#   SCANBOX_IMAGE=ghcr.io/…/scanbox:sha-abc tools/systest.sh
#
# Needs the tools image (docker build --target test -t scanbox:test-tools .) and the simulator image.
# Phase 1 runs all functional tests and leaves a scan + an OCR job in flight, phase 2 SIGKILLs and
# restarts the app container, phase 3 checks recovery. Exit code ≠ 0 on any failure; logs are dumped.
set -euo pipefail
cd "$(dirname "$0")/.."
DC=(docker compose -f docker-compose.systest.yml)
EXTRA=("$@")

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "::group::app logs (last 200 lines)"
    "${DC[@]}" logs --no-color --tail 200 scanbox || true
    echo "::endgroup::"
  fi
  [ -n "${KEEP:-}" ] || "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

"${DC[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${DC[@]}" up -d --no-build scanbox
echo "waiting for the app …"
for _ in $(seq 1 90); do
  if "${DC[@]}" exec -T scanbox python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/readyz',timeout=3).status==200 else 1)" 2>/dev/null; then
    break
  fi
  sleep 2
done

echo "=== phase 1: functional tests ==="
rc=0
"${DC[@]}" run --rm systest -m "not after_restart" -q -rfE "${EXTRA[@]}" || rc=$?

echo "=== phase 2: SIGKILL + restart while a scan and an OCR job are running ==="
"${DC[@]}" kill -s KILL scanbox
"${DC[@]}" up -d --no-build scanbox
for _ in $(seq 1 90); do
  if "${DC[@]}" exec -T scanbox python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/readyz',timeout=3).status==200 else 1)" 2>/dev/null; then
    break
  fi
  sleep 2
done

echo "=== phase 3: recovery ==="
"${DC[@]}" run --rm systest -m after_restart -q -rfE "${EXTRA[@]}" || rc=$?
[ "$rc" -eq 0 ] || { echo "system test FAILED"; exit "$rc"; }
echo "system test passed"
