"""UDP beacon discovery + TCP sync for shared notes.

Broadcasts a UDP beacon on the local network so peers can discover this
instance, accepts TCP connections, and synchronizes note text using a
monotonically increasing version (last-writer-wins). All socket I/O runs on
background threads; the UI thread consumes events via :attr:`events`.
"""

import json
import select
import socket
import threading
import time
import uuid
from queue import Empty, Queue

BEACON_PORT = 49153
BEACON_INTERVAL = 3.0
TCP_LISTEN_BACKLOG = 8
SOCKET_TIMEOUT = 5.0


class NetworkNotes:
    def __init__(self, local_name=None, beacon_port=BEACON_PORT, tcp_port=0):
        self.id = uuid.uuid4().hex[:8]
        self.local_name = local_name or socket.gethostname()
        self.beacon_port = beacon_port
        self.tcp_port = tcp_port or 0
        self._lock = threading.Lock()
        self.peers = {}          # peer_id -> {name, host, port, last_seen}
        self.connections = {}    # peer_id -> socket
        self.events = Queue()    # (kind, payload) consumed by the UI thread
        self.outbound = Queue()  # (peer_id, bytes)
        self._running = threading.Event()
        self._version = 0
        self._pending_text = ''
        self._threads = []
        self._server = None
        self._sender_thread = None

    # -- lifecycle -----------------------------------------------------
    def start(self):
        self._running.set()
        self._threads.append(threading.Thread(target=self._beacon_loop,
                                              daemon=True, name='nn-beacon'))
        self._threads.append(threading.Thread(target=self._udp_listen,
                                              daemon=True, name='nn-udp'))
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(('0.0.0.0', self.tcp_port))
        self._server.listen(TCP_LISTEN_BACKLOG)
        self.tcp_port = self._server.getsockname()[1]
        self._server.setblocking(False)
        self._threads.append(threading.Thread(target=self._tcp_accept,
                                              daemon=True, name='nn-tcp-accept'))
        self._sender_thread = threading.Thread(target=self._sender_loop,
                                               daemon=True, name='nn-sender')
        self._sender_thread.start()
        for thread in self._threads:
            thread.start()
        return self.id, self.tcp_port

    def stop(self):
        self._running.clear()
        with self._lock:
            for peer_id, sock in list(self.connections.items()):
                try:
                    sock.close()
                except OSError:
                    pass
            self.connections.clear()
        if self._server:
            try:
                self._server.close()
            except OSError:
                pass

    # -- discovery -----------------------------------------------------
    def _beacon_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.2)
        while self._running.is_set():
            msg = json.dumps({
                'type': 'beacon', 'id': self.id, 'name': self.local_name,
                'port': self.tcp_port,
            }).encode()
            try:
                sock.sendto(msg, ('255.255.255.255', self.beacon_port))
            except OSError:
                pass
            self._running.wait(BEACON_INTERVAL)
        sock.close()

    def _udp_listen(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('0.0.0.0', self.beacon_port))
        except OSError:
            sock.close()
            return
        sock.settimeout(0.2)
        while self._running.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                continue
            try:
                beacon = json.loads(data.decode())
            except (ValueError, UnicodeDecodeError):
                continue
            if beacon.get('type') != 'beacon' or beacon.get('id') == self.id:
                continue
            peer_id = beacon['id']
            entry = {
                'name': beacon.get('name', 'peer'),
                'host': addr[0],
                'port': beacon.get('port', 0),
                'last_seen': time.monotonic(),
            }
            with self._lock:
                self.peers[peer_id] = entry
            self.events.put(('peer_discovered', dict(entry, id=peer_id)))

    # -- TCP -----------------------------------------------------------
    def _tcp_accept(self):
        while self._running.is_set():
            try:
                ready, _, _ = select.select([self._server], [], [], 0.2)
            except (OSError, ValueError):
                return
            if not ready:
                continue
            try:
                conn, _addr = self._server.accept()
            except OSError:
                continue
            conn.settimeout(SOCKET_TIMEOUT)
            threading.Thread(target=self._tcp_recv, args=(conn,),
                             daemon=True, name='nn-tcp-recv').start()

    def _tcp_recv(self, conn):
        peer_id = None
        buffer = b''
        try:
            while self._running.is_set():
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    try:
                        msg = json.loads(line.decode())
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if msg.get('type') != 'sync':
                        continue
                    peer_id = msg.get('id') or peer_id
                    with self._lock:
                        if peer_id:
                            self.connections[peer_id] = conn
                    self.events.put(('sync', msg))
        finally:
            if peer_id:
                with self._lock:
                    self.connections.pop(peer_id, None)
                self.events.put(('peer_disconnected', peer_id))
            try:
                conn.close()
            except OSError:
                pass

    def connect(self, peer_id):
        with self._lock:
            if peer_id in self.connections:
                return True
            peer = self.peers.get(peer_id)
            if not peer or not peer.get('port'):
                return False
            host = peer['host']
            port = peer['port']
        try:
            conn = socket.create_connection((host, port), timeout=SOCKET_TIMEOUT)
        except OSError:
            return False
        conn.settimeout(SOCKET_TIMEOUT)
        threading.Thread(target=self._tcp_recv, args=(conn,),
                         daemon=True, name='nn-tcp-recv').start()
        with self._lock:
            self.connections[peer_id] = conn
        self.send_sync()
        return True

    # -- sync ----------------------------------------------------------
    def set_pending_text(self, text):
        self._pending_text = text

    def send_sync(self, text=None, version=None):
        with self._lock:
            self._version += 1
            version = self._version
            if text is None:
                text = self._pending_text
            payload = (json.dumps({
                'type': 'sync', 'id': self.id, 'version': version, 'text': text,
            }).encode() + b'\n')
            for peer_id in list(self.connections):
                self.outbound.put((peer_id, payload))

    def _sender_loop(self):
        while self._running.is_set():
            try:
                peer_id, payload = self.outbound.get(timeout=0.2)
            except Empty:
                continue
            with self._lock:
                conn = self.connections.get(peer_id)
            if conn is None:
                continue
            try:
                conn.sendall(payload)
            except OSError:
                with self._lock:
                    self.connections.pop(peer_id, None)
                self.events.put(('peer_disconnected', peer_id))

    # -- query ---------------------------------------------------------
    def list_peers(self):
        with self._lock:
            return {pid: dict(p) for pid, p in self.peers.items()}
