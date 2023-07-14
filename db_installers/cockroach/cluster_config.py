# Config sample for CockroachDB cluster with 3 nodes

DEPLOY_PATH = "/benchmark/cockroach"
DEPLOY_TMP_PATH = "/var/tmp"

HA_PROXY_HOSTS = ["host4.com", "host5.com"]
HA_PROXY_SETUP_PATH = "/benchmark/haproxy"


class Region:
    def __init__(self, name, hosts):
        self.Name = name
        self.Hosts = hosts


Regions = [
    Region("us-west-1", ["host1.com", ]),
    Region("us-west-2", ["host2.com", ]),
    Region("us-west-3", ["host3.com", ]),
]

LISTEN_PORT = 26257
HTTP_PORT = 8080

Disks = [
    "/dev/nvme0n1p2",
    "/dev/nvme1n1p2",
    "/dev/nvme2n1p2",
    "/dev/nvme3n1p2",
]
INIT_PER_DISK = 1

# per host
Cores = 128
CacheSizeGB = 80
SqlMemorySizeGB = 120
