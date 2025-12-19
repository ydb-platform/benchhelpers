#!/usr/bin/env python3
import os
import re
import sys
import time
import requests
from argparse import ArgumentParser
from urllib.parse import quote_plus

# TODO: eliminate global variable
VIEWER_URL_BASE = ''

VIEWER_HEADERS = {}

URL_TABLE_DESCRIPTION = '{url_base}/viewer/json/describe?path={path}&enums=true'
URL_EXECUTOR_INTERNALS = '{url_base}/tablets/executorInternals?TabletID={tablet_id}'
URL_FORCE_COMPACT = '{url_base}/tablets/executorInternals?TabletID={tablet_id}&force_compaction={local_table_id}'
RE_DBASE_SIZE = re.compile(r'DBase{.*?, (\d+)\)b}', re.S)
RE_LOANED_PARTS = re.compile(r'<h4>Loaned parts</h4><pre>(.*?)</pre>', re.S)
RE_FORCED_COMPACTION_STATE = re.compile(r'Forced compaction: (\w+)', re.S)


def load_json(url):
    response = requests.get(url, headers=VIEWER_HEADERS, verify=False)
    response.raise_for_status()
    return response.json()


def describe_table(path):
    url = URL_TABLE_DESCRIPTION.format(url_base=VIEWER_URL_BASE, path=quote_plus(path))
    return load_json(url)


def tablet_internals(tablet_id):
    url = URL_EXECUTOR_INTERNALS.format(url_base=VIEWER_URL_BASE, tablet_id=tablet_id)
    response = requests.get(url, headers=VIEWER_HEADERS, verify=False)
    response.raise_for_status()
    return response.text


def extract_loaned_parts(text):
    m = RE_LOANED_PARTS.search(text)
    if m:
        return m.group(1).split()
    else:
        return None


def extract_force_compaction_state(text):
    m = RE_FORCED_COMPACTION_STATE.search(text)
    if m:
        return m.group(1)
    else:
        return None


def start_force_compaction(tablet_id, local_table_id=1001):
    url = URL_FORCE_COMPACT.format(url_base=VIEWER_URL_BASE, tablet_id=tablet_id, local_table_id=local_table_id)
    response = requests.get(url, headers=VIEWER_HEADERS, verify=False)
    response.raise_for_status()
    text = response.text
    if 'Table will be compacted in the near future' not in text:
        print(text)


def force_compact(tablet_id, local_table_id=1001):
    state = extract_force_compaction_state(tablet_internals(tablet_id))
    if state is None:
        start_force_compaction(tablet_id, local_table_id)
        time.sleep(0.1)
    while True:
        prev_state = state
        state = extract_force_compaction_state(tablet_internals(tablet_id))
        if state is None:
            break
        if state != 'Compacting' and state != prev_state:
            print(f'... {state}')
        time.sleep(1)


def main():
    parser = ArgumentParser()
    parser.add_argument('--threads', type=int, default=10)
    parser.add_argument('--viewer-url')
    parser.add_argument('--auth', dest="auth_mode", default='OAuth')
    parser.add_argument('--token', dest="token_file", default='~/.ydb/token')
    parser.add_argument('--all', action='store_true')
    parser.add_argument('table')
    args = parser.parse_args()

    global VIEWER_HEADERS

    access_token = os.getenv("YDB_ACCESS_TOKEN_CREDENTIALS")
    anonymous_token = os.getenv("YDB_ANONYMOUS_CREDENTIALS")

    if args.auth_mode=='' or args.auth_mode.lower()=='disabled' or anonymous_token:
        VIEWER_HEADERS = {}
    elif args.token_file and os.path.isfile(args.token_file):
        token_path = os.path.expanduser(args.token_file)
        if not os.path.isfile(token_path):
            print(f"{token_path} does not exist")
            sys.exit(1)

        token = open(token_path).read().strip()
        VIEWER_HEADERS = {
            'Authorization': str(args.auth_mode) + ' ' + token,
        }
    elif access_token:
        VIEWER_HEADERS = {
            'Authorization': 'OAuth ' + token,
        }

    # TODO: eliminate global variable
    global VIEWER_URL_BASE
    VIEWER_URL_BASE = args.viewer_url

    tablet_ids = []
    for p in describe_table(args.table)['PathDescription']['TablePartitions']:
        tablet_ids.append(int(p['DatashardId']))
    tablet_ids.sort()

    def generate_tasks():
        for i, tablet_id in enumerate(tablet_ids):
            yield i + 1, len(tablet_ids), tablet_id

    def process_task(task):
        index, count, tablet_id = task
        if not args.all and not extract_loaned_parts(tablet_internals(tablet_id)):
            print(f'[{time.ctime()}] [{index}/{count}] Skip {tablet_id}')
            return

        tablet_url = URL_EXECUTOR_INTERNALS.format(url_base=VIEWER_URL_BASE, tablet_id=tablet_id)
        print(f'[{time.ctime()}] [{index}/{count}] Compacting {tablet_id} url: {tablet_url}')
        force_compact(tablet_id)
        if extract_loaned_parts(tablet_internals(tablet_id)):
            print(f'[{time.ctime()}] [{index}/{count}] !!! WARNING !!! Tablet {tablet_id} has loaned parts after compaction')

    from multiprocessing.pool import ThreadPool

    with ThreadPool(args.threads) as pool:
        for _ in pool.imap_unordered(process_task, generate_tasks()):
            pass


if __name__ == '__main__':
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    main()
