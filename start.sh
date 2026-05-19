#!/bin/bash
# ============================================================================
#  Hybrid Digital Twin for SDN Networks - Launcher Script
# ============================================================================
#  Usage:  sudo ./start.sh
#  Stop:   sudo ./stop.sh
# ============================================================================

set -e

SESSION="sdn-twin"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- Configuration (mirrors config.py) ---
PHYSICAL_CTRL_PORT=6633
PHYSICAL_API_PORT=8080
TWIN_CTRL_PORT=6634
TWIN_API_PORT=8081
DASHBOARD_PORT=5000
DOCKER_IMAGE="my-ryu"
DOCKERFILE="Dockerfile.ryu"

# --- Readiness probe settings ---
MAX_WAIT=60
POLL_INTERVAL=1

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

print_step() { echo -e "${CYAN}[STEP]${NC} $1"; }
print_ok()   { echo -e "${GREEN}[  OK]${NC} $1"; }
print_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
print_err()  { echo -e "${RED}[FAIL]${NC} $1"; }

# --- Readiness probe function ---
wait_for_port() {
    local port=$1
    local service=$2
    local elapsed=0

    while [ $elapsed -lt $MAX_WAIT ]; do
        if fuser "${port}/tcp" &>/dev/null; then
            print_ok "${service} is ready (port ${port})"
            return 0
        fi
        sleep $POLL_INTERVAL
        elapsed=$((elapsed + POLL_INTERVAL))
    done

    print_warn "${service} did not become ready within ${MAX_WAIT}s (port ${port})"
    return 1
}

# ============================================================================
#  Pre-flight checks
# ============================================================================

echo ""
echo -e "${CYAN}============================================${NC}"
echo -e "${CYAN}  SDN Digital Twin - System Launcher${NC}"
echo -e "${CYAN}============================================${NC}"
echo ""

if [ "$EUID" -ne 0 ]; then
    print_err "This script must be run as root (sudo ./start.sh)"
    exit 1
fi

print_step "Checking dependencies..."

MISSING=0
for cmd in tmux docker python3 mn ovs-vsctl; do
    if ! command -v $cmd &> /dev/null; then
        print_err "$cmd is not installed"
        MISSING=1
    else
        print_ok "$cmd found"
    fi
done

if [ $MISSING -eq 1 ]; then
    echo ""
    print_err "Missing dependencies. Install them with:"
    echo "  sudo apt install tmux docker.io mininet openvswitch-switch"
    exit 1
fi

if ! command -v iperf3 &> /dev/null; then
    print_warn "iperf3 not installed (traffic emulation won't work)"
    print_warn "Install with: sudo apt install iperf3"
fi

VENV_PYTHON="$PROJECT_DIR/venv/bin/python3"
if [ -f "$VENV_PYTHON" ] && "$VENV_PYTHON" -c "import flask" 2>/dev/null; then
    print_ok "Flask found (venv)"
    DASHBOARD_PYTHON="$VENV_PYTHON"
elif python3 -c "import flask" 2>/dev/null; then
    print_ok "Flask found (system)"
    DASHBOARD_PYTHON="python3"
else
    print_warn "Flask not installed. Dashboard won't work."
    print_warn "Install with: ./venv/bin/pip install flask"
    DASHBOARD_PYTHON="python3"
fi

# Verify venv has all required packages
if [ "$DASHBOARD_PYTHON" = "$VENV_PYTHON" ]; then
    if ! "$VENV_PYTHON" -c "import flask_socketio" 2>/dev/null; then
        print_warn "Missing venv dependencies. Installing..."
        "$VENV_PYTHON" -m pip install -r "$PROJECT_DIR/requirements.txt" --quiet
        print_ok "Dependencies installed"
    else
        print_ok "All venv dependencies verified"
    fi
fi

if ! docker image inspect $DOCKER_IMAGE &> /dev/null; then
    print_step "Building Ryu Docker image (first time only)..."
    docker build -t $DOCKER_IMAGE -f "$PROJECT_DIR/$DOCKERFILE" "$PROJECT_DIR"
    print_ok "Docker image '${DOCKER_IMAGE}' built successfully"
else
    print_ok "Docker image '${DOCKER_IMAGE}' already exists"
fi

# ============================================================================
#  Cleanup
# ============================================================================

print_step "Cleaning up previous sessions..."

tmux kill-session -t $SESSION 2>/dev/null || true
docker ps -q --filter "ancestor=${DOCKER_IMAGE}" | xargs -r docker stop 2>/dev/null || true
mn -c 2>/dev/null || true
service openvswitch-switch start 2>/dev/null || true

fuser -k ${PHYSICAL_CTRL_PORT}/tcp 2>/dev/null || true
fuser -k ${TWIN_CTRL_PORT}/tcp 2>/dev/null || true
fuser -k ${PHYSICAL_API_PORT}/tcp 2>/dev/null || true
fuser -k ${TWIN_API_PORT}/tcp 2>/dev/null || true
fuser -k ${DASHBOARD_PORT}/tcp 2>/dev/null || true

sleep 1
print_ok "Cleanup complete"

# ============================================================================
#  Launch all components in tmux
# ============================================================================

print_step "Starting tmux session '${SESSION}'..."

# 1. Physical Ryu Controller FIRST (must be ready before Mininet connects)
tmux new-session -d -s $SESSION -n "SDN-Twin" -c "$PROJECT_DIR"
tmux send-keys -t $SESSION "echo '=== PHYSICAL RYU CONTROLLER ===' && docker run --rm --network host -v \"$PROJECT_DIR\":/app -w /app ${DOCKER_IMAGE} ryu-manager --observe-links controller.py" C-m

wait_for_port $PHYSICAL_CTRL_PORT "Physical Ryu OpenFlow" || true
wait_for_port $PHYSICAL_API_PORT "Physical Ryu REST API" || true
sleep 3

# 2. Physical Network (connects to already-running controller)
tmux split-window -v -t $SESSION -c "$PROJECT_DIR"
tmux send-keys -t $SESSION "echo '=== PHYSICAL NETWORK ===' && python3 net.py" C-m

sleep 5

# 3. Twin Ryu Controller
tmux split-window -v -t $SESSION -c "$PROJECT_DIR"
tmux send-keys -t $SESSION "echo '=== TWIN RYU CONTROLLER ===' && docker run --rm --network host -v \"$PROJECT_DIR\":/app -w /app ${DOCKER_IMAGE} ryu-manager --observe-links --wsapi-port ${TWIN_API_PORT} --ofp-tcp-listen-port ${TWIN_CTRL_PORT} controller.py" C-m

wait_for_port $TWIN_CTRL_PORT "Twin Ryu OpenFlow" || true
wait_for_port $TWIN_API_PORT "Twin Ryu REST API" || true

sleep 5

# 4. Digital Twin (fetches from now-ready physical controller)
tmux split-window -v -t $SESSION -c "$PROJECT_DIR"
tmux send-keys -t $SESSION "echo '=== DIGITAL TWIN ===' && python3 twin.py --sync" C-m

sleep 10

# 5. Dashboard
tmux new-window -t $SESSION -n "Dashboard" -c "$PROJECT_DIR"
tmux send-keys -t $SESSION:Dashboard "echo '=== WEB DASHBOARD ===' && \"$DASHBOARD_PYTHON\" dashboard.py" C-m

wait_for_port $DASHBOARD_PORT "Dashboard" || true

tmux select-window -t $SESSION:0

# ============================================================================
#  Done
# ============================================================================

echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  All components started successfully!${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
echo -e "  ${CYAN}Dashboard:${NC}  http://localhost:${DASHBOARD_PORT}"
echo ""
echo -e "  ${CYAN}Tmux session:${NC}  $SESSION"
echo -e "  ${CYAN}Attach with:${NC}   tmux attach -t $SESSION"
echo ""
echo -e "  ${YELLOW}Pane layout:${NC}"
echo -e "    ┌──────────────────┬──────────────────┐"
echo -e "    │  Physical Net    │  Physical Ctrl   │"
echo -e "    │  (net.py)        │  (Ryu Docker)    │"
echo -e "    ├──────────────────┼──────────────────┤"
echo -e "    │  Digital Twin    │  Twin Ctrl       │"
echo -e "    │  (twin.py)       │  (Ryu Docker)    │"
echo -e "    └──────────────────┴──────────────────┘"
echo -e "    Tab 2: Dashboard (Flask)"
echo ""
echo -e "  ${YELLOW}Quick test:${NC}"
echo -e "    1. Attach to tmux:  tmux attach -t $SESSION"
echo -e "    2. Select physical net pane: Ctrl+B then 0"
echo -e "    3. Run:  pingall"
echo -e "    4. Run:  h2 iperf -s &"
echo -e "    5. Run:  h1 iperf -c 10.0.0.2 -t 30 &"
echo -e "    6. Open browser: http://localhost:${DASHBOARD_PORT}"
echo ""
echo -e "  ${RED}To stop everything:${NC}"
echo -e "    sudo ./stop.sh"
echo ""
echo -e "  ${YELLOW}Tmux Navigation:${NC}"
echo -e "    Switch pane:    Ctrl+B  then  Arrow Keys"
echo -e "    Switch tab:     Ctrl+B  then  n / p"
echo -e "    Detach:         Ctrl+B  then  d"
echo -e "    Re-attach:      tmux attach -t $SESSION"
echo ""

tmux attach -t $SESSION

if ! tmux has-session -t $SESSION 2>/dev/null; then
    echo ""
    echo -e "${YELLOW}Tmux session closed. Running automatic cleanup...${NC}"
    "$PROJECT_DIR/stop.sh"
else
    echo ""
    echo -e "${YELLOW}Detached from tmux session. It is still running in the background.${NC}"
    echo -e "To re-attach:  tmux attach -t $SESSION"
    echo -e "To stop:       sudo ./stop.sh"
    echo ""
fi