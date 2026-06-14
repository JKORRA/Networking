from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController
from mininet.cli import CLI
from mininet.log import setLogLevel, info
from mininet.link import TCLink

class Topology(Topo): # Mininet topology (can be shaped as you want)
    def build(self):
        # Add switches
        s1 = self.addSwitch('s1')
        s2 = self.addSwitch('s2')
        s3 = self.addSwitch('s3')
        
        # Add hosts
        h1 = self.addHost('h1', ip='10.0.0.1/24')
        h2 = self.addHost('h2', ip='10.0.0.2/24')
        h3 = self.addHost('h3', ip='10.0.0.3/24')
        
        # Add links between hosts and switches
        self.addLink(h1, s1, bw=10, delay='5ms')
        self.addLink(h2, s2, bw=10, delay='5ms')
        self.addLink(h3, s3, bw=10, delay='5ms')
        
        # Add links between switches (linear)
        self.addLink(s1, s2, bw=100, delay='2ms')
        self.addLink(s2, s3, bw=100, delay='2ms')

def run(): # Start the network with remote Ryu controller
    setLogLevel('info')
    
    
    topo = Topology()
    
    # Connect to remote Ryu controller on port 6633
    net = Mininet(
        topo=topo,
        link=TCLink,
        build=False,
        autoSetMacs=True,
        # Note: autoStaticArp is intentionally disabled so that hosts send real
        # ARP packets, allowing Ryu's topology module to discover host IPv4 addresses.
    )

    # Override default controller 
    controller = RemoteController("c1", ip="127.0.0.1", port=6633)
    net.addController(controller)
    net.build()
    net.start()
    try:
        net.waitConnected(timeout=30) # Wait for all switches to connect to the Ryu controller
    except Exception:
        info('WARNING: Not all switches connected within 30s timeout\n')
    
    CLI(net)
    
    info('*** Stopping network\n')
    net.stop()

if __name__ == '__main__':
    run()