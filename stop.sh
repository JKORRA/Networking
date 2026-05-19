#!/bin/bash
# ============================================================================
#  Hybrid Digital Twin for SDN Networks - Shutdown Script
# ============================================================================
#  Usage:  sudo ./stop.sh
# ============================================================================

SESSION="sdn-twin"
DOCKER_IMAGE="my-ryu"

RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m'

echo ""
echo -e "${RED}============================================${NC}"
echo -e "${RED}  SDN Digital Twin - Shutting Down${NC}"
echo -e "${RED}============================================${NC}"
echo ""

echo -e "${CYAN}[1/5]${NC} Killing tmux session..."
tmux kill-session -t $SESSION 2>/dev/null && echo "  Done" || echo "  No session found"

echo -e "${CYAN}[2/5]${NC} Stopping Ryu Docker containers..."
docker ps -q --filter "ancestor=${DOCKER_IMAGE}" | xargs -r docker stop 2>/dev/null
echo "  Done"

echo -e "${CYAN}[3/5]${NC} Cleaning up Mininet..."
mn -c 2>/dev/null
echo "  Done"

echo -e "${CYAN}[4/5]${NC} Freeing ports..."
for port in 6633 6634 8080 8081 5000; do
    fuser -k ${port}/tcp 2>/dev/null || true
done
echo "  Done"

echo -e "${CYAN}[5/5]${NC} Killing stray processes..."
pkill -f "ryu-manager" 2>/dev/null || true
pkill -f "dashboard.py" 2>/dev/null || true
pkill -f "iperf" 2>/dev/null || true
pkill -f "iperf3" 2>/dev/null || true
echo "  Done"

echo ""
echo -e "${GREEN}All components stopped. Clean shutdown complete.${NC}"
echo ""