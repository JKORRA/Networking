"""
utils.py

This module contains pure functions to compute the differences between
two topology states. It generates human-readable event logs for dynamic
additions or removals of switches, hosts, and links.
"""

import time

def generate_logs(old_topo, new_topo):
    """Compares two JSON topologies and returns a list of event dictionaries."""
    logs = []
    
    for s in new_topo.get('switches', {}).keys():
        if s not in old_topo.get('switches', {}):
            logs.append({"type": "switch", "action": "add", "message": f"Switch s{s} connected."})
    for s in old_topo.get('switches', {}).keys():
        if s not in new_topo.get('switches', {}):
            logs.append({"type": "switch", "action": "remove", "message": f"Switch s{s} disconnected."})

    old_hosts = set(old_topo.get('hosts', {}).keys())
    new_hosts = set(new_topo.get('hosts', {}).keys())
    for h in new_hosts - old_hosts:
        host = new_topo['hosts'][h]
        ip = host.get('ipv4') or host.get('mac')
        logs.append({"type": "host", "action": "add", "message": f"Host {ip} joined at s{host.get('dpid')}."})
    for h in old_hosts - new_hosts:
        host = old_topo['hosts'][h]
        ip = host.get('ipv4') or host.get('mac')
        logs.append({"type": "host", "action": "remove", "message": f"Host {ip} left."})

    old_links = {tuple(sorted((l['src_dpid'], l['dst_dpid']))) for l in old_topo.get('links', [])}
    new_links = {tuple(sorted((l['src_dpid'], l['dst_dpid']))) for l in new_topo.get('links', [])}
    
    for link in new_links - old_links:
        logs.append({"type": "link", "action": "add", "message": f"Link added s{link[0]} - s{link[1]}."})
    for link in old_links - new_links:
        logs.append({"type": "link", "action": "remove", "message": f"Link removed s{link[0]} - s{link[1]}."})

    for log in logs:
        log['timestamp'] = time.strftime("%H:%M:%S")
    
    return logs
