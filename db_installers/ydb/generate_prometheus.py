#!/usr/bin/env python3

import argparse
import os
import pathlib
import sys
import yaml


template = """
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: "ydb_dynamic"
    metrics_path: "/counters/counters=ydb/name_label=name/prometheus"
    metric_relabel_configs:
      - source_labels: ["__name__"]
        target_label: "__name__"
        replacement: "ydb_$1"
  - job_name: "utils"
    metrics_path: "/counters/counters=utils/prometheus"
    metric_relabel_configs:
      - source_labels: ["__name__"]
        target_label: "__name__"
        replacement: "utils_$1"
  - job_name: "kqp_dynamic"
    metrics_path: "/counters/counters=kqp/prometheus"
    metric_relabel_configs:
      - source_labels: ["__name__"]
        target_label: "__name__"
        replacement: "kqp_$1"
  - job_name: "tablets_dynamic"
    metrics_path: "/counters/counters=tablets/prometheus"
    metric_relabel_configs:
      - source_labels: ["__name__"]
        target_label: "__name__"
        replacement: "tablets_$1"
  - job_name: "proxy_dynamic"
    metrics_path: "/counters/counters=proxy/prometheus"
    metric_relabel_configs:
      - source_labels: ["__name__"]
        target_label: "__name__"
        replacement: "proxy_$1"
  - job_name: "dsproxynode_dynamic"
    metrics_path: "/counters/counters=dsproxynode/prometheus"
    metric_relabel_configs:
      - source_labels: ["__name__"]
        target_label: "__name__"
        replacement: "dsproxynode_$1"
  - job_name: "ic_dynamic"
    metrics_path: "/counters/counters=interconnect/prometheus"
    metric_relabel_configs:
      - source_labels: ["__name__"]
        target_label: "__name__"
        replacement: "interconnect_$1"
"""

static_port = 8765
dynamic_port = 8766


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hosts", help="Path to file containing the list of YDB hosts")

    args = parser.parse_args()

    if not os.path.isfile(args.hosts):
        print("File {} does not exist".format(args.hosts))
        sys.exit(1)

    with open(args.hosts, "r") as f:
        hosts = f.readlines()

    hosts = [host.strip() for host in hosts]
    static_targets = []
    dynamic_targets = []
    for host in hosts:
       static_target = {
            "targets": ["{}:{}".format(host, static_port)],
            "labels": {
                "container": "ydb-static"
            }
       }
       static_targets.append(static_target)

       dynamic_target = {
           "targets": ["{}:{}".format(host, dynamic_port)],
            "labels": {
                "container": "ydb-dynamic"
            }
       }
       dynamic_targets.append(dynamic_target)

    all_targets = static_targets + dynamic_targets

    prometheus_config = yaml.safe_load(template)

    for job in prometheus_config["scrape_configs"]:
        if job["job_name"].endswith("dynamic"):
            job["static_configs"] = dynamic_targets
        elif job["job_name"].endswith("static"):
            job["static_configs"] = static_targets
        else:
            job["static_configs"] = all_targets

    print(yaml.dump(prometheus_config, default_flow_style=False))


if __name__ == '__main__':
    main()
