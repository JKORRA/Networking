"""
topology.py

This module defines the Mininet Topo class used to construct the digital twin.
It translates the JSON topology data received from the Ryu controller into
Mininet nodes, links, and switches, carefully maintaining exact MAC/IP mappings
and avoiding switch-to-switch port conflicts to enable true control-plane mirroring.
"""

from mininet.topo import Topo
from mininet.log import info

class DigitalTwinTopo(Topo):
    """Mininet topology builder with port conflict detection."""
    def __init__(self, topology_data):
        self.topology_data = topology_data
        self.switch_map = {}  # Map dpid to Mininet switch name
        self.host_map = {}    # Map MAC to Mininet host name
        self.switch_link_ports = {}  # Ports used for switch-to-switch links
        Topo.__init__(self)
    
    def build(self):
        """Constructs the topology components."""
        info("Building digital twin topology\n")
        
        self._create_switches()
        
        self._analyze_switch_links()
        self._create_switch_links()
        
        self._create_hosts()
        
        info("Topology build complete\n")
    
    def _create_switches(self):
        """Initializes the OpenFlow switches based on the physical dpids."""
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
                # Switch will securely connect to the Shadow Controller
                self.addSwitch(switch_name, dpid=dpid_hex)
                info(f"    Added switch {switch_name} (dpid: {dpid_hex})\n")
    
    def _analyze_switch_links(self):
        """Analyzes which ports are allocated for switch-to-switch communication."""
        links = self.topology_data.get('links', [])
        
        for link in links:
            src_dpid = link.get('src_dpid')
            dst_dpid = link.get('dst_dpid')
            src_port = link.get('src_port')
            dst_port = link.get('dst_port')
            
            self.switch_link_ports.setdefault(src_dpid, set()).add(src_port)
            self.switch_link_ports.setdefault(dst_dpid, set()).add(dst_port)
    
    def _create_switch_links(self):
        """Wires up the inter-switch links, preventing duplicate bidirectional entries."""
        links = self.topology_data.get('links', [])
        added_links = set()
        
        for link in links:
            src_dpid = link.get('src_dpid')
            dst_dpid = link.get('dst_dpid')
            src_port = link.get('src_port')
            dst_port = link.get('dst_port')
            
            link_id = tuple(sorted([src_dpid, dst_dpid]))
            
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
    
    def _create_hosts(self):
        """Instantiates hosts and links them, preserving exact MACs and IPs."""
        hosts = self.topology_data.get('hosts', {})
        host_counter = 1
        hosts_added = 0
        
        for mac, host_info in sorted(hosts.items(), key=lambda item: item[0]):
            dpid = host_info.get('dpid')
            port = host_info.get('port')
            
            # Skip if port is already used for switch-to-switch links
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
            
            if ipv4 and ipv4 != 'None':
                ip_with_mask = ipv4 if '/' in ipv4 else f"{ipv4}/24"
            else:
                ip_with_mask = f"10.0.0.{host_counter}/24"

            self.addHost(host_name, ip=ip_with_mask, mac=mac_addr)
            info(f"Added host {host_name} (IP: {ip_with_mask}, MAC: {mac_addr})\n")
            
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
