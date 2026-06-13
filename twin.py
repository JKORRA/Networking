import urllib.request
import urllib.error
import json
import argparse
import os
import time
import heapq
import traceback
from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController, Host
from mininet.cli import CLI
from mininet.log import setLogLevel, info, error, output
from mininet.link import TCLink
import threading
import sys
import subprocess
RYU_URL = 'http://localhost:8080'
TOPOLOGY_ENDPOINT = '/api/topology'
FLOWS_ENDPOINT = '/api/flows'
CONTROLLER_IP = '127.0.0.1'
CONTROLLER_PORT = 6634
SYNC_INTERVAL = 5
MAX_RETRIES = 10
MAX_EMULATED_FLOWS = 5
RETRY_DELAY = 2

def _fetch_json(endpoint, base_url=RYU_URL, timeout=10):
    try:
        url = base_url + endpoint
        req = urllib.request.Request(url, headers={'Authorization': os.environ.get('SDN_TWIN_AUTH_TOKEN', 'Bearer SDN-Twin-Secret-Token-2026')})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = response.read().decode('utf-8')
            return json.loads(data)
    except urllib.error.URLError:
        return None
    except json.JSONDecodeError:
        return None

def _fetch_json_cached(endpoint, base_url=RYU_URL, timeout=10, etag=None):
    try:
        url = base_url + endpoint
        headers = {'Authorization': os.environ.get('SDN_TWIN_AUTH_TOKEN', 'Bearer SDN-Twin-Secret-Token-2026')}
        if etag:
            headers['If-None-Match'] = etag
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = response.read().decode('utf-8')
            return json.loads(data), response.getheader('ETag')
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return "NOT_MODIFIED", etag
        return None, None
    except Exception:
        return None, None


class TopologyFetcher: # Handles robust topology fetching with retries and validation
    def __init__(self, api_url=RYU_URL):
        self.api_url = api_url
    
    def fetch_topology(self, max_retries=MAX_RETRIES, retry_delay=RETRY_DELAY, silent=False):  # Fetch topology with retry logic and validation
        if not silent:
            info(f"Fetching topology from {self.api_url}\n")
        
        for attempt in range(max_retries):
            try:
                topology = _fetch_json(TOPOLOGY_ENDPOINT, self.api_url, timeout=5)
                
                if not topology:
                    if not silent:
                        error(f'Failed to fetch topology (attempt {attempt + 1}/{max_retries})\n')
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                    continue
                
                if not topology.get('switches', {}): # Validate topology has switches
                    if not silent:
                        error(f'No switches in topology yet (attempt {attempt + 1}/{max_retries})\n')
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                        continue
                    else:
                        return topology
                
                if not topology.get('links', []): # Check for links
                    if not silent:
                        error(f'WARNING: No links discovered yet\n')
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                        continue
                    else:
                        return topology
                
                if not silent: # Success
                    num_switches = len(topology.get('switches', {}))
                    num_links = len(topology.get('links', []))
                    num_hosts = len(topology.get('hosts', {}))
                    info(f'Topology fetched successfully (version {topology.get("version", 0)})\n')
                    info(f'Switches: {num_switches}, Links: {num_links}, Hosts: {num_hosts}\n')
                
                return topology
                
            except Exception as e:
                if not silent:
                    error(f'Connection attempt {attempt + 1}/{max_retries} failed: {e}\n')
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
        
        if not silent:
            error('Failed to fetch topology after maximum retries\n')
        return None


class DigitalTwinTopo(Topo): # Mininet topology with port conflict detection
    def __init__(self, topology_data):
        self.topology_data = topology_data
        self.switch_map = {}  # Map dpid to Mininet switch name
        self.host_map = {}    # Map MAC to Mininet host name
        self.switch_link_ports = {}  # Ports used for switch-to-switch links
        Topo.__init__(self)
    
    def build(self):
        info("Building digital twin topology\n")
        
        self._create_switches()
        
        self._analyze_switch_links()
        self._create_switch_links()
        
        self._create_hosts()
        
        info("Topology build complete\n")
    
    def _create_switches(self):
        switches = self.topology_data.get('switches', {})
        
        for dpid_str, switch_info in switches.items():
            dpid = switch_info.get('dpid')
            
            if dpid:
                if isinstance(dpid, str):
                    dpid_int = int(dpid)
                else:
                    dpid_int = dpid
                
                dpid_hex = format(dpid_int, '016x')
                switch_name = f"twin_s{dpid_int}"
                
                self.switch_map[dpid_int] = switch_name
                self.addSwitch(switch_name, dpid=dpid_hex, failMode='secure')
                info(f"    Added switch {switch_name} (dpid: {dpid_hex})\n")
    
    def _analyze_switch_links(self): # Analyze which ports are used for switch-to-switch links
        links = self.topology_data.get('links', [])
        
        for link in links:
            src_dpid = link.get('src_dpid')
            dst_dpid = link.get('dst_dpid')
            src_port = link.get('src_port')
            dst_port = link.get('dst_port')
            
            # Track which ports are used for inter-switch links
            self.switch_link_ports.setdefault(src_dpid, set()).add(src_port)
            self.switch_link_ports.setdefault(dst_dpid, set()).add(dst_port)
    
    def _create_switch_links(self): # Create links between switches avoiding duplicates
        links = self.topology_data.get('links', [])
        added_links = set()
        
        for link in links:
            src_dpid = link.get('src_dpid')
            dst_dpid = link.get('dst_dpid')
            src_port = link.get('src_port')
            dst_port = link.get('dst_port')
            
            link_id = tuple(sorted([src_dpid, dst_dpid])) # Create unique link identifier (bidirectional)
            
            if link_id in added_links:
                continue
            
            if src_dpid in self.switch_map and dst_dpid in self.switch_map:
                src_switch = self.switch_map[src_dpid]
                dst_switch = self.switch_map[dst_dpid]
                
                # Enforce identical port mapping for Control-Plane Mirroring
                self.addLink(
                    src_switch, dst_switch,
                    port1=src_port,
                    port2=dst_port,
                    bw=100,
                    delay='2ms'
                )
                info(f"Linked {src_switch} (port {src_port}) <-> {dst_switch} (port {dst_port})\n")
                
                added_links.add(link_id)
    
    def _create_hosts(self): # Create hosts from topology data with port conflict detection
        hosts = self.topology_data.get('hosts', {})
        host_counter = 1
        hosts_added = 0
        
        for mac, host_info in sorted(hosts.items(), key=lambda item: item[0]):
            dpid = host_info.get('dpid')
            port = host_info.get('port')
            
            # Skip if port is used for switch-to-switch links
            if dpid in self.switch_link_ports and port in self.switch_link_ports[dpid]:
                info(f"Skipping MAC {mac} (s{dpid}:{port} is a switch link port)\n")
                continue
            
            host_name = f"twin_h{host_counter}"
            mac_addr = host_info.get('mac')
            ipv4 = host_info.get('ipv4')
            
            self.host_map[mac] = {
                'name': host_name,
                'switch': dpid,
                'port': port
            }
            
            # Use original physical IP to ensure true packet fidelity
            if ipv4 and ipv4 != 'None':
                ip_with_mask = ipv4 if '/' in ipv4 else f"{ipv4}/24"
            else:
                ip_with_mask = f"10.0.0.{host_counter}/24"

            self.addHost(host_name, ip=ip_with_mask, mac=mac_addr)
            info(f"Added host {host_name} (IP: {ip_with_mask}, MAC: {mac_addr})\n")
            
            # Link host to switch
            if dpid in self.switch_map:
                switch_name = self.switch_map[dpid]
                self.addLink(
                    host_name,
                    switch_name,
                    port2=port,
                    bw=10,
                    delay='5ms'
                )
                info(f"Linked {host_name} to {switch_name} (port {port})\n")
            
            host_counter += 1
            hosts_added += 1
        
        # Fallback: create default hosts if none were valid
        if hosts_added == 0:
            info("No valid hosts found, creating default configuration\n")
            for dpid in sorted(self.switch_map.keys()):
                host_name = f"twin_h{host_counter}"
                ip = f"192.168.0.{host_counter}/24"
                mac = f"02:00:00:00:0000:00:{host_counter:02x}"
                
                self.addHost(host_name, ip=ip, mac=mac)
                info(f"Added host {host_name} (IP: {ip}, MAC: {mac})\n")
                
                switch_name = self.switch_map[dpid]
                self.addLink(host_name, switch_name, bw=10, delay='5ms')
                info(f"Linked {host_name} to {switch_name}\n")
                
                host_counter += 1
                hosts_added += 1
                
                if hosts_added >= 3:
                    break


class DigitalTwin: # Digital twin network with dynamic synchronization
    def __init__(self, topology_data, enable_sync=False):
        self.topology_data = topology_data
        self.enable_sync = enable_sync
        self.net = None
        self.topo = None
        self.sync_thread = None
        self.running = False
        self.link_map = {}  # Map (dpid1, dpid2) -> Link object
        self.host_counter = len(topology_data.get('hosts', {})) + 1
        self.created_hosts = {}  # Track dynamically created hosts: MAC -> Host object
        self._iperf_pids = {}  # host_name -> [(pid, start_time), ...]
        self._iperf_servers = set()  # track hosts with iperf3 server running
    
    def create(self): # Create and start the digital twin network
        info("Creating digital twin network\n")
        
        self.topo = DigitalTwinTopo(self.topology_data) # Build topology

        self.net = Mininet(
            topo=self.topo,
            link=TCLink,
            controller=None,
            autoSetMacs=True,
            autoStaticArp=True,
            build=False
        )
        
        # Controller removed for True Control-Plane Mirroring. 
        # Switches are in failMode='secure' and driven entirely by _sync_flow_tables
        
        self.net.build()
        
        # Build fast lookup: MAC -> Mininet host
        host_by_mac = {h.MAC(): h for h in self.net.hosts}
        for mac, hinfo in self.topology_data.get('hosts', {}).items():
            h = host_by_mac.get(mac)
            if h:
                self.created_hosts[mac] = h
                ipv4 = hinfo.get('ipv4')
                if ipv4 and ipv4 != 'None':
                    physical_ip = ipv4.split('/')[0]
                    self.created_hosts[physical_ip] = h

        info("Starting digital twin network (No Controller)\n")
        self.net.start()
        
        
        self._build_link_map() # Build link map for quick lookup
        
        # Controller removed for Control-Plane Mirroring. No need to wait for connection.
        
        time.sleep(2)
        
        # Display network info
        self._display_network_info()
        
        return self.net
    
    def _build_link_map(self):
        for link in self.net.links:
            node1 = link.intf1.node
            node2 = link.intf2.node

            if hasattr(node1, 'dpid') and hasattr(node2, 'dpid'):
                dpid1 = int(node1.dpid, 16)
                dpid2 = int(node2.dpid, 16)

                key = tuple(sorted([dpid1, dpid2]))
                self.link_map[key] = link

                info(f"    Mapped link: s{dpid1} <-> s{dpid2}\n")

    def _update_link_map(self):
        for link in self.net.links:
            node1 = link.intf1.node
            node2 = link.intf2.node
            if hasattr(node1, 'dpid') and hasattr(node2, 'dpid'):
                dpid1 = int(node1.dpid, 16)
                dpid2 = int(node2.dpid, 16)
                key = tuple(sorted([dpid1, dpid2]))
                if key not in self.link_map:
                    self.link_map[key] = link
                    info(f"    Updated link_map: s{dpid1} <-> s{dpid2}\n")
    
    def _wait_for_switches(self, timeout=30): 
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            all_connected = True
            for switch in self.net.switches:
                if not switch.connected():
                    all_connected = False
                    break
            
            if all_connected:
                return True
            
            info(".")
            time.sleep(1)
        
        info("\n")
        return False
    
    def _display_network_info(self):
        info('\nDigital Twin Network Information:\n')
        info(f'Controller: {CONTROLLER_IP}:{CONTROLLER_PORT}\n')
        info(f'Original API: {RYU_URL}\n')
        
        info('\nSwitches:\n')
        for switch in self.net.switches:
            info(f'    {switch.name} (dpid: {switch.dpid})\n')
        
        info('\nHosts:\n')
        for host in self.net.hosts:
            try:
                host_ip = host.IP() if host.defaultIntf() else 'No interface'
                host_mac = host.MAC() if host.defaultIntf() else 'No interface'
                info(f'    {host.name}: {host_ip} (MAC: {host_mac})\n')
            except:
                info(f'    {host.name}: Configuration pending\n')
        
        info('\nLinks:\n')
        for link in self.net.links:
            status = "UP" if link.intf1.isUp() and link.intf2.isUp() else "DOWN"
            info(f'    {link.intf1.node.name} <-> {link.intf2.node.name} [{status}]\n')
        
        info('\n')
    
    def start_sync(self): # Start background thread to sync topology changes
        if not self.enable_sync:
            return
            
        if self.sync_thread and self.sync_thread.is_alive():
            info("Sync already running\n")
            return
        
        self.running = True
        self.sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
        self.sync_thread.start()
        info(f"Started topology synchronization (interval: {SYNC_INTERVAL}s)\n")
        info("Twin will replicate: Link changes, New hosts\n")
        info("Sync runs in background - you can still use the CLI!\n\n")
    
    def _sync_loop(self): # Background thread to continuously sync topology
        last_version = self.topology_data.get('version', 0)
        last_etag = str(last_version)
        
        while self.running:
            try:
                # Fetch traffic stats
                traffic_data = _fetch_json(FLOWS_ENDPOINT, RYU_URL, timeout=5)
                if traffic_data:
                    self._handle_traffic(traffic_data)
                
                # Sync raw OpenFlow tables for Control-Plane Mirroring
                self._sync_flow_tables()
                
                # Fetch latest topology efficiently using ETag caching
                topology_data, new_etag = _fetch_json_cached(TOPOLOGY_ENDPOINT, RYU_URL, timeout=5, etag=last_etag)
                
                if topology_data == "NOT_MODIFIED" or not topology_data:
                    continue # ETag matched (no changes) or error
                
                new_topology = topology_data
                last_etag = new_etag
                new_version = new_topology.get('version', 0)
                
                # Check if topology changed
                if new_version > last_version:
                    output(f"!!!TOPOLOGY CHANGE DETECTED!!! (v{last_version} -> v{new_version})\n")
                    self._handle_topology_change(self.topology_data, new_topology)
                    
                    output("mininet> ")  # Re-print prompt
                    self.topology_data = new_topology
                    last_version = new_version

            except Exception as e:
                error(f"Sync error: {e}\n")
            finally:
                time.sleep(SYNC_INTERVAL)
    
    def _handle_traffic(self, flow_matrix):
        all_flows = [
            (mbps, src_ip, dst_ip)
            for src_ip, targets in flow_matrix.items()
            for dst_ip, mbps in targets.items()
            if mbps > 0.1
        ]
        top_flows = heapq.nlargest(5, all_flows, key=lambda x: x[0])

        self._cleanup_expired_iperf3()

        for mbps, src_ip, dst_ip in top_flows:
            src_host = self.created_hosts.get(src_ip)
            dst_host = self.created_hosts.get(dst_ip)

            if not src_host or not dst_host:
                continue

            if dst_host.name not in self._iperf_servers:
                dst_host.cmd('iperf3 -s -D')
                self._iperf_servers.add(dst_host.name)

            twin_dst_ip = dst_host.IP()
            duration = SYNC_INTERVAL + 2
            cmd = f"iperf3 -c {twin_dst_ip} -u -b {mbps}M -t {duration} &"
            result = src_host.cmd(cmd)
            self._track_iperf3_pid(src_host, result)

    def _sync_flow_tables(self):
        try:
            for switch in self.net.switches:
                if not switch.name.startswith('twin_s'):
                    continue
                
                physical_switch = switch.name.replace('twin_', '')
                
                # Fetch raw OpenFlow table from the physical switch
                cmd = f"ovs-ofctl dump-flows {physical_switch} -O OpenFlow13 --no-stats"
                try:
                    flows = subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL).decode('utf-8')
                except subprocess.CalledProcessError:
                    continue # Physical switch might not exist yet
                
                # Filter out the header (NXST_FLOW/OFPST_FLOW) and clean lines
                flow_lines = []
                for line in flows.splitlines():
                    line = line.strip()
                    if line.startswith('NXST_FLOW') or line.startswith('OFPST_FLOW') or not line:
                        continue
                    flow_lines.append(line)
                
                if not flow_lines:
                    output(f"No flows found on {physical_switch} to sync to {switch.name}\n")
                    continue
                    
                # Write to temp file and replace
                tmp_file = f"/tmp/{switch.name}_flows.txt"
                with open(tmp_file, 'w') as f:
                    f.write('\n'.join(flow_lines) + '\n')
                
                replace_cmd = f"ovs-ofctl replace-flows {switch.name} {tmp_file} -O OpenFlow13"
                result = subprocess.run(replace_cmd, shell=True, capture_output=True, text=True)
                if result.returncode != 0:
                    error(f"Flow replace error on {switch.name}: {result.stderr}\n")
                
        except Exception as e:
            error(f"Flow table sync error: {e}\n")

    def _track_iperf3_pid(self, host, cmd_output):
        pids = []
        for line in cmd_output.split('\n'):
            line = line.strip()
            if line and line.isdigit():
                pids.append((int(line), time.time()))
        if pids:
            if host.name not in self._iperf_pids:
                self._iperf_pids[host.name] = []
            self._iperf_pids[host.name].extend(pids)

    def _cleanup_expired_iperf3(self):
        now = time.time()
        for host_name in list(self._iperf_pids.keys()):
            alive = []
            for pid, start_time in self._iperf_pids[host_name]:
                if now - start_time < SYNC_INTERVAL + 5:
                    alive.append((pid, start_time))
                else:
                    host = self.net.get(host_name) if host_name in [h.name for h in self.net.hosts] else None
                    if host:
                        host.cmd(f'kill {pid} 2>/dev/null')
            if alive:
                self._iperf_pids[host_name] = alive
            else:
                del self._iperf_pids[host_name]
                        
    def _handle_topology_change(self, old_topology, new_topology): # Handle changes in topology 
        # 1. Handle LINK changes
        old_links = {self._link_key(l) for l in old_topology.get('links', [])}
        new_links = {self._link_key(l) for l in new_topology.get('links', [])}
        
        added_links = new_links - old_links
        removed_links = old_links - new_links
        
        if removed_links:
            output(f"Links REMOVED: {len(removed_links)}\n")
            for link_key in removed_links:
                dpid1 = link_key[0][0]
                dpid2 = link_key[1][0]
                output(f"     - s{dpid1} <-> s{dpid2}\n")
                self._bring_link_down(dpid1, dpid2)

        if added_links:
            output(f"Links ADDED: {len(added_links)}\n")
            for link_key in added_links:
                dpid1 = link_key[0][0]
                port1 = link_key[0][1]
                dpid2 = link_key[1][0]
                port2 = link_key[1][1]
                output(f"     - s{dpid1} (port {port1}) <-> s{dpid2} (port {port2})\n")
                self._bring_link_up(dpid1, dpid2, port1, port2)
        
        # 2. Handle HOST changes
        old_hosts = set(old_topology.get('hosts', {}).keys())
        new_hosts = set(new_topology.get('hosts', {}).keys())
        
        added_hosts = new_hosts - old_hosts
        removed_hosts = old_hosts - new_hosts
        
        # ADD new hosts dynamically
        if added_hosts:
            output(f"  Hosts ADDED: {len(added_hosts)}\n")
            for mac in added_hosts:
                host_info = new_topology['hosts'][mac]
                output(f"     - {mac} at s{host_info.get('dpid')}\n")
                self._add_host_dynamically(mac, host_info)
        
        # 3. Handle SWITCH changes
        old_switches = set(old_topology.get('switches', {}).keys())
        new_switches = set(new_topology.get('switches', {}).keys())
        
        added_switches = new_switches - old_switches
        removed_switches = old_switches - new_switches
        
        if removed_switches:
            output(f"Switches REMOVED: {len(removed_switches)}\n")
            for dpid in removed_switches:
                output(f"     - s{dpid}\n")
                self._remove_switch_dynamically(dpid)
                
        if added_switches:
            output(f"Switches ADDED: {len(added_switches)}\n")
            for dpid in added_switches:
                output(f"     - s{dpid}\n")
                self._add_switch_dynamically(dpid)

            self._update_link_map()
        
        if added_links or removed_links or added_hosts or added_switches or removed_switches: # Summary
            output(f"\nTwin network updated!\n")
    
    def _bring_link_down(self, dpid1, dpid2): # Bring down a link between two switches
        link_key = tuple(sorted([dpid1, dpid2]))
        
        if link_key in self.link_map:
            link = self.link_map[link_key]
            
            # Bring down both interfaces
            link.intf1.ifconfig('down')
            link.intf2.ifconfig('down')
            
            output(f"Brought down link twin_s{dpid1} <-> twin_s{dpid2}\n")
        else:
            output(f"Link twin_s{dpid1} <-> twin_s{dpid2} not found in link map\n")
    
    def _bring_link_up(self, dpid1, dpid2, port1, port2): # Bring up a link between two switches
        link_key = tuple(sorted([dpid1, dpid2]))
        
        if link_key in self.link_map:
            link = self.link_map[link_key]
            
            # Bring up both interfaces
            link.intf1.ifconfig('up')
            link.intf2.ifconfig('up')
            
            output(f"Brought up link twin_s{dpid1} <-> twin_s{dpid2}\n")
        else:
            # Dynamic New Link Creation
            switch1_name = f"twin_s{dpid1}"
            switch2_name = f"twin_s{dpid2}"
            
            s1 = next((s for s in self.net.switches if s.name == switch1_name), None)
            s2 = next((s for s in self.net.switches if s.name == switch2_name), None)
            
            if s1 and s2:
                link = self.net.addLink(s1, s2, port1=port1, port2=port2, bw=100, delay='2ms')
                s1.attach(link.intf1.name)
                s2.attach(link.intf2.name)
                link.intf1.ifconfig('up')
                link.intf2.ifconfig('up')
                self.link_map[link_key] = link
                output(f"Dynamically created NEW link {switch1_name} (port {port1}) <-> {switch2_name} (port {port2})\n")
            else:
                output(f"Link twin_s{dpid1} <-> twin_s{dpid2} not found, and switches don't exist\n")
    
    def _add_host_dynamically(self, mac, host_info):
        try:
            dpid = host_info.get('dpid')
            ipv4 = host_info.get('ipv4')

            switch_name = f"twin_s{dpid}"
            switch = next((s for s in self.net.switches if s.name == switch_name), None)

            if not switch:
                output(f"Switch {switch_name} not found, cannot add host\n")
                return

            host_name = f"twin_h{self.host_counter}"
            self.host_counter += 1

            if ipv4 and ipv4 != 'None':
                ip_with_mask = ipv4 if '/' in ipv4 else f"{ipv4}/24"
            else:
                ip_with_mask = f"10.0.0.{self.host_counter}/24"

            port = host_info.get('port')
            host = self.net.addHost(host_name, cls=Host, ip=ip_with_mask, mac=mac)
            link = self.net.addLink(host, switch, port2=port, bw=10, delay='5ms')
            host.configDefault()
            switch.attach(link.intf2.name)
            link.intf1.ifconfig('up')
            link.intf2.ifconfig('up')

            ip_clean = ip_with_mask.split('/')[0]
            for existing_host in self.net.hosts:
                if existing_host.name != host_name:
                    existing_ip = existing_host.IP()
                    existing_mac = existing_host.MAC()
                    if existing_ip and existing_mac:
                        host.setARP(existing_ip, existing_mac)

            self.created_hosts[mac] = host
            if ipv4 and ipv4 != 'None':
                physical_ip = ipv4.split('/')[0]
                self.created_hosts[physical_ip] = host

            output(f"Added host {host_name} (IP: {ip_with_mask}, MAC: {mac})\n")
            output(f"Linked {host_name} to {switch_name}\n")

        except Exception as e:
            output(f"Failed to add host {mac}: {e}\n")
            if 'host' in dir() and host and host.name in [h.name for h in self.net.hosts]:
                output(f"Cleaning up failed host {host_name}\n")
    
    def _add_switch_dynamically(self, dpid_str):
        try:
            dpid_int = int(dpid_str)
            dpid_hex = format(dpid_int, '016x')
            switch_name = f"twin_s{dpid_int}"
            
            switch = self.net.addSwitch(switch_name, dpid=dpid_hex, failMode='secure')
            switch.start(self.net.controllers)
            output(f"Added switch {switch_name} (dpid: {dpid_hex})\n")
        except Exception as e:
            output(f"Failed to add switch {dpid_str}: {e}\n")
            
    def _remove_switch_dynamically(self, dpid_str):
        try:
            dpid_int = int(dpid_str)
            switch_name = f"twin_s{dpid_int}"
            
            switch_to_remove = None
            for s in self.net.switches:
                if s.name == switch_name:
                    switch_to_remove = s
                    break
                    
            if switch_to_remove:
                self.net.delSwitch(switch_to_remove)
                # Clean up link_map entries referencing this switch
                for key in list(self.link_map.keys()):
                    if dpid_int in key:
                        del self.link_map[key]
                # Clean up created_hosts entries for hosts no longer in the network
                current_host_names = {h.name for h in self.net.hosts}
                self.created_hosts = {
                    k: v for k, v in self.created_hosts.items()
                    if isinstance(v, str) or v.name in current_host_names
                }
                output(f"Removed switch {switch_name}\n")
            else:
                output(f"Switch {switch_name} not found to remove\n")
        except Exception as e:
            output(f"Failed to remove switch {dpid_str}: {e}\n")

    def _link_key(self, link): # Create a hashable key for a link
        return tuple(sorted([
            (link.get('src_dpid'), link.get('src_port')),
            (link.get('dst_dpid'), link.get('dst_port'))
        ]))
    
    def stop_sync(self): # Stop background sync
        self.running = False
        if self.sync_thread:
            self.sync_thread.join(timeout=2)
        info("Stopped topology sync\n")
    
    def test(self):
        info("Running connectivity tests\n")
        self.net.pingAll()
    
    def start_cli(self):
        info("Type 'exit' to stop the digital twin\n\n")
        CLI(self.net)
    
    def stop(self):
        self.stop_sync()
        if self.net:
            for host in self.net.hosts:
                host.cmd('pkill -f "iperf3" 2>/dev/null')
            info("Stopping digital twin network\n")
            self.net.stop()


def validate_topology(topology):
    if not topology:
        error("ERROR: Topology is None or empty\n")
        return False
    
    if not isinstance(topology, dict):
        error("ERROR: Topology is not a dictionary\n")
        return False
    
    required_keys = ['switches', 'links', 'hosts']
    for key in required_keys:
        if key not in topology:
            error(f"ERROR: Topology missing required key: {key}\n")
            return False
    
    if not topology['switches']:
        error("WARNING: No switches found in topology\n")
    
    if not topology['hosts']:
        error("WARNING: No hosts found in topology\n")
    
    return True


def check_controller(ip, port): # Check if controller is reachable
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((ip, port))
        sock.close()
        return result == 0
    except:
        return False


def main():
    parser = argparse.ArgumentParser(description='Digital Twin Network')
    parser.add_argument(
        '--sync', 
        action='store_true',
        help='Enable continuous topology synchronization'
    )
    
    args = parser.parse_args()
    
    setLogLevel('info')
    
    info("Checking if twin RYU controller is running...\n")
    if not check_controller(CONTROLLER_IP, CONTROLLER_PORT):
        error(f"\n WARNING: Cannot connect to twin controller\n")
        error("\nMake sure to start a second RYU controller:\n")
        error(f"  ryu-manager --wsapi-port 8081 --ofp-tcp-listen-port {CONTROLLER_PORT} controller.py\n")
        error("\nContinuing anyway, but switches may not connect...\n\n")
        time.sleep(3)
    else:
        info(f"Twin controller is reachable on port {CONTROLLER_PORT}\n\n")
    
    fetcher = TopologyFetcher(RYU_URL) # Fetch topology with retries
    topology = fetcher.fetch_topology()
    
    # Validate topology
    if not validate_topology(topology):
        error("\nERROR: Invalid topology data. Cannot create twin.\n")
        error("\nPlease ensure:\n")
        error("1. RYU controller is running (port 8080)\n")
        error("   ryu-manager --observe-links controller.py\n")
        error("2. Original network is started\n")
        error("   sudo python3 net.py\n")
        error("3. Run 'pingall' in the original network to discover topology\n")
        return 1
    
    # Create digital twin
    twin = DigitalTwin(topology, enable_sync=args.sync)
    
    try:
        twin.create()
        
        # Start sync if requested
        if args.sync:
            twin.start_sync()
        
        twin.start_sync()
        
        twin.start_cli()
    
    except KeyboardInterrupt:
        info('\nInterrupted by user\n')
    except Exception as e:
        error(f"\nERROR: Failed to create digital twin: {e}\n")
        traceback.print_exc()
        return 1
    
    finally:
        twin.stop()
    
    return 0


if __name__ == '__main__':
    sys.exit(main())