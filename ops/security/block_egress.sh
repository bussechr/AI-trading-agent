#!/usr/bin/env bash
#
# block_egress.sh -- host-level egress lockdown for the offline model stack.
#
# After the offline docker stack is set up and model weights are pre-staged
# (see fx-quant-stack/docker/docker-compose.offline.yml), this script blocks ALL
# outbound traffic from the host except loopback. It is the host-level "defence
# in depth" layer on top of the compose `internal: true` network.
#
# SAFETY: the default mode is DRY-RUN -- it only prints the firewall commands it
# *would* run. You must pass --apply to actually change firewall rules. Run it
# as root (or via sudo) when applying.
#
# Backends: prefers nftables (nft); falls back to iptables. Loopback (lo / 127/8
# / ::1) is always allowed, as is already-established/related return traffic so
# in-progress local sessions are not torn down.
#
# Usage:
#   ops/security/block_egress.sh                 # dry-run, show planned rules
#   ops/security/block_egress.sh --apply         # apply (needs root)
#   ops/security/block_egress.sh --backend iptables --apply
#   ops/security/block_egress.sh --allow-cidr 10.0.0.0/24   # extra allowed dst
#   ops/security/block_egress.sh --help
#
# Idempotency: applying twice is safe -- the nft path replaces a dedicated table
# and the iptables path flushes the OUTPUT chain before re-adding rules.

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults / argument parsing
# ---------------------------------------------------------------------------
APPLY=0
BACKEND="auto"          # auto | nft | iptables
declare -a EXTRA_ALLOW=()  # additional destination CIDRs to permit (egress)

usage() {
  sed -n '2,/^set -euo/{/^set -euo/!p}' "$0"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --apply)        APPLY=1; shift ;;
    --dry-run)      APPLY=0; shift ;;
    --backend)      BACKEND="${2:?--backend needs a value}"; shift 2 ;;
    --backend=*)    BACKEND="${1#*=}"; shift ;;
    --allow-cidr)   EXTRA_ALLOW+=("${2:?--allow-cidr needs a value}"); shift 2 ;;
    --allow-cidr=*) EXTRA_ALLOW+=("${1#*=}"); shift ;;
    -h|--help)      usage; exit 0 ;;
    *) echo "block_egress.sh: unknown argument '$1' (try --help)" >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# run CMD... : execute when --apply is set, otherwise just echo it (dry-run).
run() {
  if [ "$APPLY" -eq 1 ]; then
    "$@"
  else
    printf 'DRY-RUN: %s\n' "$*"
  fi
}

have() { command -v "$1" >/dev/null 2>&1; }

resolve_backend() {
  case "$BACKEND" in
    nft)      echo "nft" ;;
    iptables) echo "iptables" ;;
    auto)
      if have nft; then echo "nft"
      elif have iptables; then echo "iptables"
      else
        echo "block_egress.sh: neither nft nor iptables found" >&2
        exit 1
      fi
      ;;
    *) echo "block_egress.sh: invalid --backend '$BACKEND'" >&2; exit 2 ;;
  esac
}

require_root_when_applying() {
  if [ "$APPLY" -eq 1 ] && [ "$(id -u)" -ne 0 ]; then
    echo "block_egress.sh: --apply requires root (re-run with sudo)" >&2
    exit 1
  fi
}

# ---------------------------------------------------------------------------
# nftables backend
# ---------------------------------------------------------------------------
# Builds a single atomic ruleset in a dedicated 'egress_lock' table so a re-run
# cleanly replaces prior state without touching other tables.
apply_nft() {
  local extra_rules=""
  local cidr
  for cidr in "${EXTRA_ALLOW[@]:-}"; do
    [ -n "$cidr" ] || continue
    extra_rules+="        ip daddr ${cidr} accept"$'\n'
  done

  # NOTE: heredoc is expanded so ${extra_rules} is inlined; the nft syntax keeps
  # loopback + established/related open and drops everything else outbound.
  local ruleset
  ruleset=$(cat <<NFT
table inet egress_lock {
  chain output {
    type filter hook output priority 0; policy drop;

    # Always allow loopback traffic.
    oif "lo" accept
    ip daddr 127.0.0.0/8 accept
    ip6 daddr ::1 accept

    # Allow return traffic for already-open local connections.
    ct state established,related accept

${extra_rules}    # Everything else outbound is dropped by the chain policy above.
  }
}
NFT
)

  if [ "$APPLY" -eq 1 ]; then
    # Replace any prior lock table atomically.
    nft delete table inet egress_lock 2>/dev/null || true
    printf '%s\n' "$ruleset" | nft -f -
    echo "block_egress.sh: nftables egress lock applied."
  else
    echo "DRY-RUN: would load the following nftables ruleset:"
    printf '%s\n' "$ruleset" | sed 's/^/DRY-RUN: | /'
  fi
}

# ---------------------------------------------------------------------------
# iptables backend
# ---------------------------------------------------------------------------
apply_iptables() {
  # Flush the OUTPUT chain so a re-run is idempotent, then build allow rules
  # before flipping the default policy to DROP.
  run iptables -F OUTPUT
  run iptables -A OUTPUT -o lo -j ACCEPT
  run iptables -A OUTPUT -d 127.0.0.0/8 -j ACCEPT
  run iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

  local cidr
  for cidr in "${EXTRA_ALLOW[@]:-}"; do
    [ -n "$cidr" ] || continue
    run iptables -A OUTPUT -d "$cidr" -j ACCEPT
  done

  run iptables -P OUTPUT DROP

  # IPv6: allow loopback, drop the rest (skip silently if ip6tables is absent).
  if have ip6tables; then
    run ip6tables -F OUTPUT
    run ip6tables -A OUTPUT -o lo -j ACCEPT
    run ip6tables -A OUTPUT -d ::1 -j ACCEPT
    run ip6tables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
    run ip6tables -P OUTPUT DROP
  fi

  if [ "$APPLY" -eq 1 ]; then
    echo "block_egress.sh: iptables egress lock applied."
  fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  require_root_when_applying
  local backend
  backend="$(resolve_backend)"

  if [ "$APPLY" -eq 0 ]; then
    echo "block_egress.sh: DRY-RUN (no changes). Pass --apply to enforce."
  fi
  echo "block_egress.sh: backend=${backend} apply=${APPLY}"

  case "$backend" in
    nft)      apply_nft ;;
    iptables) apply_iptables ;;
  esac
}

main "$@"
