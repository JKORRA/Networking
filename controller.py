from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.lib import hub
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, arp, ipv4 as ipv4_pkt
from ryu.topology import event
from ryu.topology.api import get_switch, get_link, get_host
from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from webob import Response
import json
import time

api_instance_name = 'api_app'

class NetworkController(app_manager.RyuApp):
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
        
        self.datapaths = {} # Track datapaths
        
        self.mac_to_ip = {} # Learn IP from any packet (ARP or IPv4)
        
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
    def state_change_handler(self, ev): # Handle datapath state changes
        datapath = ev.datapath
        
        if ev.state == MAIN_DISPATCHER: # Negotiation between RYU and OF Switch must be completed
            if datapath.id not in self.datapaths:
                self.logger.info("Switch datapath id %s CONNECTED", datapath.id)
                self.datapaths[datapath.id] = datapath
        elif ev.state == DEAD_DISPATCHER:
            if datapath.id in self.datapaths:
                self.logger.warning("Switch datapath id %s DISCONNECTED", datapath.id)
                del self.datapaths[datapath.id]
    
    # Code source: https://osrg.github.io/ryu-book/en/html/switching_hub.html
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER) # Waiting to receive SwitchFeatures message
    def switch_features_handler(self, ev): # Handle OF switch connection
        try: # RYU gets this reply from a previously sent request to the switch
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
            
            self.update_topology() # Trigger topology update
        except Exception as e:
            self.logger.error("Error in switch_features_handler: %s", e)
            self.logger.exception(e)
    
    def add_flow(self, datapath, priority, match, actions, buffer_id=None): # Add a flow entry to the switch
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
    def packet_in_handler(self, ev): # Handle packet-in messages
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
            
            # Learn IP from ARP or IPv4 packets
            arp_pkt_check = pkt.get_protocol(arp.arp)
            ip_pkt = pkt.get_protocol(ipv4_pkt.ipv4)
            if arp_pkt_check is not None:
                self.mac_to_ip[arp_pkt_check.src_mac] = arp_pkt_check.src_ip
            elif ip_pkt is not None:
                self.mac_to_ip[src] = ip_pkt.src
            
            if dst in self.mac_to_port[dpid]:
                out_port = self.mac_to_port[dpid][dst]
            else:
                out_port = ofproto.OFPP_FLOOD
            
            actions = [parser.OFPActionOutput(out_port)]

            # Always flood ARP (helps dynamic host addition)
            arp_pkt = pkt.get_protocol(arp.arp)
            if arp_pkt is not None:
                out_port = ofproto.OFPP_FLOOD
                actions = [parser.OFPActionOutput(out_port)]
            
            # L2 learning-switch flow for known unicast destinations
            if out_port != ofproto.OFPP_FLOOD:
                if ip_pkt is not None:
                    match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, eth_dst=dst, eth_src=src, ipv4_src=ip_pkt.src, ipv4_dst=ip_pkt.dst)
                else:
                    match = parser.OFPMatch(eth_dst=dst)
                if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                    self.add_flow(datapath, 1, match, actions, msg.buffer_id)
                    return
                else:
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
    def switch_enter_handler(self, ev): # Handle switch addition
        try:
            switch = ev.switch
            self.logger.info("Topology: Switch %s ENTERED", switch.dp.id)
            self.update_topology()
        except Exception as e:
            self.logger.error("Error in switch_enter_handler: %s", e)
    
    def _monitor(self): # Request port stats and refresh host IPs periodically
        while True:
            # Optionally clear inactive flows
            self.flow_matrix.clear()
            for dp in self.datapaths.values():
                self._request_stats(dp)
            self._refresh_host_ips() # Supplement IPs without changing version
            hub.sleep(5) # polling interval
            
    def _request_stats(self, datapath):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        req = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
        datapath.send_msg(req)
        
        flow_req = parser.OFPFlowStatsRequest(datapath)
        datapath.send_msg(flow_req)

    def _refresh_host_ips(self): # Supplement missing host IPs from learned mac_to_ip mapping
        hosts = self.topology.get('hosts', {})
        for mac, host_info in hosts.items():
            if host_info.get('ipv4') is None and mac in self.mac_to_ip:
                host_info['ipv4'] = self.mac_to_ip[mac]

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev): # Handle port stats replies
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
    def switch_leave_handler(self, ev): # Handle switch removal
        try:
            switch = ev.switch
            dpid = switch.dp.id
            if dpid in self.mac_to_port:
                for mac in self.mac_to_port[dpid]:
                    self.mac_to_ip.pop(mac, None)
                del self.mac_to_port[dpid]
            self.logger.warning("Topology: Switch %s LEFT", dpid)
            self.update_topology()
        except Exception as e:
            self.logger.error("Error in switch_leave_handler: %s", e)
    
    @set_ev_cls(event.EventLinkAdd)
    def link_add_handler(self, ev): # Handle link addition
        try:
            link = ev.link
            self.logger.info("Topology: Link ADDED s%s:%s -> s%s:%s", link.src.dpid, link.src.port_no, link.dst.dpid, link.dst.port_no)
            self.update_topology()
        except Exception as e:
            self.logger.error("Error in link_add_handler: %s", e)
    
    @set_ev_cls(event.EventLinkDelete)
    def link_delete_handler(self, ev): # Handle link deletion
        try:
            link = ev.link
            self.logger.warning("Topology: Link DELETED s%s:%s -> s%s:%s", link.src.dpid, link.src.port_no, link.dst.dpid, link.dst.port_no)
            self.update_topology()
        except Exception as e:
            self.logger.error("Error in link_delete_handler: %s", e)
    
    @set_ev_cls(event.EventHostAdd)
    def host_add_handler(self, ev): # Handle host addition
        try:
            host = ev.host
            self.logger.info("Topology: Host ADDED %s at s%s:%s", host.mac, host.port.dpid, host.port.port_no)
            self.update_topology()
        except Exception as e:
            self.logger.error("Error in host_add_handler: %s", e)
    
    def update_topology(self): # Update topology information
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
            

class NetworkAPI(ControllerBase): # REST API for topology exposure
    def __init__(self, req, link, data, **config):
        super(NetworkAPI, self).__init__(req, link, data, **config)
        self.controller = data[api_instance_name]
        self.secret_token = 'Bearer SDN-Twin-Secret-Token-2026'

    def _check_auth(self, req):
        if req.headers.get('Authorization') != self.secret_token:
            return Response(status=401, body='{"error": "Unauthorized"}', content_type='application/json')
        return None
    
    @route('topology', '/api/topology', methods=['GET'])
    def get_topology(self, req, **kwargs):
        auth_resp = self._check_auth(req)
        if auth_resp: return auth_resp
        
        version = str(self.controller.topology['version'])
        if req.headers.get('If-None-Match') == version:
            return Response(status=304)

        body = json.dumps(self.controller.topology.copy(), indent=2)
        resp = Response(
            content_type='application/json',
            body=body.encode('utf-8')
        )
        resp.headers['ETag'] = version
        return resp
    
    @route('switches', '/api/switches', methods=['GET'])
    def get_switches(self, req, **kwargs):
        auth_resp = self._check_auth(req)
        if auth_resp: return auth_resp
        
        body = json.dumps(self.controller.topology['switches'].copy(), indent=2)
        return Response(
            content_type='application/json',
            body=body.encode('utf-8')
        )
    
    @route('links', '/api/links', methods=['GET'])
    def get_links(self, req, **kwargs):
        auth_resp = self._check_auth(req)
        if auth_resp: return auth_resp
        
        body = json.dumps(list(self.controller.topology['links']), indent=2)
        return Response(
            content_type='application/json',
            body=body.encode('utf-8')
        )
    
    @route('hosts', '/api/hosts', methods=['GET'])
    def get_hosts(self, req, **kwargs):
        auth_resp = self._check_auth(req)
        if auth_resp: return auth_resp
        
        body = json.dumps(self.controller.topology['hosts'].copy(), indent=2)
        return Response(
            content_type='application/json',
            body=body.encode('utf-8')
        )

    @route('version', '/api/version', methods=['GET'])
    def get_version(self, req, **kwargs):
        auth_resp = self._check_auth(req)
        if auth_resp: return auth_resp
        
        version_info = {
            'version': self.controller.topology['version']
        }
        body = json.dumps(version_info, indent=2)
        return Response(
            content_type='application/json',
            body=body.encode('utf-8')
        )

    @route('traffic', '/api/traffic', methods=['GET'])
    def get_traffic(self, req, **kwargs):
        auth_resp = self._check_auth(req)
        if auth_resp: return auth_resp
        
        body = json.dumps(self.controller.traffic.copy(), indent=2)
        return Response(
            content_type='application/json',
            body=body.encode('utf-8')
        )
        
    @route('flows', '/api/flows', methods=['GET'])
    def get_flows(self, req, **kwargs):
        auth_resp = self._check_auth(req)
        if auth_resp: return auth_resp
        
        body = json.dumps(self.controller.flow_matrix.copy(), indent=2)
        return Response(
            content_type='application/json',
            body=body.encode('utf-8')
        )