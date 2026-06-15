"""
engine.py

This module contains the core DigitalTwin orchestration engine.
It spins up the Mininet environment in 'secure' mode (without a local controller)
and manages the background synchronization thread. It dynamically mirrors
OpenFlow tables from the physical network via ovs-ofctl, replicates live topology 
changes (link up/down, host discovery), and spawns iperf3 to emulate traffic loads.
"""

import time
import heapq
import threading
import subprocess
from mininet.net import Mininet
from mininet.node import Host, RemoteController
from mininet.cli import CLI
from mininet.log import info, error, output
from mininet.link import TCLink

from topology import DigitalTwinTopo
from api_client import _fetch_json, _fetch_json_cached, FLOWS_ENDPOINT, TOPOLOGY_ENDPOINT, RYU_URL

SYNC_INTERVAL = 1
CONTROLLER_IP = '127.0.0.1'
CONTROLLER_PORT = 6634

class DigitalTwin:
    """Digital twin network with dynamic background synchronization."""
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
    
    def create(self):
        """Creates and starts the digital twin Mininet network."""
        info("Creating digital twin network\n")
        
        self.topo = DigitalTwinTopo(self.topology_data)

        self.net = Mininet(
            topo=self.topo,
            link=TCLink,
            controller=None,
            autoSetMacs=True,
            autoStaticArp=True,
            build=False
        )
        
        # Connect to the Twin Shadow Controller
        self.net.addController('c2', controller=RemoteController, ip=CONTROLLER_IP, port=CONTROLLER_PORT)
        
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
        
        self._build_link_map()
        
        time.sleep(2)
        
        self._display_network_info()
        
        return self.net
    
    def _build_link_map(self):
        """Constructs a map of switch links for rapid dynamic updates."""
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
        """Updates the link map when new switches or links are dynamically added."""
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
        """Waits for switches to become ready (legacy use)."""
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
        """Prints out the current Twin network state."""
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
    
    def start_sync(self):
        """Starts the background thread to continuously sync topology and flows via direct ETag polling."""
        if not self.enable_sync:
            return
            
        if self.sync_thread and self.sync_thread.is_alive():
            info("Sync already running\n")
            return
        
        self.running = True
        self.sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
        self.sync_thread.start()
        info(f"Started high-frequency topology synchronization (interval: {SYNC_INTERVAL}s)\n")
        info("Twin will independently replicate: Link changes, New hosts, Traffic\n")
        info("Sync runs in background - you can still use the CLI!\n\n")
    
    def _sync_loop(self):
        """Background thread loop: fetches APIs and drives Twin updates independently."""
        last_version = self.topology_data.get('version', 0)
        last_etag = str(last_version)
        
        while self.running:
            try:
                # Fetch traffic stats directly from Ryu
                traffic_data = _fetch_json(FLOWS_ENDPOINT, RYU_URL, timeout=5)
                if traffic_data:
                    self._handle_traffic(traffic_data)
                
                # Fetch latest topology efficiently using ETag caching directly from Ryu
                topology_data, new_etag = _fetch_json_cached(TOPOLOGY_ENDPOINT, RYU_URL, timeout=5, etag=last_etag)
                
                if topology_data == "NOT_MODIFIED" or not topology_data:
                    continue # ETag matched (no changes) or error
                
                new_topology = topology_data
                last_etag = new_etag
                new_version = new_topology.get('version', 0)
                
                # Check if topology changed
                if new_version > last_version:
                    output(f"\n!!!TOPOLOGY CHANGE DETECTED!!! (v{last_version} -> v{new_version})\n")
                    self._handle_topology_change(self.topology_data, new_topology)
                    output("mininet> ")
                    self.topology_data = new_topology
                    last_version = new_version
                    
            except Exception as e:
                error(f"ETag sync error: {e}\n")
            finally:
                time.sleep(SYNC_INTERVAL)
    
    def _handle_traffic(self, flow_matrix):
        """Spawns iperf3 to emulate detected traffic flow rates."""
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
                dst_host.cmd('taskset -c 2,3 iperf3 -s -D')
                self._iperf_servers.add(dst_host.name)

            twin_dst_ip = dst_host.IP()
            duration = SYNC_INTERVAL + 2
            cmd = f"taskset -c 2,3 iperf3 -c {twin_dst_ip} -u -b {mbps}M -t {duration} &"
            result = src_host.cmd(cmd)
            self._track_iperf3_pid(src_host, result)

    # Note: _sync_flow_tables has been removed because the Shadow Controller 
    # now handles all flow programming autonomously.

    def _track_iperf3_pid(self, host, cmd_output):
        """Tracks the PIDs of spawned iperf3 processes."""
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
        """Kills old iperf3 instances to prevent system resource starvation."""
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
                        
    def _handle_topology_change(self, old_topology, new_topology): 
        """Replicates live topology changes into the Mininet environment."""
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
    
    def _bring_link_down(self, dpid1, dpid2):
        """Simulates an interface going down."""
        link_key = tuple(sorted([dpid1, dpid2]))
        
        if link_key in self.link_map:
            link = self.link_map[link_key]
            
            link.intf1.ifconfig('down')
            link.intf2.ifconfig('down')
            
            output(f"Brought down link twin_s{dpid1} <-> twin_s{dpid2}\n")
        else:
            output(f"Link twin_s{dpid1} <-> twin_s{dpid2} not found in link map\n")
    
    def _bring_link_up(self, dpid1, dpid2, port1, port2):
        """Brings an interface up, or establishes a new Link if none exists."""
        link_key = tuple(sorted([dpid1, dpid2]))
        
        if link_key in self.link_map:
            link = self.link_map[link_key]
            
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
        """Attaches a newly discovered host to the Mininet topology."""
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
    
    def _add_switch_dynamically(self, dpid_str):
        """Dynamically spins up a new Switch."""
        try:
            dpid_int = int(dpid_str)
            dpid_hex = format(dpid_int, '016x')
            switch_name = f"twin_s{dpid_int}"
            
            switch = self.net.addSwitch(switch_name, dpid=dpid_hex)
            switch.start(self.net.controllers)
            output(f"Added switch {switch_name} (dpid: {dpid_hex})\n")
        except Exception as e:
            output(f"Failed to add switch {dpid_str}: {e}\n")
            
    def _remove_switch_dynamically(self, dpid_str):
        """Tears down a switch and its links."""
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
                for key in list(self.link_map.keys()):
                    if dpid_int in key:
                        del self.link_map[key]
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

    def _link_key(self, link):
        """Generates a stable, sortable tuple key for a given link dict."""
        return tuple(sorted([
            (link.get('src_dpid'), link.get('src_port')),
            (link.get('dst_dpid'), link.get('dst_port'))
        ]))
    
    def stop_sync(self):
        """Halts the background synchronization thread."""
        self.running = False
        if self.sync_thread:
            self.sync_thread.join(timeout=2)
        info("Stopped topology sync\n")
    
    def test(self):
        """Tests internal Mininet connectivity."""
        info("Running connectivity tests\n")
        self.net.pingAll()
    
    def start_cli(self):
        """Drops into the interactive Mininet shell."""
        info("Type 'exit' to stop the digital twin\n\n")
        CLI(self.net)
    
    def stop(self):
        """Safely tears down the Twin network."""
        self.stop_sync()
        if self.net:
            for host in self.net.hosts:
                host.cmd('pkill -f "iperf3" 2>/dev/null')
            info("Stopping digital twin network\n")
            self.net.stop()
