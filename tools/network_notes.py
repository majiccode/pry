"""TCP sync for shared notes (no broadcast).

Accepts TCP connections and synchronizes note text using a
monotonically increasing version (last-writer-wins). All socket I/O runs on
background threads; the UI thread consumes events via :attr:`events`.

Peers are added manually via :meth:`add_peer` (host:port) -- there is no
UDP broadcasting, so this will not flood WiFi networks.
"""

import json
import select
import socket
import threading
import uuid
from queue import Empty, Queue

TCP_LISTEN_BACKLOG = 8
SOCKET_TIMEOUT = 5.0
DEBOUNCE_INTERVAL = 1.0
DEFAULT_PEER_HOST = '127.0.0.1'
DEFAULT_PEER_PORT = 8081


class NetworkNotes:
    def __init__(self, local_name=None, tcp_port=0):
        self.id = uuid.uuid4().hex[:8]
        self.local_name = local_name or socket.gethostname()
        self.tcp_port = tcp_port or 0
        self._lock = threading.Lock()
        self.peers = {}          # peer_id -> {name, host, port}
        self.connections = {}    # peer_id -> socket
        self.events = Queue()    # (kind, payload) consumed by the UI thread
        self.outbound = Queue()  # (peer_id, bytes)
        self._running = threading.Event()
        self._version = 0
        self._pending_text = ''
        self._pending_version = 0
        self._debounce_timer = None
        self._started = False
        self._threads = []
        self._server = None
        self._sender_thread = None

    # -- lifecycle -----------------------------------------------------
    def start(self):
        if self._started:
            return self.id, self.tcp_port
        self._started = True
        self._running.set()
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._server.bind(('0.0.0.0', DEFAULT_PEER_PORT))
            self.tcp_port = DEFAULT_PEER_PORT
        except OSError:
            self._server.bind(('0.0.0.0', 0))
            self.tcp_port = self._server.getsockname()[1]
        self._server.listen(TCP_LISTEN_BACKLOG)
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
        self._started = False
        self._cancel_debounce()
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
        for thread in self._threads:
            try:
                thread.join(timeout=1.0)
            except (RuntimeError, AttributeError):
                pass
        self._threads = []

    # -- peer management -----------------------------------------------
    def add_peer(self, peer_id, host, port, name=None):
        with self._lock:
            self.peers[peer_id] = {
                'name': name or host,
                'host': host,
                'port': port,
            }
        self.events.put(('peer_discovered', dict(self.peers[peer_id], id=peer_id)))

    def remove_peer(self, peer_id):
        with self._lock:
            self.peers.pop(peer_id, None)
            sock = self.connections.pop(peer_id, None)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        self.events.put(('peer_disconnected', peer_id))

    def list_peers(self):
        with self._lock:
            return {pid: dict(p) for pid, p in self.peers.items()}

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
                        old = self.connections.get(peer_id)
                        if old is not None and old is not conn:
                            try:
                                old.close()
                            except OSError:
                                pass
                        if peer_id:
                            self.connections[peer_id] = conn
                    self.events.put(('sync', msg))
        finally:
            if peer_id:
                with self._lock:
                    if self.connections.get(peer_id) is conn:
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
        self.send_sync(force=True)
        return True

    # -- sync ----------------------------------------------------------
    def set_pending_text(self, text):
        self._pending_text = text

    def _cancel_debounce(self):
        timer = self._debounce_timer
        if timer is not None:
            timer.cancel()
            self._debounce_timer = None

    def _flush_pending(self):
        with self._lock:
            version = self._pending_version
            text = self._pending_text
            self._debounce_timer = None
        payload = (json.dumps({
            'type': 'sync', 'id': self.id, 'version': version, 'text': text,
        }).encode() + b'\n')
        with self._lock:
            for peer_id in list(self.connections):
                self.outbound.put((peer_id, payload))

    def send_sync(self, text=None, force=False):
        with self._lock:
            self._version += 1
            version = self._version
            if text is None:
                text = self._pending_text
            self._pending_text = text
            self._pending_version = version
        if force:
            self._cancel_debounce()
            self._flush_pending()
        else:
            self._cancel_debounce()
            self._debounce_timer = threading.Timer(DEBOUNCE_INTERVAL, self._flush_pending)
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

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
