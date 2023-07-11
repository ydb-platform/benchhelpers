# Config sample for YugabyteDB cluster with 3 nodes

DEPLOY_PATH = "/benchmark/yugabyte"
DEPLOY_TMP_PATH = "/var/tmp"


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
LOCAL_IP = {'host1.com': 'localhost1', 'host2.com': 'localhost2', 'host3.com': 'localhost3'}

# More information can be found here https://docs.yugabyte.com/preview/reference/configuration/default-ports/
LISTEN_PORT_MASTER = 7100
LISTEN_PORT_SERVER = 9100
PSQL_PORT = 5433
CQL_PORT = 9042
REDIS_WEBSERVER_PORT = 11001
MASTER_WEBSERVER_PORT = 7000
SERVER_WEBSERVER_PORT = 9000
CQL_WEBSERVER_PORT = 12000
PSQL_WEBSERVER_PORT = 13000

Disks = [
    "/dev/nvme0n1p2",
    "/dev/nvme1n1p2",
    "/dev/nvme2n1p2",
    "/dev/nvme3n1p2",
]

Cores = 128
Cache = "20GB"
SqlMemory = "30GB"
