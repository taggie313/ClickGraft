#!/usr/bin/env bash
# Provision the clickgraft CT. Run ON the PVE node, once.
#
#   ssh <pve-node> 'bash -s' < site/deploy/setup-ct.sh
#
# Follows the house pattern: unprivileged Debian CT with nesting+keyctl so
# Docker works untuned, DHCP on vmbr0, onboot. Everything after this is
# deploy/redeploy.sh, which never ssh's into the CT directly.
set -euo pipefail

CT_ID="${CT_ID:-117}"
HOSTNAME="${HOSTNAME_:-clickgraft}"
STORAGE="${STORAGE:-rpool-ssd}"
DISK_GB="${DISK_GB:-8}"
MEMORY="${MEMORY:-1024}"
CORES="${CORES:-1}"
TEMPLATE_STORE="${TEMPLATE_STORE:-local}"

if pct status "$CT_ID" >/dev/null 2>&1; then
  echo "CT $CT_ID already exists — nothing to do."
  exit 0
fi

echo "==> finding a Debian template"
pveam update >/dev/null 2>&1 || true
TMPL=$(pveam list "$TEMPLATE_STORE" 2>/dev/null | awk '/debian-1[23]-standard/ {print $1}' | sort | tail -1)
if [ -z "$TMPL" ]; then
  AVAIL=$(pveam available --section system | awk '/debian-1[23]-standard/ {print $2}' | sort | tail -1)
  echo "    downloading $AVAIL"
  pveam download "$TEMPLATE_STORE" "$AVAIL"
  TMPL="${TEMPLATE_STORE}:vztmpl/${AVAIL}"
fi
echo "    $TMPL"

echo "==> creating CT $CT_ID ($HOSTNAME)"
pct create "$CT_ID" "$TMPL" \
  --hostname "$HOSTNAME" \
  --cores "$CORES" --memory "$MEMORY" --swap 512 \
  --rootfs "${STORAGE}:${DISK_GB}" \
  --features nesting=1,keyctl=1 \
  --unprivileged 1 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp,firewall=1 \
  --onboot 1 \
  --description "ClickGraft download site — clickgraft.elusive.net via cloudflared. Deploy with site/deploy/redeploy.sh from the ClickGraft repo."

pct start "$CT_ID"

echo "==> waiting for network"
for _ in $(seq 1 30); do
  pct exec "$CT_ID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
  sleep 2
done

echo "==> installing Docker"
pct exec "$CT_ID" -- sh -lc '
  set -e
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl gnupg logrotate >/dev/null
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg \
    -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $VERSION_CODENAME stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
  systemctl enable --now docker
'

# No log rotation on purpose. This is a single static page; the access log runs
# to a few lines a day, so rotation only ever cost something — it split the
# history across two files and made "is that number real" harder to answer.
# If it ever does grow, rotate then.

pct exec "$CT_ID" -- mkdir -p /opt/clickgraft

IP=$(pct exec "$CT_ID" -- hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "CT $CT_ID ready at ${IP:-<dhcp pending>}"
echo "Docker: $(pct exec "$CT_ID" -- docker --version)"
echo
echo "Next: put CLOUDFLARE_TUNNEL_TOKEN in /opt/clickgraft/.env, then run deploy/redeploy.sh"
