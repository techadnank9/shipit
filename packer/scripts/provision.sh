#!/usr/bin/env bash
# Runs as root on the Packer builder (Amazon Linux 2023, arm64).
set -euxo pipefail

dnf -y update
dnf -y install docker amazon-cloudwatch-agent git jq

# ---- Docker CLI plugins: compose + buildx (AL2023 ships neither), checksum-verified ----
plugins=/usr/libexec/docker/cli-plugins
install -d "$plugins"
cd /tmp
curl -fsSLO "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-aarch64"
curl -fsSLO "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-aarch64.sha256"
sha256sum -c docker-compose-linux-aarch64.sha256
install -m 0755 docker-compose-linux-aarch64 "$plugins/docker-compose"

curl -fsSLO "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/buildx-${BUILDX_VERSION}.linux-arm64"
curl -fsSLO "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/checksums.txt"
grep " \*\?buildx-${BUILDX_VERSION}.linux-arm64$" checksums.txt | sed 's/ \*/  /' | sha256sum -c
install -m 0755 "buildx-${BUILDX_VERSION}.linux-arm64" "$plugins/docker-buildx"

# ---- gVisor (runsc) as an extra Docker runtime ----
gv="https://storage.googleapis.com/gvisor/releases/release/${GVISOR_RELEASE}/aarch64"
curl -fsSLO "$gv/runsc" -O "$gv/runsc.sha512" -O "$gv/containerd-shim-runsc-v1" -O "$gv/containerd-shim-runsc-v1.sha512"
sha512sum -c runsc.sha512 containerd-shim-runsc-v1.sha512
install -m 0755 runsc containerd-shim-runsc-v1 /usr/local/bin/
/usr/local/bin/runsc install   # adds "runsc" to /etc/docker/daemon.json runtimes

# ---- daemon settings: bounded logs, live-restore, chosen default runtime ----
tmp="$(mktemp)"
jq --arg rt "$DEFAULT_RUNTIME" '. + {
  "default-runtime": $rt,
  "live-restore": true,
  "log-driver": "json-file",
  "log-opts": {"max-size": "10m", "max-file": "3"}
}' /etc/docker/daemon.json > "$tmp"
install -m 0644 "$tmp" /etc/docker/daemon.json

systemctl enable --now docker
usermod -aG docker ec2-user
docker info --format '{{json .Runtimes}}' | grep -q runsc
docker compose version
docker buildx version

# ---- pre-pull the images every copy uses ----
for img in $PREPULL; do
  docker pull --platform linux/arm64 "$img"
done
docker run --rm --runtime=runsc curlimages/curl:8.16.0 --version   # gVisor smoke test

# ---- CloudWatch agent is configured by the launch template user data ----
systemctl enable amazon-cloudwatch-agent || true

# ---- cleanup ----
rm -f /tmp/docker-compose-* /tmp/buildx-* /tmp/checksums.txt /tmp/runsc* /tmp/containerd-shim-runsc-v1*
dnf clean all
rm -rf /var/cache/dnf
cloud-init clean --logs || true
