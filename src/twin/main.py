"""
main.py

The main entry point for the Digital Twin application.
This script handles command-line arguments, validates the physical controller's
reachability, fetches the initial baseline topology, and starts the Digital Twin engine.
"""

import argparse
import sys
import time
import traceback
from mininet.log import setLogLevel, info, error

from api_client import TopologyFetcher, RYU_URL
from engine import DigitalTwin, CONTROLLER_IP, CONTROLLER_PORT

def validate_topology(topology):
    """Validates the raw JSON topology for necessary structures."""
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

def check_controller(ip, port):
    """Checks if the given IP and port are reachable (for the local Ryu container)."""
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
    """Main execution loop for parsing args, setting up logging, and launching the twin."""
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
    
    fetcher = TopologyFetcher(RYU_URL)
    topology = fetcher.fetch_topology()
    
    if not validate_topology(topology):
        error("\nERROR: Invalid topology data. Cannot create twin.\n")
        error("\nPlease ensure:\n")
        error("1. RYU controller is running (port 8080)\n")
        error("   ryu-manager --observe-links controller.py\n")
        error("2. Original network is started\n")
        error("   sudo python3 net.py\n")
        error("3. Run 'pingall' in the original network to discover topology\n")
        return 1
    
    twin = DigitalTwin(topology, enable_sync=args.sync)
    
    try:
        twin.create()
        
        if args.sync:
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
