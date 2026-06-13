# Hybrid Digital Twin for SDN Networks

A fully automated system that generates a real-time **Digital Twin** of an SDN network. The twin replicates the physical topology, mirrors traffic loads, and visualizes everything through an interactive web dashboard with live traffic heatmaps.

The system exploits the **Ryu Northbound REST API** to retrieve topology and traffic information from the physical network, and automatically reproduces any runtime change into the Digital Twin.

---

## Table of Contents

- [Architecture](#architecture)
- [Features](#features)
- [Project Structure](#project-structure)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
  - [1. System Dependencies](#1-system-dependencies)
  - [2. Docker Image for Ryu (Required)](#2-docker-image-for-ryu-required)
  - [3. Python Dependencies](#3-python-dependencies)
- [Execution](#execution)
  - [Step 1 — Start the Physical Network](#step-1--start-the-physical-network)
  - [Step 2 — Start the Physical Ryu Controller](#step-2--start-the-physical-ryu-controller)
  - [Step 3 — Start the Twin Ryu Controller](#step-3--start-the-twin-ryu-controller)
  - [Step 4 — Start the Digital Twin](#step-4--start-the-digital-twin)
  - [Step 5 — Start the Web Dashboard](#step-5--start-the-web-dashboard)
- [Testing and Demonstration](#testing-and-demonstration)
  - [Basic Connectivity](#basic-connectivity)
  - [Traffic Generation and Heatmap](#traffic-generation-and-heatmap)
  - [Dynamic Topology Changes](#dynamic-topology-changes)
- [Architecture Details](#architecture-details)
  - [IP Translation (Namespace Isolation)](#ip-translation-namespace-isolation)
  - [Traffic Monitoring and Emulation](#traffic-monitoring-and-emulation)
  - [Real-Time Dashboard](#real-time-dashboard)
- [REST API Reference](#rest-api-reference)
- [Troubleshooting](#troubleshooting)

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                        Linux Host Machine                         │
│                                                                   │
│  ┌────────────────────────┐    ┌────────────────────────────────┐  │
│  │   Physical Network     │    │        Digital Twin            │  │
│  │                        │    │                                │  │
│  │  h1 ── s1 ── s2 ── s3 │    │  th1 ── ts1 ── ts2 ── ts3     │  │
│  │        │         │     │    │         │           │          │  │
│  │       h2        h3     │    │        th2         th3         │  │
│  │                        │    │                                │  │
│  │  IPs: 10.0.0.x/24     │    │  IPs: 192.168.0.x/24          │  │
│  │  MACs: 00:00:00:...    │    │  MACs: 02:00:00:...           │  │
│  └───────────┬────────────┘    └──────────────┬─────────────────┘  │
│              │                                │                    │
│       ┌──────┴──────┐               ┌─────────┴──────┐            │
│       │ Ryu Ctrl #1 │               │  Ryu Ctrl #2   │            │
│       │ (Docker)    │               │  (Docker)      │            │
│       │ Port 6633   │               │  Port 6634     │            │
│       │ API: 8080   │               │  API: 8081     │            │
│       └──────┬──────┘               └────────────────┘            │
│              │                                                     │
│       ┌──────┴──────┐                                             │
│       │  Dashboard  │  ◄── Fetches /api/topology + /api/traffic   │
│       │  Flask:5000 │                                             │
│       └─────────────┘                                             │
└────────────────────────────────────────────────────────────────────┘
```

---

## Features

| Feature | Description |
|---|---|
| **Automated Topology Replication** | Fetches real-time switch, link, and host data via Ryu REST API and builds an identical Mininet clone |
| **IP Translation** | Maps physical IPs (`10.0.0.x`) → twin IPs (`192.168.0.x`) to prevent Linux kernel namespace conflicts |
| **MAC Isolation** | Remaps MAC addresses (`00:00:00:` → `02:00:00:`) to prevent ARP poisoning between coexisting Mininet instances |
| **Traffic Monitoring** | Polls OpenFlow `OFPPortStatsRequest` and `OFPFlowStatsRequest` to calculate real-time Rx/Tx Mbps per port and per flow |
| **Flow-Based Emulation** | Detects active end-to-end IP flows >1 Mbps and auto-spawns exact `iperf3` client/server replicas between twin hosts |
| **Dynamic Sync** | Background thread detects topology changes (link up/down, new hosts, dynamic switches) and reproduces them identically at runtime |
| **Web Dashboard** | Real-time WebSocket (Socket.IO) dashboard with interactive nodes, live event logs, active flows table, and traffic heatmaps |
| **ETag Caching** | High-frequency API polling (2s interval) optimized by zero-payload `304 Not Modified` responses to virtually eliminate control-plane overhead |
| **Bearer Authentication** | Enforces secure access for all Northbound REST API endpoints, the dashboard, and external twin integrations |
| **Emulation Capacity Caps** | Intelligently limits `iperf3` concurrent flow emulation to only the Top N heaviest flows to protect host CPU and prevent namespace resource starvation |
| **Teardown Orchestration** | Strict shutdown sequence that explicitly detects and kills all trailing user-space daemons (`iperf3`), containers, and Mininet artifacts on exit |

---

## Project Structure

```
.
├── net.py               # Physical network topology (Mininet)
├── controller.py        # Ryu SDN controller with REST API + traffic stats
├── twin.py              # Digital Twin builder with sync and traffic emulation
├── dashboard.py         # Flask web server for the visualization dashboard
├── Dockerfile.ryu       # Docker image for running Ryu on modern systems
├── start.sh             # Launcher script (Tmux + Docker orchestration)
├── stop.sh              # Cleanup script
├── templates/
│   └── index.html       # vis-network frontend with traffic heatmaps
└── README.md            # This file
```

### File Descriptions

- **`net.py`** — Defines the physical SDN network: 3 switches in a linear topology (`s1-s2-s3`), each with one host (`h1`, `h2`, `h3`). Connects to a remote Ryu controller on port `6633`.

- **`controller.py`** — A Ryu OpenFlow 1.3 controller implementing:
  - L2 learning switch with ARP flood handling
  - Topology discovery via `ryu.topology` events
  - Real-time port statistics polling (`OFPPortStatsRequest`)
  - REST API endpoints: `/api/topology`, `/api/switches`, `/api/links`, `/api/hosts`, `/api/traffic`, `/api/version`

- **`twin.py`** — The main Digital Twin engine:
  - Fetches the physical topology via REST API with retry logic
  - Builds an isolated Mininet replica with translated IPs and MACs
  - Runs a background synchronization loop detecting topology and traffic changes
  - Spawns `iperf3` inside twin hosts to emulate detected traffic loads

- **`dashboard.py`** — A Flask application that proxies data from Ryu and serves the web dashboard.

- **`templates/index.html`** — Interactive graph visualization using [vis-network](https://visjs.github.io/vis-network/docs/network/). Edges are color-coded based on traffic:
  - 🟢 Green: < 1 Mbps
  - 🟡 Yellow: 1–5 Mbps
  - 🔴 Red: > 5 Mbps

- **`start.sh` & `stop.sh`** — Automated orchestration scripts that use Tmux to launch all components in isolated panes cleanly, managing dependencies and port conflicts.

---

## Prerequisites

| Requirement | Purpose |
|---|---|
| **Linux** (Debian/Ubuntu recommended) | Mininet requires Linux kernel network namespaces |
| **Mininet** | Virtual network emulation |
| **Open vSwitch** | OpenFlow-compatible virtual switches |
| **Docker** | Runs the Ryu controller in an isolated Python 3.9 environment |
| **iperf3** | Traffic generation for load emulation |
| **Python 3** (system) | Runs `net.py`, `twin.py`, and `dashboard.py` natively |
| **Flask** | Web dashboard backend |

> **Why Docker?** The Ryu SDN framework was abandoned in 2017 and is incompatible with Python ≥ 3.12 due to removed `distutils` and `setuptools` internals. Docker isolates Ryu in a Python 3.9 container while the rest of the project runs natively.

---

## Setup

### 1. System Dependencies

```bash
sudo apt update
sudo apt install -y mininet openvswitch-switch iperf3 docker.io
```

Start the Open vSwitch service:

```bash
sudo service openvswitch-switch start
```

Ensure your user can run Docker (or use `sudo`):

```bash
sudo usermod -aG docker $USER
# Log out and back in for this to take effect, or use sudo for docker commands
```

### 2. Docker Image for Ryu (Required)

Build the custom Ryu Docker image. This only needs to be done **once**:

```bash
sudo docker build -t my-ryu -f Dockerfile.ryu .
```

This creates a lightweight image (~250 MB) with Python 3.9, a compatible `setuptools`, `eventlet==0.30.2`, and `ryu==4.34`.

### 3. Python Dependencies

Install Flask for the dashboard (in a virtual environment or system-wide):

```bash
# Option A: Using a virtual environment
python3 -m venv venv
./venv/bin/pip install flask

# Option B: System-wide
pip3 install flask
```

The Mininet Python library is installed system-wide by the `mininet` apt package; it does **not** need to be installed via pip.

---

## Execution

### Automated Launch (Recommended)

The easiest way to run the entire project is using the automated `start.sh` script, which orchestrates all 5 components in a single `tmux` session with split panes.

```bash
sudo ./start.sh
```

This will automatically:
1. Check dependencies.
2. Build the Docker image if missing.
3. Clean up any stale Mininet/Ryu instances.
4. Launch the physical network, both controllers, the twin engine, and the dashboard in separate tmux panes.

To detach from the tmux session, press `Ctrl+B` then `d`.
To re-attach, run `tmux attach -t sdn-twin`.
To stop everything, run `sudo ./stop.sh`.

### Manual Execution

If you prefer to run things manually, open **5 separate terminal windows/tabs** and run the commands in the following order.

### Step 1 — Start the Physical Network

```bash
sudo python3 net.py
```

Wait for the `mininet>` prompt to appear. This means the 3 switches and 3 hosts are running.

### Step 2 — Start the Physical Ryu Controller

```bash
sudo docker run -it --network host -v $(pwd):/app -w /app my-ryu ryu-manager --observe-links controller.py
```

You should see log messages like:
```
Switch datapath id 1 CONNECTED
Switch datapath id 2 CONNECTED
Switch datapath id 3 CONNECTED
```

### Step 3 — Start the Twin Ryu Controller

This is a second, independent Ryu controller for the twin network, running on different ports:

```bash
sudo docker run -it --network host -v $(pwd):/app -w /app my-ryu ryu-manager --observe-links --wsapi-port 8081 --ofp-tcp-listen-port 6634 controller.py
```

### Step 4 — Start the Digital Twin

```bash
sudo python3 twin.py --sync
```

The `--sync` flag enables continuous background synchronization (topology changes + traffic emulation).

You should see:
```
Fetching topology from http://localhost:8080
Topology fetched successfully (version X)
Switches: 3, Links: 4, Hosts: 3
Building digital twin topology...
Started topology synchronization (interval: 10s)
```

### Step 5 — Start the Web Dashboard

```bash
python3 dashboard.py
# Or, if using a virtual environment:
./venv/bin/python3 dashboard.py
```

Open your browser and navigate to: **http://localhost:5000**

---

## Testing and Demonstration

### Basic Connectivity

In **Terminal 1** (physical network's `mininet>` prompt):

```bash
mininet> pingall
```

This discovers all hosts in the topology. Go back to the **Dashboard** — you should see all 3 switches and 3 hosts rendered as an interactive graph.

In **Terminal 4** (twin network's `mininet>` prompt):

```bash
mininet> pingall
```

Verify that the Digital Twin also has full connectivity between its hosts using the translated `192.168.0.x` IP addresses.

### Traffic Generation and Heatmap

In **Terminal 1** (physical network):

```bash
mininet> iperf h1 h2
```

This generates a burst of TCP traffic between `h1` and `h2`. After ~5 seconds:

1. **Dashboard**: The edges between the involved switches will turn **yellow** or **red**, and the traffic labels will show the Mbps values. The Active Flows table will list the `10.0.0.1 -> 10.0.0.2` flow.

2. **Twin** (Terminal 4): The sync loop will detect the traffic and automatically spawn `iperf3` client-server background processes inside the corresponding twin hosts to perfectly replicate the load.

**Important:** For sustained traffic that shows the heatmap change in real time, you must start an `iperf` TCP server listener on the destination host before running the client:

```bash
mininet> h2 iperf -s -D
mininet> h1 iperf -c 10.0.0.2 -t 30 &
```

### Dynamic Topology Changes

To demonstrate live topology synchronization, take down a link in the physical network:

```bash
mininet> link s1 s2 down
```

Within 10 seconds, the Twin (Terminal 4) will print:
```
!!!TOPOLOGY CHANGE DETECTED!!! (vX -> vY)
Links REMOVED: 1
     - s1 <-> s2
Brought down link twin_s1 <-> twin_s2
Twin network updated!
```

Restore the link:

```bash
mininet> link s1 s2 up
```

The twin will detect and replicate this change as well.

---

## Architecture Details

### IP Translation (Namespace Isolation)

Running two Mininet instances on the same Linux host causes **kernel namespace conflicts**: both instances create interfaces named `h1-eth0`, `s1-eth1`, etc., and use the same IP range (`10.0.0.x`). This leads to:

- ARP poisoning (hosts answering for IPs they shouldn't own)
- Routing table contamination
- Unpredictable packet delivery

**Solution**: The twin translates all addresses:

| Property | Physical Network | Digital Twin |
|---|---|---|
| IP Range | `10.0.0.x/24` | `192.168.0.x/24` |
| MAC Prefix | `00:00:00:00:00:xx` | `02:00:00:00:00:xx` |
| Switch Names | `s1`, `s2`, `s3` | `twin_s1`, `twin_s2`, `twin_s3` |
| Host Names | `h1`, `h2`, `h3` | `twin_h1`, `twin_h2`, `twin_h3` |
| Controller Port | `6633` | `6634` |
| REST API Port | `8080` | `8081` |

### Traffic Monitoring and Emulation

The Ryu controller periodically sends both `OFPPortStatsRequest` and `OFPFlowStatsRequest` messages to all connected switches. When a reply arrives, it tracks active connections based on source/destination IPv4 addresses, computing accurate Mbps through exact time deltas. 

The twin's sync loop reads this generated flow matrix via `/api/flows`. When physical end-to-end IP flows exhibit >1 Mbps throughput, the twin dynamically provisions corresponding `iperf3` processes on the mirrored twin hosts:

```bash
# Destination Twin Host runs the daemon natively
mininet> target_twin_host iperf3 -s -D

# Source Twin Host natively replicates the identical load
mininet> source_twin_host iperf3 -c {target_twin_IP} -u -b {Mbps}M -t {SYNC_INTERVAL} &
```

This ensures zero duplicate packet counting and establishes a perfectly accurate L3 flow replication network.

### Real-Time Dashboard

The Flask backend (`dashboard.py`) functions as a persistent **WebSocket (Socket.IO)** stream. It passively polls the Ryu REST APIs and instantly pushes state changes up to the client browsers:

1. Connects clients via WebSocket instead of legacy HTTP fetching.
2. Updates interactive nodes allowing users to click devices to pop out an **Interactive Sidebar Dashboard** for port logs, mappings, and byte counts.
3. Automatically computes topology `diffs` turning into **Live Event Console Logs**: `Switch disconnected`, `Host e6 joined`, `Link cut`.
4. Renders dynamic active flow charts detailing exactly `who` is talking to `who`, instantly synced from controller API values.

---

## REST API Reference

All endpoints are served by the Ryu controller on port `8080` (physical) or `8081` (twin).

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/topology` | Full topology: switches, links, hosts, version counter |
| `GET` | `/api/switches` | List of switches with DPID and port numbers |
| `GET` | `/api/links` | List of inter-switch links with src/dst DPID and port |
| `GET` | `/api/hosts` | Discovered hosts with MAC, IPv4, IPv6, and attachment point |
| `GET` | `/api/traffic` | Per-port Rx/Tx Mbps for each switch |
| `GET` | `/api/flows` | Active IP-to-IP tracking arrays detailing traffic bandwidth inside the SDN |
| `GET` | `/api/version` | Current topology version number |

### Example Response — `/api/topology`

```json
{
  "switches": {
    "1": { "dpid": 1, "ports": [1, 2] },
    "2": { "dpid": 2, "ports": [1, 2, 3] },
    "3": { "dpid": 3, "ports": [1, 2] }
  },
  "links": [
    { "src_dpid": 1, "src_port": 2, "dst_dpid": 2, "dst_port": 1 },
    { "src_dpid": 2, "src_port": 3, "dst_dpid": 3, "dst_port": 2 }
  ],
  "hosts": {
    "00:00:00:00:00:01": { "mac": "00:00:00:00:00:01", "ipv4": "10.0.0.1", "dpid": 1, "port": 1 }
  },
  "version": 5
}
```

### Example Response — `/api/traffic`

```json
{
  "1": {
    "1": { "rx_mbps": 0.0012, "tx_mbps": 0.0008 },
    "2": { "rx_mbps": 4.2351, "tx_mbps": 4.1923 }
  }
}
```

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `ModuleNotFoundError: No module named 'mininet'` | Install Mininet system-wide: `sudo apt install mininet` |
| `Cannot find required executable mnexec` | Mininet is not properly installed: `sudo apt install mininet` |
| `ovs-vsctl: not found` | Install Open vSwitch: `sudo apt install openvswitch-switch` and start it: `sudo service openvswitch-switch start` |
| Docker build fails with TLS timeout | Network issue; retry: `sudo systemctl restart docker && sudo docker build -t my-ryu -f Dockerfile.ryu .` |
| `ALREADY_HANDLED` import error in Ryu | Rebuild the Docker image — it pins `eventlet==0.30.2` which includes this export |
| No hosts visible in the Dashboard | Run `pingall` in the physical network first to trigger host discovery |
| Twin shows 0 hosts | Run `pingall` in the physical `mininet>` prompt before starting the twin |
| `Address already in use` on port 6633/8080 | Kill stale processes: `sudo mn -c && sudo fuser -k 6633/tcp 8080/tcp` |
| Dashboard shows "Could not fetch topology" | Ensure the physical Ryu controller (Docker container) is running on port 8080 |

### Clean Up

To fully reset the environment between runs, you can simply use the provided stop script:

```bash
sudo ./start.sh --stop
```

Alternatively, run the cleanup commands manually:

```bash
sudo mn -c                          # Clean up Mininet
sudo docker stop $(sudo docker ps -q)   # Stop all Docker containers
sudo fuser -k 6633/tcp 6634/tcp 8080/tcp 8081/tcp 5000/tcp  # Free ports
```