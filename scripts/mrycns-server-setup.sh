#!/bin/bash
# MRYCNSStudio - Oracle Cloud (Ubuntu, ARM) first-boot setup for MoneyPrinterTurbo.
# Runs once as cloud-init user data. No API keys live in this file.
set -euxo pipefail
exec > >(tee -a /var/log/mrycns-setup.log) 2>&1

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ca-certificates curl git

# Docker
curl -fsSL https://get.docker.com | sh
usermod -aG docker ubuntu || true
systemctl enable --now docker

# 4 GB swap so video rendering doesn't run out of memory
if [ ! -f /swapfile ]; then
  fallocate -l 4G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# Project
APP_DIR=/opt/mrycns/MoneyPrinterTurbo
mkdir -p /opt/mrycns
if [ ! -d "$APP_DIR/.git" ]; then
  git clone --branch mrycns-setup https://github.com/mmrycnsstudio-svg/MoneyPrinterTurbo.git "$APP_DIR"
fi
cd "$APP_DIR"
[ -f config.toml ] || cp config.example.toml config.toml
mkdir -p storage
chown -R ubuntu:ubuntu /opt/mrycns

# Build and start (web UI on 127.0.0.1:8501, API on 127.0.0.1:8080 - not exposed to the internet)
docker compose up -d --build

echo "MRYCNS_SETUP_DONE" > /opt/mrycns/SETUP_DONE
