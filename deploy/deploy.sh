#!/usr/bin/env bash
# Deploy Clip Studio from this repo. Safe to re-run.
#   backend -> /opt/clipstudio (root-owned; runs as user clipstudio)
#   web     -> /srv/clipstudio (static, served by Caddy)
#   data    -> /var/lib/clipstudio (own SQLite DB + work files; PocketBase is NOT touched: login is read-only)
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="$(git rev-parse --short HEAD 2>/dev/null || echo dev)-$(date +%Y%m%d%H%M%S)"
echo "==> clipstudio $VERSION"
# Never cut running work short: wait (before changing ANY file) until the worker has no job running and no post
# being uploaded. If it stays busy (e.g. a long live recording), stop here without changes. FORCE=1 skips the wait.
busy() {
  [ -x /opt/clipstudio/venv/bin/python ] || return 1
  sudo -u clipstudio env CS_STATE_DIR=/var/lib/clipstudio PYTHONPATH=/opt/clipstudio /opt/clipstudio/venv/bin/python -c "
from cs import db
j = db.one(\"SELECT COUNT(*) n FROM jobs WHERE status='running'\")['n']
p = db.one(\"SELECT COUNT(*) n FROM posts WHERE status IN ('uploading','processing')\")['n']
print(f'{j} job(s) running, {p} post(s) uploading')
raise SystemExit(0 if j or p else 1)" 2>/dev/null
}
if [ "${FORCE:-0}" != 1 ] && systemctl is-active --quiet clipstudio-worker; then
  for i in $(seq "$(( ${WAIT_MIN:-15} * 4 ))"); do
    msg="$(busy)" || break
    [ "$i" = 1 ] && echo "==> worker busy ($msg): waiting for it to finish (up to ${WAIT_MIN:-15} min)…"
    sleep 15
  done
  if msg="$(busy)"; then
    echo "!! worker still busy ($msg). Nothing was changed. Deploy again later (or FORCE=1)."
    exit 1
  fi
fi

command -v ffmpeg >/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends ffmpeg
command -v deno >/dev/null || { echo "!! deno missing: install it to /usr/local/bin (see README)"; exit 1; }
id clipstudio >/dev/null 2>&1 || sudo useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin clipstudio

echo "==> backend"
sudo install -d -o root -g root -m 755 /opt/clipstudio
[ -x /opt/clipstudio/venv/bin/python ] || sudo python3 -m venv /opt/clipstudio/venv
sudo /opt/clipstudio/venv/bin/pip install --quiet --disable-pip-version-check -r backend/requirements.txt
sudo rsync -a --delete --chown=root:root --chmod=D755,F644 --exclude __pycache__ backend/cs/ /opt/clipstudio/cs/
for u in clipstudio.service clipstudio-worker.service clipstudio-ytdlp-update.service clipstudio-ytdlp-update.timer; do
  sudo install -o root -g root -m 644 "deploy/$u" "/etc/systemd/system/$u"
done

echo "==> web"
STAGE="$(mktemp -d)"
trap 'rm -rf -- "$STAGE"' EXIT
cp -r web/. "$STAGE/"
{ grep -rl __VERSION__ "$STAGE" --include=*.js --include=*.html --include=*.css || true; } | xargs -r sed -i "s/__VERSION__/$VERSION/g"
sudo install -d -o root -g root -m 755 /srv/clipstudio
sudo rsync -a --delete --chown=root:root --chmod=D755,F644 "$STAGE/" /srv/clipstudio/

echo "==> secrets"
sudo chown root:clipstudio /etc/clipstudio/env && sudo chmod 640 /etc/clipstudio/env
if sudo test -f /etc/clipstudio/youtube-cookies.txt; then
  sudo chown root:clipstudio /etc/clipstudio/youtube-cookies.txt && sudo chmod 640 /etc/clipstudio/youtube-cookies.txt
fi
sudo chmod 755 /etc/clipstudio

echo "==> caddy"
if ! sudo grep -q "^clips.bashir.my.id" /etc/caddy/Caddyfile; then
  BAK="$HOME/work/Caddyfile.bak-$(date +%Y%m%d-%H%M%S)"
  sudo cp /etc/caddy/Caddyfile "$BAK" && sudo chown "$USER" "$BAK"
  { echo; cat deploy/Caddyfile.clipstudio; } | sudo tee -a /etc/caddy/Caddyfile >/dev/null
  if sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
    sudo systemctl reload caddy && echo "   added clips.bashir.my.id (backup: $BAK)"
  else
    sudo cp "$BAK" /etc/caddy/Caddyfile && echo "!! Caddyfile invalid, restored $BAK" && exit 1
  fi
fi

echo "==> restart"
sudo systemctl daemon-reload
sudo systemctl enable --quiet clipstudio clipstudio-worker clipstudio-ytdlp-update.timer
sudo systemctl start clipstudio-ytdlp-update.timer
sudo systemctl restart clipstudio
for i in $(seq 30); do curl -fsS http://127.0.0.1:8400/api/health >/dev/null 2>&1 && break; sleep 1; done
curl -fsS http://127.0.0.1:8400/api/health && echo
sudo systemctl restart clipstudio-worker
sleep 3
systemctl is-active clipstudio clipstudio-worker caddy pocketbase finance lyrsync shop-web shop-admin bashgames
echo "==> done: $VERSION"
