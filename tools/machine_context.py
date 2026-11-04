"""Small, cached local environment profile; no network access."""
import json
import os
import platform
import shutil
import subprocess
from functools import lru_cache


def default_shell():
    candidate = os.environ.get('SHELL', '/bin/sh')
    return candidate if os.path.isfile(candidate) and os.access(candidate, os.X_OK) else '/bin/sh'


@lru_cache(maxsize=8)
def profile(shell):
    try:
        distro = platform.freedesktop_os_release() if platform.system() == 'Linux' else {}
    except (OSError, AttributeError):
        distro = {}
    try:
        version = subprocess.run([shell, '-c', 'echo "${BASH_VERSION:-${ZSH_VERSION:-POSIX shell}}"'],
                                 capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        version = 'unknown'
    tools = ('find', 'sed', 'grep', 'awk', 'rsync', 'git', 'ssh', 'curl', 'systemctl',
             'launchctl', 'brew', 'apt', 'dnf', 'pacman', 'zypper', 'apk', 'man')
    return dict(os=platform.system(), release=platform.release(), architecture=platform.machine(),
                distribution=distro.get('PRETTY_NAME', platform.mac_ver()[0]), shell=shell,
                shell_version=version, tools={name: shutil.which(name) for name in tools})


def describe(shell):
    return 'Actual machine profile (use compatible commands):\n' + json.dumps(profile(shell))


def diagnostics(shell, model, url):
    import urllib.request
    lines = [describe(shell)]
    try:
        with urllib.request.urlopen(url.rsplit('/api/', 1)[0] + '/api/tags', timeout=3) as response:
            names = [entry['name'] for entry in json.load(response).get('models', [])]
        present = model in names or model + ':latest' in names
        lines.append(f'Ollama reachable; model {model}: {"installed" if present else "not found"}')
        if not present:
            lines.append('Installed models: ' + ', '.join(names))
    except (OSError, ValueError, KeyError) as exc:
        lines.append(f'Ollama unavailable: {exc}. Start with: ollama serve')
    return '\n'.join(lines)
