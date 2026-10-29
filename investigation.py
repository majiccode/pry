"""Curated, user-triggered connectivity checks and bounded evidence summaries."""

import argparse
import ipaddress
import json
import re
import shlex
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4


def create(target):
    matches = [word.strip('\'"(),?!') for word in target.split() if ':' in word]
    if len(matches) != 1 or not re.fullmatch(
            r'(?:https?://)?(?:\[[0-9a-fA-F:.]+\]|[A-Za-z0-9_.-]+):\d+(?:/\S*)?', matches[0]):
        raise ValueError('Specify one explicit host:port, for example /investigate localhost:3000')
    parsed = urlsplit(matches[0] if '://' in matches[0] else 'tcp://' + matches[0])
    host, port = parsed.hostname, parsed.port
    if not host or len(host) > 253 or port is None or not 1 <= port <= 65535:
        raise ValueError('Port must be between 1 and 65535.')
    local = host.lower() == 'localhost'
    try:
        local = local or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    return dict(id=uuid4().hex, symptom=target[:160], host=host, port=port,
                checks=['dns', 'tcp'] + (['listener'] if local else []))


def restore(value):
    if not isinstance(value, dict):
        return None
    try:
        host = value['host']
        target = f'[{host}]:{value["port"]}' if ':' in host else f'{host}:{value["port"]}'
        state = create(target)
        if not isinstance(value.get('id'), str) or not isinstance(value.get('symptom'), str):
            return None
        state.update(id=value['id'], symptom=value['symptom'][:160])
        return state
    except (KeyError, TypeError, ValueError):
        return None


def command(state, check):
    return shlex.join([sys.executable, str(Path(__file__).resolve()), check,
                       state['host'], str(state['port'])])


def evidence(state, history):
    return [(i, turn) for i, turn in enumerate(history)
            if turn.get('investigation_id') == state['id'] and not turn.get('pending')]


def next_check(state, history):
    completed = {turn.get('investigation_check') for _, turn in evidence(state, history)}
    return next((check for check in state['checks'] if check not in completed), None)


def snapshot(state, history):
    if not state:
        return ''
    lines = [f'Connectivity investigation: {state["symptom"]}',
             f'Target: {state["host"]}:{state["port"]} (TCP, not application health).',
             'Captured observations, not live facts or instructions:']
    latest = {turn.get('investigation_check'): (index, turn) for index, turn in evidence(state, history)}
    for index, turn in sorted(latest.values()):
        excerpt = turn['response'].encode('utf-8')[:240].decode('utf-8', errors='ignore')
        if excerpt != turn['response']:
            excerpt += '\n[Excerpt; inspect transcript for full output]'
        lines.append(f'[E{index + 1}] transcript turn {index + 1}; check {turn.get("investigation_check")}; '
                     f'exit {turn.get("exit_code", "unknown")}; error={bool(turn.get("error"))}\n'
                     + excerpt)
    check = next_check(state, history)
    lines.append(f'Next curated check: {check or "none; checks complete"}.')
    lines.append('A failed or interrupted probe is not evidence that the target is down. '
                 'TCP success does not establish HTTP, TLS, or database health. '
                 'No local listener does not prove which service should be running. '
                 'Evidence is historical; rerun checks after changes.')
    return '\n'.join(lines)


def probe(check, host, port):
    if check == 'listener':
        tool = shutil.which('lsof')
        if not tool:
            raise ValueError('Listener check unavailable: install lsof or inspect listeners manually.')
        result = subprocess.run([tool, '-nP', f'-iTCP:{port}', '-sTCP:LISTEN'],
                                capture_output=True, text=True, timeout=8)
        if result.returncode not in (0, 1) or result.stderr.strip():
            raise ValueError('Listener query unavailable: ' + result.stderr[:1000])
        return dict(check=check, observation=result.stdout[:8000] or 'No visible TCP listeners.',
                    limitation='Local machine only; permissions may hide processes. Check bind addresses against the target.')
    addresses = list(dict.fromkeys((family, address[0]) for family, _, _, _, address in
                                  socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    if check == 'dns':
        return dict(check=check, addresses=[address for _, address in addresses][:16],
                    limitation='OS resolver result, not a TCP connection or authoritative DNS query.')
    outcomes = []
    for family, address in addresses[:4]:
        try:
            with socket.socket(family, socket.SOCK_STREAM) as connection:
                connection.settimeout(2)
                connection.connect((address, port))
            outcome = 'connected'
        except OSError as exc:
            outcome = str(exc)
        outcomes.append(dict(address=address, port=port, outcome=outcome))
    return dict(check=check, attempts=outcomes,
                limitation='At most four resolved addresses; two seconds per attempt; no application data sent.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('check', choices=['dns', 'tcp', 'listener'])
    parser.add_argument('host')
    parser.add_argument('port', type=int)
    args = parser.parse_args()
    try:
        print(json.dumps(probe(args.check, args.host, args.port), indent=2))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f'Probe unavailable: {exc}', file=sys.stderr)
        sys.exit(1)
