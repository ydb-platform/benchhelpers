# Config sample for YugabyteDB cluster with 3 nodes

DEPLOY_PATH = "/benchmark/yugabyte"
DEPLOY_TMP_PATH = "/var/tmp"

KIND_NVME = 0
KIND_SSD = 1


class Region:
    def __init__(self, name, nodes):
        self.Nodes = nodes
        self.Name = name


# For now regions are ignored
Regions = [
    Region("us-west-1", ["host1.com", ]),
    Region("us-west-1", ["host2.com", ]),
    Region("us-west-1", ["host3.com", ]),
]

Disks = [
    "/dev/nvme0n1p2",
    "/dev/nvme1n1p2",
    "/dev/nvme2n1p2",
    "/dev/nvme3n1p2",
]

Kind = KIND_NVME

Cores = 128
Cache = "20GB"
SqlMemory = "30GB"
