#!/usr/bin/env bash
# deploy.sh — Copy rp_pll source to the Red Pitaya board and compile it.
#
# Usage: ./deploy.sh <board-ip-or-hostname>
# Example: ./deploy.sh rp-f05a6b.local
#          ./deploy.sh 192.168.1.50

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: $0 <board-ip-or-hostname>" >&2
    exit 1
fi

BOARD="$1"
REMOTE_DIR="/root/rp_pll"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10"

echo "==> Deploying to root@${BOARD}:${REMOTE_DIR}"

# 1. Create remote directory
ssh $SSH_OPTS "root@${BOARD}" "mkdir -p ${REMOTE_DIR}"

# 2. Copy source files
echo "==> Copying rp_pll.c and Makefile..."
scp $SSH_OPTS rp_pll.c Makefile "root@${BOARD}:${REMOTE_DIR}/"

# 3. Compile on board
echo "==> Building on board..."
ssh $SSH_OPTS "root@${BOARD}" "cd ${REMOTE_DIR} && make clean && make"

echo "==> Done. Run the PLL with:"
echo "    ssh root@${BOARD} '${REMOTE_DIR}/rp_pll [phase_deg] [duty] [port]'"
