#!/usr/bin/env bash
# DVR Kiosk -- in-place code update from GitHub.
#
#   curl -fsSL https://raw.githubusercontent.com/tahashemi/DVR-kiosk/main/update.sh | sudo bash
#
# Updates only the Python application code, wherever the running dvr-kiosk
# service actually loads it from (found from the service unit, so it works for
# both the standard /opt/dvr-kiosk install and older hand-built layouts).
#
# It never touches: configuration or data files (*.json, go2rtc.yaml),
# credentials, systemd units, go2rtc, or the dvrwall binary. Those are site
# state, not code. For a full (re)install use install.sh.
#
# Safety: every replaced file is backed up first; the new code is syntax-
# checked on this machine's interpreter before anything is swapped in; and if
# the service doesn't come back healthy, the backup is restored automatically.
#
# Options (env): BRANCH=main   REPO=tahashemi/DVR-kiosk
set -euo pipefail

REPO="${REPO:-tahashemi/DVR-kiosk}"
BRANCH="${BRANCH:-main}"
SERVICE="dvr-kiosk.service"

say()  { echo -e "\033[0;32m[update]\033[0m $*"; }
fail() { echo -e "\033[0;31m[update] $*\033[0m" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "run as root (pipe into sudo bash)"
command -v curl >/dev/null || fail "curl is required"

# ---- where does the running service load its code from? ----
EXEC=$(systemctl show -p ExecStart --value "$SERVICE" 2>/dev/null | sed -n 's/.*argv\[\]=\([^;]*\).*/\1/p')
SCRIPT=$(echo "$EXEC" | tr ' ' '\n' | grep -m1 'dvr_control\.py$' || true)
[ -n "$SCRIPT" ] || fail "can't find dvr_control.py in $SERVICE's ExecStart"
case "$SCRIPT" in
  /*) APP_DIR=$(dirname "$SCRIPT") ;;
  *)  WD=$(systemctl show -p WorkingDirectory --value "$SERVICE")
      APP_DIR=$(dirname "${WD%/}/$SCRIPT") ;;
esac
PY=$(echo "$EXEC" | awk '{print $1}')
[ -x "$PY" ] || PY=$(command -v python3)
[ -d "$APP_DIR" ] || fail "app directory $APP_DIR does not exist"
say "service code lives in $APP_DIR (python: $PY)"

# ---- fetch the code (public tarball: no git, no credentials) ----
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
say "downloading $REPO@$BRANCH ..."
curl -fsSL --retry 2 "https://codeload.github.com/${REPO}/tar.gz/refs/heads/${BRANCH}" \
  | tar -xz -C "$TMP" --strip-components=1 || fail "download failed"
NEW="$TMP/src"
[ -f "$NEW/dvr_control.py" ] || fail "downloaded archive has no src/dvr_control.py"

# ---- syntax-check everything against THIS interpreter before touching anything ----
"$PY" - "$NEW" <<'EOF' || fail "new code failed the syntax check -- nothing was changed"
import ast, pathlib, sys
for p in sorted(pathlib.Path(sys.argv[1]).glob("*.py")):
    ast.parse(p.read_text(encoding="utf-8"), str(p))
print("[update] syntax ok")
EOF

# ---- anything to do? ----
CHANGED=()
for f in "$NEW"/*.py; do
  b=$(basename "$f")
  if ! cmp -s "$f" "$APP_DIR/$b"; then CHANGED+=("$b"); fi
done
if [ ${#CHANGED[@]} -eq 0 ]; then
  say "already up to date"
  exit 0
fi
say "updating: ${CHANGED[*]}"

# ---- back up, swap in atomically, restart ----
BK="$APP_DIR/.update-backup/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BK"
for b in "${CHANGED[@]}"; do
  [ -f "$APP_DIR/$b" ] && cp -p "$APP_DIR/$b" "$BK/$b"
  install -m 0644 "$NEW/$b" "$APP_DIR/.$b.new"
  mv -f "$APP_DIR/.$b.new" "$APP_DIR/$b"
done
echo "${CHANGED[*]}" > "$BK/FILES"

rollback() {
  echo -e "\033[0;31m[update] $1 -- rolling back\033[0m" >&2
  for b in "${CHANGED[@]}"; do
    if [ -f "$BK/$b" ]; then cp -p "$BK/$b" "$APP_DIR/$b"; else rm -f "$APP_DIR/$b"; fi
  done
  systemctl restart "$SERVICE"
  fail "rolled back to the previous version (backup kept in $BK)"
}

say "restarting $SERVICE ..."
systemctl restart "$SERVICE"

# Healthy = service active and the web UI answering (sign-in page counts).
for _ in $(seq 1 20); do
  sleep 1
  if systemctl is-active --quiet "$SERVICE"; then
    code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' http://127.0.0.1/ || true)
    if [ "$code" = "200" ] || [ "$code" = "302" ]; then
      say "done -- web UI is answering (HTTP $code). Backup: $BK"
      # Keep the five most recent backups.
      ls -1dt "$APP_DIR/.update-backup"/*/ 2>/dev/null | tail -n +6 | xargs -r rm -rf
      exit 0
    fi
  fi
done
rollback "service did not come back healthy within 20 s"
