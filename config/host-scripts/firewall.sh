#!/usr/bin/env bash
set -euo pipefail

# This script configures UFW on the manager and pins the Swarm data-path address
# to the manager's VPC IP (10.124.0.0/20). Run as root on the manager.

VPC_CIDR="10.124.0.0/20"

# Detect interfaces
VPC_IFACE=$(ip -o -4 addr show | awk '$4 ~ /10\.124\./ {print $2; exit 0}')
if [[ -z "${VPC_IFACE:-}" ]]; then
  echo "ERROR: Could not find an interface in ${VPC_CIDR}" >&2
  exit 1
fi
MANAGER_VPC_IP=$(ip -o -4 addr show dev "${VPC_IFACE}" | awk '{print $4}' | cut -d/ -f1 | head -n1)

# Try to guess a "public" iface (best-effort; may equal VPC_IFACE if single-NIC)
PUBLIC_IFACE=$(ip -o link show | awk -F': ' '$2 ~ /^e(th|np|ns)/ {print $2}' | head -n1)

echo "Detected VPC iface: ${VPC_IFACE} (${MANAGER_VPC_IP})"
echo "Detected public iface: ${PUBLIC_IFACE:-<none>}"

# --- UFW ---

# Baseline policy
ufw default deny incoming
ufw default allow outgoing

# Allow SSH
ufw allow 22/tcp

# Allow swarm dataplane from VPC only (on VPC iface)
ufw allow in on "${VPC_IFACE}" from ${VPC_CIDR} to any port 7946 proto tcp
ufw allow in on "${VPC_IFACE}" from ${VPC_CIDR} to any port 7946 proto udp
ufw allow in on "${VPC_IFACE}" from ${VPC_CIDR} to any port 4789 proto udp

# Manager control plane (2377/tcp) from VPC only
ufw allow in on "${VPC_IFACE}" from ${VPC_CIDR} to any port 2377 proto tcp

# Explicitly deny on the public iface (if distinct)
if [[ -n "${PUBLIC_IFACE:-}" ]]; then
  ufw deny in on "${PUBLIC_IFACE}" to any port 7946 proto tcp || true
  ufw deny in on "${PUBLIC_IFACE}" to any port 7946 proto udp || true
  ufw deny in on "${PUBLIC_IFACE}" to any port 4789 proto udp || true
  ufw deny in on "${PUBLIC_IFACE}" to any port 2377 proto tcp || true
fi

ufw reload