"""TCP sync for shared notes (no broadcast).

Accepts TCP connections and synchronizes note text using a
monotonically increasing version (last-writer-wins). All socket I/O runs on
background threads; the UI thread consumes events via :attr:`events`.

Peers are added manually via :meth:`add_peer` (host:port) -- there is no
UDP broadcasting, so this will not flood WiFi networks.
"""

import base64
import json
import select
import socket
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Queue
from pathlib import Path

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
        self.user_id_to_peer = {}  # user_id -> peer_id
        self.events = Queue()    # (kind, payload) consumed by the UI thread
        self.outbound = Queue()  # (peer_id, bytes)
        self.pending_file_requests = {}  # request_id -> (peer_id, path, callback)
        self._running = threading.Event()
        self._version = 0
        self._pending_text = ''
        self._pending_version = 0
        self._debounce_timer = None
        self._started = False
        self._threads = []
        self._server = None
        self._sender_thread = None
        self._sender_token = None
        self._discovering = threading.Event()

    # -- lifecycle -----------------------------------------------------
    def _spin_threads(self):
        if self._server is not None:
            self._server.listen(TCP_LISTEN_BACKLOG)
            self._server.setblocking(False)
            self._threads.append(threading.Thread(target=self._tcp_accept, args=(self._server,),
                                                  daemon=True, name='nn-tcp-accept'))
        token = object()
        self._sender_token = token
        self._sender_thread = threading.Thread(target=self._sender_loop, args=(token,),
                                               daemon=True, name='nn-sender')
        self._sender_thread.start()
        for thread in self._threads:
            thread.start()

    def ensure_started(self):
        """Prepare client-side sync I/O (no listener) if not running yet."""
        if self._running.is_set():
            return True
        self.start()
        return True

    def start(self, port=None):
        """Start sync I/O. With `port` (server role) also listen on that port;
        without it, run as a pure client (connections are opened by us)."""
        if self._started:
            return True
        self._started = True
        self._running.set()
        self._server = None
        self.tcp_port = 0
        if port is not None:
            self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self._server.bind(('0.0.0.0', port))
                self.tcp_port = port
            except OSError:
                self._server.bind(('0.0.0.0', 0))
                self.tcp_port = self._server.getsockname()[1]
        self._spin_threads()
        return True

    def start_server(self, port=0):
        """Become the server on an explicit port; False when the port is taken.

        Restarting on a different port while running is supported.
        """
        if self._running.is_set():
            if self._server is not None and (not port or self.tcp_port == port):
                return True
            self.stop()
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(('0.0.0.0', port))
        except OSError:
            return False
        finally:
            probe.close()
        self.start(port=port)
        return True

    def stop(self):
        self._running.clear()
        self._started = False
        self._cancel_debounce()
        self._sender_token = None
        with self._lock:
            for peer_id, sock in list(self.connections.items()):
                try:
                    sock.close()
                except OSError:
                    pass
            self.connections.clear()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
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

    def has_connection(self):
        with self._lock:
            return bool(self.connections)

    def connected_endpoints(self):
        with self._lock:
            return sorted(self.connections)

    # -- TCP -----------------------------------------------------------
    def _tcp_accept(self, server=None):
        if server is None:
            return
        while self._running.is_set():
            try:
                ready, _, _ = select.select([server], [], [], 0.2)
            except (OSError, ValueError):
                return
            if not ready:
                continue
            try:
                conn, _addr = server.accept()
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
                    msg_type = msg.get('type')
                    if msg_type == 'sync':
                        sender_id = msg.get('id')
                        if sender_id:
                            self.user_id_to_peer[sender_id] = sender_id
                        peer_id = sender_id or peer_id
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
                        if peer_id and old is None and self._pending_text:
                            # Greet a new accepted peer with our current notes; the
                            # client's own (possibly empty) initial push cannot win.
                            self.send_sync(force=True)
                    elif msg_type == 'file_request':
                        self._handle_file_request(msg, conn)
                    elif msg_type == 'file_response':
                        self._handle_file_response(msg)
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

    def connect(self, peer_id, timeout=SOCKET_TIMEOUT):
        with self._lock:
            if peer_id in self.connections:
                return True
            peer = self.peers.get(peer_id)
            if not peer or not peer.get('port'):
                return False
            host = peer['host']
            port = peer['port']
        try:
            conn = socket.create_connection((host, port), timeout=timeout)
        except OSError:
            return False
        conn.settimeout(SOCKET_TIMEOUT)
        threading.Thread(target=self._tcp_recv, args=(conn,),
                         daemon=True, name='nn-tcp-recv').start()
        with self._lock:
            self.connections[peer_id] = conn
        self.send_sync(force=True)
        return True

    def connect_async(self, peer_id, host, port, name=None, timeout=3.0):
        """Connect on a worker thread; posts ('connect_result', payload) either way."""
        self.ensure_started()
        self.add_peer(peer_id, host, port, name=name)
        if peer_id in self.connected_endpoints():
            self.events.put(('connect_result',
                             {'id': peer_id, 'host': host, 'port': port, 'success': True}))
            return
        def worker():
            try:
                conn = socket.create_connection((host, port), timeout=timeout)
            except OSError:
                self.events.put(('connect_result',
                                 {'id': peer_id, 'host': host, 'port': port, 'success': False}))
                return
            conn.settimeout(SOCKET_TIMEOUT)
            threading.Thread(target=self._tcp_recv, args=(conn,),
                             daemon=True, name='nn-tcp-recv').start()
            with self._lock:
                self.connections[peer_id] = conn
            self.send_sync(force=True)
            self.events.put(('connect_result',
                             {'id': peer_id, 'host': host, 'port': port, 'success': True}))
        threading.Thread(target=worker, daemon=True, name='nn-tcp-connect').start()

    # -- discovery -----------------------------------------------------
    def local_addresses(self):
        """(own address, candidates) for the primary interface's /24 subnet."""
        own = None
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(('1.1.1.1', 9))  # no packets sent; picks the route source
            own = probe.getsockname()[0]
        except OSError:
            pass
        finally:
            probe.close()
        if not own or own.startswith('127.'):
            try:
                own = socket.gethostbyname(socket.gethostname())
            except OSError:
                own = None
        parts = (own or '').split('.')
        if len(parts) != 4 or not all(p.isdigit() for p in parts):
            return own or '', []
        candidates = [p for p in (f'{".".join(parts[:3])}.{n}' for n in range(1, 255))
                      if p != own]
        return own, candidates

    def discover(self, port=DEFAULT_PEER_PORT, timeout=0.25):
        """Probe each /24 address for a Pry notes server; False when already scanning.

        Posts ('discover_started', ...) then ('discover_done', {found: [hosts]}).
        """
        if self._discovering.is_set():
            return False
        self._discovering.set()
        self.ensure_started()
        def worker():
            try:
                own, candidates = self.local_addresses()
                self.events.put(('discover_started',
                                 {'count': len(candidates), 'port': port, 'own': own}))
                found = []
                def probe(address):
                    try:
                        sock = socket.create_connection((address, port), timeout=timeout)
                        sock.close()
                        return address
                    except OSError:
                        return None
                with ThreadPoolExecutor(max_workers=32) as pool:
                    found = [host for host in pool.map(probe, candidates) if host]
                self.events.put(('discover_done', {'port': port, 'found': found}))
            finally:
                self._discovering.clear()
        threading.Thread(target=worker, daemon=True, name='nn-discover').start()
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

    def _sender_loop(self, token=None):
        while token is self._sender_token and self._running.is_set():
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

    def get_peer_for_user(self, user_id):
        """Return the peer_id (connection key) for a given user_id, if known."""
        with self._lock:
            return self.user_id_to_peer.get(user_id)

    # -- file transfer ---------------------------------------------------
    def request_file(self, peer_id, path, callback):
        """Request a file from a peer. Calls callback(data, error) on the network thread."""
        request_id = uuid.uuid4().hex[:8]
        with self._lock:
            self.pending_file_requests[request_id] = (peer_id, path, callback)
        payload = (json.dumps({
            'type': 'file_request', 'id': self.id, 'request_id': request_id, 'path': path,
        }).encode() + b'\n')
        with self._lock:
            conn = self.connections.get(peer_id)
        if conn is not None:
            self.outbound.put((peer_id, payload))
        else:
            with self._lock:
                self.pending_file_requests.pop(request_id, None)
            callback(None, 'Not connected to peer')

    def _handle_file_request(self, msg, conn):
        sender_id = msg.get('id')
        if sender_id:
            self.user_id_to_peer[sender_id] = sender_id
        path = msg.get('path')
        request_id = msg.get('request_id')
        peer_id = msg.get('id')
        try:
            data = Path(path).read_bytes()
            encoded = base64.b64encode(data).decode('ascii')
        except OSError as exc:
            encoded = None
            error = str(exc)
        else:
            error = None
        response = (json.dumps({
            'type': 'file_response', 'id': self.id, 'request_id': request_id,
            'path': path, 'data': encoded, 'error': error,
        }).encode() + b'\n')
        with self._lock:
            target = self.connections.get(peer_id)
        if target is not None:
            try:
                target.sendall(response)
            except OSError:
                pass

    def _handle_file_response(self, msg):
        request_id = msg.get('request_id')
        with self._lock:
            pending = self.pending_file_requests.pop(request_id, None)
        if pending is None:
            return
        peer_id, path, callback = pending
        data = msg.get('data')
        error = msg.get('error')
        if data is not None:
            try:
                file_data = base64.b64decode(data)
                callback(file_data, None)
            except ValueError as exc:
                callback(None, str(exc))
        else:
            callback(None, error or 'File unavailable')
