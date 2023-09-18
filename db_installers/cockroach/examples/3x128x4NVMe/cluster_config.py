# Config sample for CockroachDB cluster with 3 nodes

DEPLOY_PATH = "/opt/cockroach"
DEPLOY_TMP_PATH = "/var/tmp"

HA_PROXY_HOSTS = None
HA_PROXY_SETUP_PATH = ""


class Region:
    def __init__(self, name, hosts):
        self.Name = name
        self.Hosts = hosts


Regions = [
    Region("us-west-1", ["host-fqdn-001", ]),
    Region("us-west-2", ["host-fqdn-002", ]),
    Region("us-west-3", ["host-fqdn-003", ]),
]

LISTEN_PORT = 26257
HTTP_PORT = 8080

Disks = [
    "/dev/disk/by-partlabel/nvme_01",
    "/dev/disk/by-partlabel/nvme_02",
    "/dev/disk/by-partlabel/nvme_03",
    "/dev/disk/by-partlabel/nvme_04",
]
INIT_PER_DISK = 1

# per host
Cores = 128
CacheSizeGB = 150
SqlMemorySizeGB = 150
