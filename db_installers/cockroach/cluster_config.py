# Config sample for CocroachDB cluster with 3 nodes


KIND_NVME = 0
KIND_SSD = 1


class Region:
    def __init__(self, name, nodes):
        self.Nodes = nodes
        self.Name = name


Regions = [
    Region("us-west-1", ["host1.com", ]),
    Region("us-west-2", ["host2.com", ]),
    Region("us-west-3", ["host3.com", ]),
]

Disks = [
    "/dev/nvme0n1p2",
    "/dev/nvme1n1p2",
    "/dev/nvme2n1p2",
    "/dev/nvme3n1p2",
]

Kind = KIND_NVME

# per host
Cores = 128
CacheSizeGB = 80
SqlMemorySizeGB = 120
