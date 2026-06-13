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

# Auto-generate auth token if not set
if [ -z "${SDN_TWIN_AUTH_TOKEN}" ]; then
    export SDN_TWIN_AUTH_TOKEN="Bearer $(tr -dc 'a-zA-Z0-9' < /dev/urandom | fold -w 32 | head -n 1)"
fi

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
wait_for_http() {
    local url=$1
    local service=$2
    local elapsed=0
    while [ $elapsed -lt $MAX_WAIT ]; do
        if curl -s -o /dev/null -w "%{http_code}" -H "Authorization: ${SDN_TWIN_AUTH_TOKEN}" "$url" 2>/dev/null | grep -q 200; then
            print_ok "${service} is ready"
            return 0
        fi
        sleep $POLL_INTERVAL
        elapsed=$((elapsed + POLL_INTERVAL))
    done
    print_warn "${service} did not become ready within ${MAX_WAIT}s"
    return 1
}

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

cleanup() {
    print_step "Cleaning up previous sessions and processes..."
    tmux kill-session -t $SESSION 2>/dev/null || true
    docker ps -q --filter "ancestor=${DOCKER_IMAGE}" | xargs -r docker stop 2>/dev/null || true
    # Always run mn -c to clean up stale veth interfaces, even if processes crashed
    mn -c 2>/dev/null || true
    service openvswitch-switch start 2>/dev/null || true

    for port in ${PHYSICAL_CTRL_PORT} ${TWIN_CTRL_PORT} ${PHYSICAL_API_PORT} ${TWIN_API_PORT} ${DASHBOARD_PORT}; do
        fuser -k "${port}/tcp" 2>/dev/null || true
    done
    
    pkill -f "ryu-manager" 2>/dev/null || true
    pkill -f "dashboard.py" 2>/dev/null || true
    pkill -f "iperf" 2>/dev/null || true
    pkill -f "iperf3" 2>/dev/null || true
    print_ok "Cleanup complete"
}

if [ "$1" = "--stop" ]; then
    if [ "$EUID" -ne 0 ]; then
        print_err "This script must be run as root (sudo ./start.sh --stop)"
        exit 1
    fi
    cleanup
    exit 0
fi

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

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
if [ "$(echo "$PYTHON_VERSION >= 3.12" | bc 2>/dev/null)" = "1" ] 2>/dev/null; then
    print_warn "Python $PYTHON_VERSION detected. Ryu requires Python < 3.12."
    print_warn "The Ryu controller runs in Docker but dashboard and twin run on the host."
    print_warn "If you encounter issues, use Python 3.11 or earlier."
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

# Build docker image in background if needed
docker image inspect $DOCKER_IMAGE &> /dev/null || {
    print_step "Building Ryu Docker image in background..."
    (docker build -t $DOCKER_IMAGE -f "$PROJECT_DIR/$DOCKERFILE" "$PROJECT_DIR" && print_ok "Docker image '${DOCKER_IMAGE}' built") &
    DOCKER_BUILD_PID=$!
}

# ============================================================================
#  Cleanup
# ============================================================================

cleanup

sleep 1
# Wait for Docker build if it was kicked off
if [ -n "$DOCKER_BUILD_PID" ]; then
    print_step "Waiting for Docker build to complete..."
    wait $DOCKER_BUILD_PID 2>/dev/null || print_err "Docker build failed"
fi

# ============================================================================
#  Launch all components in tmux
# ============================================================================

print_step "Starting tmux session '${SESSION}'..."

# 1. Physical Ryu Controller FIRST (must be ready before Mininet connects)
tmux new-session -d -s $SESSION -n "SDN-Twin" -c "$PROJECT_DIR"
tmux set-option -t $SESSION pane-border-status top
PANE_PHYS_CTRL=$(tmux display-message -p -t $SESSION -F "#{pane_id}")
tmux select-pane -t $PANE_PHYS_CTRL -T "Physical Ryu Controller"
tmux send-keys -t $PANE_PHYS_CTRL "echo '=== PHYSICAL RYU CONTROLLER ===' && docker run --rm --network host -v \"$PROJECT_DIR\":/app -w /app -e SDN_TWIN_AUTH_TOKEN=\"$SDN_TWIN_AUTH_TOKEN\" ${DOCKER_IMAGE} ryu-manager --observe-links controller.py" C-m

wait_for_http "http://localhost:${PHYSICAL_API_PORT}/api/version" "Physical Ryu" || true

# 2. Physical Network (connects to already-running controller)
tmux split-window -v -t $PANE_PHYS_CTRL -c "$PROJECT_DIR"
PANE_PHYS_NET=$(tmux display-message -p -t $SESSION -F "#{pane_id}")
tmux select-pane -t $PANE_PHYS_NET -T "Physical Network (Mininet)"
tmux send-keys -t $PANE_PHYS_NET "echo '=== PHYSICAL NETWORK ===' && python3 net.py" C-m

# Wait for physical network to be discovered (topology version >= 1)
print_step "Waiting for physical network topology..."
TOPOLOGY_READY=0
for i in $(seq 1 30); do
    VERSION=$(curl -s -H "Authorization: ${SDN_TWIN_AUTH_TOKEN}" "http://localhost:${PHYSICAL_API_PORT}/api/version" 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('version',0))" 2>/dev/null)
    if [ "$VERSION" -ge 1 ] 2>/dev/null; then
        print_ok "Physical network discovered (version $VERSION)"
        TOPOLOGY_READY=1
        break
    fi
    sleep 1
done
if [ "$TOPOLOGY_READY" -ne 1 ]; then
    print_warn "Physical network topology not fully discovered, continuing anyway..."
fi

# 3. Twin Ryu Controller
tmux split-window -h -t $PANE_PHYS_CTRL -c "$PROJECT_DIR"
PANE_TWIN_CTRL=$(tmux display-message -p -t $SESSION -F "#{pane_id}")
tmux select-pane -t $PANE_TWIN_CTRL -T "Twin Ryu Controller"
tmux send-keys -t $PANE_TWIN_CTRL "echo '=== TWIN RYU CONTROLLER ===' && docker run --rm --network host -v \"$PROJECT_DIR\":/app -w /app -e SDN_TWIN_AUTH_TOKEN=\"$SDN_TWIN_AUTH_TOKEN\" ${DOCKER_IMAGE} ryu-manager --observe-links --wsapi-port ${TWIN_API_PORT} --ofp-tcp-listen-port ${TWIN_CTRL_PORT} controller.py" C-m

wait_for_http "http://localhost:${TWIN_API_PORT}/api/version" "Twin Ryu" || true

# 4. Digital Twin (fetches from now-ready physical controller)
tmux split-window -h -t $PANE_PHYS_NET -c "$PROJECT_DIR"
PANE_TWIN_NET=$(tmux display-message -p -t $SESSION -F "#{pane_id}")
tmux select-pane -t $PANE_TWIN_NET -T "Digital Twin (Mininet)"
tmux send-keys -t $PANE_TWIN_NET "echo '=== DIGITAL TWIN ===' && SDN_TWIN_AUTH_TOKEN=\"$SDN_TWIN_AUTH_TOKEN\" python3 twin.py --sync" C-m

# Wait for physical network to have hosts discovered
print_step "Waiting for hosts to be discovered..."
for i in $(seq 1 45); do
    HOST_COUNT=$(curl -s -H "Authorization: ${SDN_TWIN_AUTH_TOKEN}" "http://localhost:${PHYSICAL_API_PORT}/api/topology" 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('hosts',{})))" 2>/dev/null)
    if [ "$HOST_COUNT" -ge 1 ] 2>/dev/null; then
        print_ok "Hosts discovered ($HOST_COUNT hosts)"
        break
    fi
    sleep 1
done

# 5. Dashboard
tmux new-window -t $SESSION -n "Dashboard" -c "$PROJECT_DIR"
tmux send-keys -t $SESSION:Dashboard "echo '=== WEB DASHBOARD ===' && SDN_TWIN_AUTH_TOKEN=\"$SDN_TWIN_AUTH_TOKEN\" \"$DASHBOARD_PYTHON\" dashboard.py" C-m

wait_for_port $DASHBOARD_PORT "Dashboard" || true

if command -v xdg-open > /dev/null; then
    print_step "Opening dashboard in browser..."
    if [ -n "$SUDO_USER" ]; then
        USER_UID=$(id -u "$SUDO_USER")
        sudo -H -u "$SUDO_USER" env DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$USER_UID/bus" xdg-open "http://localhost:${DASHBOARD_PORT}" &> /dev/null &
    else
        xdg-open "http://localhost:${DASHBOARD_PORT}" &> /dev/null &
    fi
fi

tmux select-window -t $SESSION:0
tmux select-pane -t $PANE_PHYS_NET

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
echo -e "    │  Physical Ctrl   │  Twin Ctrl       │"
echo -e "    │  (Ryu Docker)    │  (Ryu Docker)    │"
echo -e "    ├──────────────────┼──────────────────┤"
echo -e "    │  Physical Net    │  Digital Twin    │"
echo -e "    │  (net.py)        │  (twin.py)       │"
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
echo -e "    sudo ./start.sh --stop"
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
    cleanup
else
    echo ""
    echo -e "${YELLOW}Detached from tmux session. It is still running in the background.${NC}"
    echo -e "To re-attach:  tmux attach -t $SESSION"
    echo -e "To stop:       sudo ./start.sh --stop"
    echo ""
fi