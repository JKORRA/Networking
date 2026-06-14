"""
ryu_app.py

This module is the core Ryu SDN application.
It implements the OpenFlow 1.3 switch hub, dynamically learning MAC-to-IP-to-Port
mappings, installing dynamic forwarding rules, and polling switches for real-time
port and flow byte statistics to compute network utilization metrics.
"""

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.lib import hub
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, arp, ipv4 as ipv4_pkt
from ryu.topology import event
from ryu.topology.api import get_switch, get_link, get_host
from ryu.app.wsgi import WSGIApplication

import time
import sys
import os
import logging

# Ensure local imports work regardless of how ryu-manager is invoked
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from api import NetworkAPI, api_instance_name

logging.getLogger('eventlet.wsgi.server').setLevel(logging.WARNING)

class NetworkController(app_manager.RyuApp):
    """Core SDN application managing OpenFlow rules and state discovery."""
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {'wsgi': WSGIApplication}
    
    def __init__(self, *args, **kwargs):
        super(NetworkController, self).__init__(*args, **kwargs)

        self.topology = {
            'switches': {},
            'links': [],
            'hosts': {},
            'version': 0
        }
        
        self.traffic = {} # To store real-time traffic (Mbps)
        self.port_stats = {} # To store previous byte counts
        self.flow_stats = {} # To store previous flow byte counts
        self.flow_matrix = {} # To store real-time traffic matrix (Source IP -> Dest IP : Mbps)
        
        self.mac_to_port = {} # MAC to port mapping for each switch
        self.mac_to_port_ts = {} # Timestamp for mac_to_port entries (dpid -> mac -> time)
        self.mac_to_ip_ts = {} # Timestamp for mac_to_ip entries (mac -> time)
        
        self.datapaths = {} # Track datapaths
        
        self.mac_to_ip = {} # Learn IP from any packet (ARP or IPv4)
        
        self._topo_update_scheduled = False
        self._topo_update_timer = None

        # Register REST API
        wsgi = kwargs['wsgi']
        wsgi.register(
            NetworkAPI,
            {api_instance_name: self}
        )
    
    def start(self):
        super(NetworkController, self).start()
        self.monitor_thread = hub.spawn(self._monitor)
    
    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        """Tracks the connection state of the switches."""
        datapath = ev.datapath
        
        if ev.state == MAIN_DISPATCHER: # Negotiation between RYU and OF Switch must be completed
            if datapath.id not in self.datapaths:
                self.logger.info("Switch datapath id %s CONNECTED", datapath.id)
                self.datapaths[datapath.id] = datapath
        elif ev.state == DEAD_DISPATCHER:
            if datapath.id in self.datapaths:
                self.logger.warning("Switch datapath id %s DISCONNECTED", datapath.id)
                del self.datapaths[datapath.id]
    
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """Installs the default table-miss flow entry when a switch connects."""
        try:
            datapath = ev.msg.datapath 
            ofproto = datapath.ofproto
            parser = datapath.ofproto_parser
            
            self.logger.info("Configuring switch datapath id %s", datapath.id)
            
            # Clear all existing flows to ensure fresh PacketIn events
            mod = parser.OFPFlowMod(
                datapath=datapath,
                command=ofproto.OFPFC_DELETE,
                out_port=ofproto.OFPP_ANY,
                out_group=ofproto.OFPG_ANY
            )
            datapath.send_msg(mod)
            
            # Install table-miss flow entry
            match = parser.OFPMatch()
            actions = [parser.OFPActionOutput(
                ofproto.OFPP_CONTROLLER,
                ofproto.OFPCML_NO_BUFFER
            )]
            self.add_flow(datapath, 0, match, actions)
            
            self.logger.info("Switch datapath id %s configured successfully", datapath.id)
            
            self._schedule_topology_update() # Trigger topology update
        except Exception as e:
            self.logger.error("Error in switch_features_handler: %s", e)
            self.logger.exception(e)
    
    def add_flow(self, datapath, priority, match, actions, buffer_id=None):
        """Pushes a new flow rule to a switch datapath."""
        try:
            ofproto = datapath.ofproto
            parser = datapath.ofproto_parser
            
            inst = [parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS, actions
            )]
            
            if buffer_id:
                mod = parser.OFPFlowMod(
                    datapath=datapath, buffer_id=buffer_id,
                    priority=priority, match=match, instructions=inst
                )
            else:
                mod = parser.OFPFlowMod(
                    datapath=datapath, priority=priority,
                    match=match, instructions=inst
                )
            
            datapath.send_msg(mod)
        except Exception as e:
            self.logger.error("Error adding flow: %s", e)
    
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        """Learns host MACs, IPs, and ports dynamically from incoming packets."""
        try:
            msg = ev.msg
            datapath = msg.datapath
            ofproto = datapath.ofproto
            parser = datapath.ofproto_parser
            in_port = msg.match['in_port']
            
            pkt = packet.Packet(msg.data)
            eth = pkt.get_protocols(ethernet.ethernet)[0]
            
            if eth.ethertype == ether_types.ETH_TYPE_LLDP:
                # Don't process LLDP packets
                return
            
            dst = eth.dst
            src = eth.src
            
            if not (src.startswith('00:00:00') or src.startswith('02:00:00')):
                return
            
            dpid = datapath.id
            
            self.mac_to_port.setdefault(dpid, {})
            
            self.mac_to_port[dpid][src] = in_port # Learn MAC address
            self.mac_to_port_ts.setdefault(dpid, {})[src] = time.time()
            
            # Learn IP from ARP or IPv4 packets
            arp_pkt = pkt.get_protocol(arp.arp)
            ip_pkt = pkt.get_protocol(ipv4_pkt.ipv4)
            if arp_pkt is not None:
                self.mac_to_ip[arp_pkt.src_mac] = arp_pkt.src_ip
                self.mac_to_ip_ts[arp_pkt.src_mac] = time.time()
            elif ip_pkt is not None:
                self.mac_to_ip[src] = ip_pkt.src
                self.mac_to_ip_ts[src] = time.time()
            
            if dst in self.mac_to_port[dpid]:
                out_port = self.mac_to_port[dpid][dst]
            else:
                out_port = ofproto.OFPP_FLOOD
            
            actions = [parser.OFPActionOutput(out_port)]

            # Always flood ARP (helps dynamic host addition)
            if arp_pkt is not None:
                out_port = ofproto.OFPP_FLOOD
                actions = [parser.OFPActionOutput(out_port)]
            
            # L2 learning-switch flow for known unicast destinations
            if out_port != ofproto.OFPP_FLOOD:
                if ip_pkt is not None:
                    match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, eth_dst=dst, eth_src=src, ipv4_src=ip_pkt.src, ipv4_dst=ip_pkt.dst)
                else:
                    match = parser.OFPMatch(eth_dst=dst)
                
                # ALWAYS install flow without buffer_id to prevent the switch from dropping it
                self.add_flow(datapath, 1, match, actions)
            
            data = None
            if msg.buffer_id == ofproto.OFP_NO_BUFFER:
                data = msg.data
            
            out = parser.OFPPacketOut(
                datapath=datapath, buffer_id=msg.buffer_id,
                in_port=in_port, actions=actions, data=data
            )
            datapath.send_msg(out)
        except Exception as e:
            self.logger.error("Error in packet_in_handler: %s", e)
    
    @set_ev_cls(event.EventSwitchEnter)
    def switch_enter_handler(self, ev):
        """Triggers topology update when a switch connects."""
        try:
            switch = ev.switch
            self.logger.info("Topology: Switch %s ENTERED", switch.dp.id)
            self._schedule_topology_update()
        except Exception as e:
            self.logger.error("Error in switch_enter_handler: %s", e)
    
    def _schedule_topology_update(self):
        """Schedules a debounced update to prevent hammering the API."""
        if self._topo_update_scheduled:
            return
        self._topo_update_scheduled = True
        self._topo_update_timer = hub.spawn_after(0.5, self._do_update_topology)

    def _do_update_topology(self):
        self._topo_update_scheduled = False
        self.update_topology()

    def _monitor(self):
        """Background thread to poll port and flow stats."""
        while True:
            # Optionally clear inactive flows
            self.flow_matrix.clear()
            for dp in self.datapaths.values():
                self._request_stats(dp)
            self._refresh_host_ips() # Supplement IPs without changing version

            # Prune stale mac_to_port entries (older than 300s)
            now = time.time()
            for dpid in list(self.mac_to_port_ts.keys()):
                stale = [mac for mac, ts in self.mac_to_port_ts[dpid].items() if now - ts > 300]
                for mac in stale:
                    del self.mac_to_port[dpid][mac]
                    del self.mac_to_port_ts[dpid][mac]

            # Prune stale mac_to_ip entries (older than 300s)
            stale = [mac for mac, ts in self.mac_to_ip_ts.items() if now - ts > 300]
            for mac in stale:
                self.mac_to_ip.pop(mac, None)
                self.mac_to_ip_ts.pop(mac, None)

            hub.sleep(5) # polling interval
            
    def _request_stats(self, datapath):
        """Requests switch port and flow statistics."""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        req = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
        datapath.send_msg(req)
        
        flow_req = parser.OFPFlowStatsRequest(datapath)
        datapath.send_msg(flow_req)

    def _refresh_host_ips(self):
        """Supplements missing host IPs from learned mac_to_ip mappings."""
        hosts = self.topology.get('hosts', {})
        for mac, host_info in hosts.items():
            if host_info.get('ipv4') is None and mac in self.mac_to_ip:
                host_info['ipv4'] = self.mac_to_ip[mac]

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        """Processes port stats to calculate real-time Tx/Rx Mbps."""
        body = ev.msg.body
        dpid = ev.msg.datapath.id
        
        self.traffic.setdefault(dpid, {})
        self.port_stats.setdefault(dpid, {})
        
        for stat in body:
            port_no = stat.port_no
            if port_no >= 65535: # skip OFPP_LOCAL
                 continue
                 
            curr_bytes_rx = stat.rx_bytes
            curr_bytes_tx = stat.tx_bytes
            curr_time = time.time()
            
            if port_no in self.port_stats[dpid]:
                old_bytes_rx = self.port_stats[dpid][port_no]['rx']
                old_bytes_tx = self.port_stats[dpid][port_no]['tx']
                old_time = self.port_stats[dpid][port_no]['time']
                
                dt = curr_time - old_time
                if dt > 0:
                    # Bits per second calculation
                    rx_rate = ((curr_bytes_rx - old_bytes_rx) * 8) / (dt * 1000000.0)
                    tx_rate = ((curr_bytes_tx - old_bytes_tx) * 8) / (dt * 1000000.0)
                    
                    self.traffic[dpid][port_no] = {
                        'rx_mbps': round(rx_rate, 4),
                        'tx_mbps': round(tx_rate, 4)
                    }
            
            self.port_stats[dpid][port_no] = {
                'rx': curr_bytes_rx,
                'tx': curr_bytes_tx,
                'time': curr_time
            }

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply_handler(self, ev):
        """Processes flow stats to calculate source-to-destination bandwidth metrics."""
        body = ev.msg.body
        dpid = ev.msg.datapath.id
        
        self.flow_stats.setdefault(dpid, {})
        
        for stat in body:
            match = stat.match
            if 'ipv4_src' in match and 'ipv4_dst' in match:
                src_ip = match['ipv4_src']
                dst_ip = match['ipv4_dst']
                
                curr_bytes = stat.byte_count
                curr_time = time.time()
                
                flow_key = (src_ip, dst_ip)
                if flow_key in self.flow_stats[dpid]:
                    old_bytes = self.flow_stats[dpid][flow_key]['bytes']
                    old_time = self.flow_stats[dpid][flow_key]['time']
                    
                    dt = curr_time - old_time
                    if dt > 0:
                        mbps = ((curr_bytes - old_bytes) * 8) / (dt * 1000000.0)
                        
                        self.flow_matrix.setdefault(src_ip, {})
                        # Keep maximum across all switches reporting this flow to avoid double counting along path
                        if dst_ip not in self.flow_matrix[src_ip] or mbps > self.flow_matrix[src_ip].get(dst_ip, -1):
                            self.flow_matrix[src_ip][dst_ip] = round(mbps, 4)
                        
                self.flow_stats[dpid][flow_key] = {
                    'bytes': curr_bytes,
                    'time': curr_time
                }

    @set_ev_cls(event.EventSwitchLeave)
    def switch_leave_handler(self, ev):
        """Cleans up internal states when a switch disconnects."""
        try:
            switch = ev.switch
            dpid = switch.dp.id
            if dpid in self.mac_to_port:
                for mac in self.mac_to_port[dpid]:
                    self.mac_to_ip.pop(mac, None)
                del self.mac_to_port[dpid]
            self.logger.warning("Topology: Switch %s LEFT", dpid)
            self._schedule_topology_update()
        except Exception as e:
            self.logger.error("Error in switch_leave_handler: %s", e)
    
    @set_ev_cls(event.EventLinkAdd)
    def link_add_handler(self, ev):
        """Triggers topology update when a link is added."""
        try:
            link = ev.link
            self.logger.info("Topology: Link ADDED s%s:%s -> s%s:%s", link.src.dpid, link.src.port_no, link.dst.dpid, link.dst.port_no)
            self._schedule_topology_update()
        except Exception as e:
            self.logger.error("Error in link_add_handler: %s", e)
    
    @set_ev_cls(event.EventLinkDelete)
    def link_delete_handler(self, ev):
        """Triggers topology update when a link drops."""
        try:
            link = ev.link
            self.logger.warning("Topology: Link DELETED s%s:%s -> s%s:%s", link.src.dpid, link.src.port_no, link.dst.dpid, link.dst.port_no)
            self._schedule_topology_update()
        except Exception as e:
            self.logger.error("Error in link_delete_handler: %s", e)
    
    @set_ev_cls(event.EventHostAdd)
    def host_add_handler(self, ev):
        """Triggers topology update when a host joins."""
        try:
            host = ev.host
            self.logger.info("Topology: Host ADDED %s at s%s:%s", host.mac, host.port.dpid, host.port.port_no)
            self._schedule_topology_update()
        except Exception as e:
            self.logger.error("Error in host_add_handler: %s", e)
    
    def update_topology(self):
        """Compiles the full topology state and increments version if structure changes."""
        try:
            switch_list = get_switch(self, None)
            switches = {}
            for switch in switch_list:
                dpid = switch.dp.id
                switches[str(dpid)] = {
                    'dpid': dpid,
                    'ports': [port.port_no for port in switch.ports if port.port_no < 65535]
                }
            
            links_list = get_link(self, None) # Get all links
            links = []
            for link in links_list:
                links.append({
                    'src_dpid': link.src.dpid,
                    'src_port': link.src.port_no,
                    'dst_dpid': link.dst.dpid,
                    'dst_port': link.dst.port_no
                })
            
            hosts_list = get_host(self, None) # Get all hosts
            hosts = {}
            for host in hosts_list:
                # Use Ryu's discovered IP, or fall back to our own learned IP
                discovered_ip = host.ipv4[0] if host.ipv4 else self.mac_to_ip.get(host.mac)
                hosts[host.mac] = {
                    'mac': host.mac,
                    'ipv4': discovered_ip,
                    'ipv6': host.ipv6[0] if host.ipv6 else None,
                    'port': host.port.port_no,
                    'dpid': host.port.dpid
                }
            
            old_version = self.topology['version'] # Update topology
            
            # Only increment version if the actual structure changed
            old_switches = set(self.topology.get('switches', {}).keys())
            old_links = {(l.get('src_dpid'), l.get('dst_dpid')) for l in self.topology.get('links', [])}
            old_hosts_dict = self.topology.get('hosts', {})
            
            new_switches = set(switches.keys())
            new_links = {(l['src_dpid'], l['dst_dpid']) for l in links}
            
            structure_changed = (old_switches != new_switches or 
                               old_links != new_links or 
                               old_hosts_dict != hosts)
            
            self.topology = {
                'switches': switches,
                'links': links,
                'hosts': hosts,
                'version': old_version + 1 if structure_changed else old_version
            }
            
            if structure_changed:
                self.logger.info("Topology updated to version %s - Switches: %s, Links: %s, Hosts: %s", self.topology['version'], len(switches), len(links), len(hosts))
        except Exception as e:
            self.logger.error("Error updating topology: %s", e)
            self.logger.exception(e)
