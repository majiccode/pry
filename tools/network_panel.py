"""Network browser using the shared terminal panel navigation and rendering."""
import threading
from copy import deepcopy
from git_panel import GitPanel
from network_workspace import Network


class NetworkPanel(GitPanel):
    _cached: dict | None = None
    tabs = [('hosts','Servers'), ('shares','Shares / folders'), ('mounts','Mounted'), ('tools','Tools')]
    list_title = 'Servers / shares / mounts'
    output_title = 'Connection details / results'
    footer = 'Enter: server lists / SMB share mounts / mounted folder browses · Tab focus · Esc close'
    hints = {}

    def __init__(self, directory, address=''):
        self.hosts, self.shares, self.mounted = [], [], []
        self.current = None
        self.address = address
        super().__init__(directory, auto_refresh=False)
        self.tab = 'hosts'
        if self._cached:
            for name, value in deepcopy(self._cached).items():
                setattr(self, name, value)
        self.output = self.overview()
        self.message = 'Ready · saved results from this app session · Discover refreshes servers' if self.hosts else 'Ready · choose Discover to find servers, or Add server'
        if address:
            self.address = ''
            self.start('add', address)

    def heading(self):
        return 'NETWORK · SMB / NFS / SFTP', (self.current['label'] if self.current else 'Discover advertised services or add a server address')

    def entries(self):
        return {'hosts':self.hosts, 'shares':self.shares, 'mounts':self.mounted,
                'tools':Network().tools()}[self.tab]

    def actions(self):
        return [('List / inspect','list'), ('Add server','add'), ('Discover','refresh'),
                ('Mount','mount'), ('Connect','connect'), ('SSH shell','shell'),
                ('Browse mount','browse'), ('Unmount','unmount'), ('Refresh mounts','mounts'),
                ('Ask AI','explain'), ('Close','close')]

    def entry_action(self):
        if self.tab == 'mounts':
            return 'browse'
        item = self.selected()
        if self.tab == 'shares' and item and item.get('protocol') == 'smb':
            return 'open_share'
        return 'list'

    def overview(self):
        return ('SMB: Windows, Mac and NAS shares. NFS: Unix exports. SFTP: SSH file transfer.\n'
                'Add server: smb://host/share, nfs://host/export, sftp://user@host:22/path.\n'
                'Listing uses existing keys/agent or noninteractive access. SMB Connect uses Finder on macOS, smbclient on Linux.\n'
                'Click/Enter on an SMB share mounts it on macOS and shows its local folder in Mounted.\n'
                'The Mount action chooses a custom mount directory; commands open for review in the editor.\n'
                'Use an existing empty mount directory. Refresh mounts after connecting, then Browse mount.\n'
                'SMB listing may require login; NFSv4 servers may not advertise exports.\n'
                'SSHFS is optional; SFTP Connect works without it. Discovery checks mDNS and known network neighbors.\n'
                'TCP-open results indicate a reachable port, not verified credentials or protocol support.')

    def start(self, action, value='', entry=None):
        if self.busy:
            return
        self.busy = True
        self.cancel.clear()
        self.message = 'Working · ' + action + ' · Ctrl+C cancels'
        if entry:
            self.current = entry
        def worker():
            network = Network(self.cancel)
            try:
                if action == 'refresh':
                    hosts, output = network.discover(lambda entries:self.results.put(('hosts',entries)))
                    self.results.put(('hosts',hosts))
                    self.results.put(('mounts',network.mounts()))
                elif action == 'mounts':
                    self.results.put(('mounts',network.mounts()))
                    output = 'Mounted filesystems refreshed.'
                elif action == 'add':
                    item = network.location(value)
                    self.results.put(('add',item))
                    output = 'Server added. Select List / inspect to query it.'
                elif action == 'open_share':
                    mounted = network.mount_share(entry)
                    mounts = network.mounts()
                    self.results.put(('mounted_share', (mounted, mounts)))
                    output = ('Share mounted at: ' + mounted['mountpoint'] + '\n'
                              'Click the mounted folder or press Enter to browse.\n'
                              'Use this local path as the destination when copying files.')
                else:
                    entries, output = network.listing(entry)
                    self.results.put(('shares',(entries,entry)))
                self.results.put(('done',output))
            except (OSError,ValueError) as exc:
                self.results.put(('error',str(exc)))
        self.thread = threading.Thread(target=worker,daemon=True)
        self.thread.start()

    def poll(self):
        updated = False
        while not self.results.empty():
            kind, value = self.results.get()
            if kind == 'hosts':
                # Preserve manual entries across discovery refreshes.
                known = {(e['host'],e['protocol'],e['path']) for e in self.hosts}
                self.hosts += [e for e in value if (e['host'],e['protocol'],e['path']) not in known]
            elif kind == 'add':
                self.hosts.append(value)
                self.current = value
                self.tab, self.index = 'hosts', len(self.hosts)-1
            elif kind == 'shares':
                self.shares, self.current = value
                self.tab, self.index = 'shares', 0
            elif kind == 'mounts':
                self.mounted = value
            elif kind == 'mounted_share':
                mounted, mounts = value
                self.mounted = mounts
                self.index = next((i for i, item in enumerate(mounts)
                                   if item['mountpoint'].rstrip('/') == mounted['mountpoint'].rstrip('/')), -1)
                if self.index == -1:
                    self.mounted.append(mounted)
                    self.index = len(self.mounted) - 1
                self.tab, self.focus, self.top = 'mounts', 'list', 0
            else:
                self.busy = False
                self.output = value + '\n\n' + self.overview()
                self.offset = 0
                self.message = ('Failed · ' + value.splitlines()[0][:180] if kind == 'error' else 'Finished · inspect results')
                if kind == 'error':
                    self.focus = 'output'
            updated = True
        if self.address and not self.busy:
            address, self.address = self.address, ''
            self.start('add',address)
        if updated:
            type(self)._cached = deepcopy({name: getattr(self, name) for name in
                                          ('hosts', 'shares', 'mounted', 'current', 'tab', 'index')})
        return updated

    def activate(self, action):
        if action == 'close':
            return super().activate(action)
        if self.busy:
            return None
        item = self.selected()
        if action == 'explain':
            import json
            return ('network_explain', 'Explain this network selection and connection results. Distinguish observed facts from suggestions.\n'
                    + json.dumps(item or self.current,ensure_ascii=False) + '\n' + self.output[-12000:])
        if action == 'add':
            self.form = dict(action='add',fields=[('Server URL','')],index=0,value='',entry=None,
                             summary='Add SMB, NFS or SFTP/SSH server · no passwords in URLs')
        elif action in ('refresh','mounts'):
            self.start(action)
        elif action == 'browse':
            if not item or 'mountpoint' not in item:
                self.message = 'Select a filesystem in Mounted first.'
            else:
                return ('network_browse', item['mountpoint'])
        elif action == 'open_share':
            if item and item.get('protocol') == 'smb' and item['path'] != '/':
                if Network().mac:
                    self.start('open_share', entry=item)
                    self.message = 'Mounting share · complete the macOS login dialog if shown · Ctrl+C cancels'
                else:
                    return self.activate('mount')
            else:
                self.message = 'Select a share first.'
        elif action == 'list':
            if item and 'protocol' in item:
                self.start('list',entry=item)
            else:
                self.message = 'Select a server or share first.'
        elif action in ('mount','connect','shell','unmount'):
            if not item or (action == 'unmount' and 'mountpoint' not in item) or (action != 'unmount' and 'protocol' not in item):
                self.message = 'Select a mount to unmount, or a server/share to connect.'
            elif action == 'shell' and item['protocol'] != 'sftp':
                self.message = 'SSH shell is available for SFTP/SSH servers.'
            else:
                fields = [('Local mount directory',''),('Username (optional)',item.get('user',''))] if action == 'mount' else []
                if action == 'mount' and item['protocol'] == 'smb':
                    fields.insert(0, ('Share name', item['path'].strip('/')))
                if action == 'connect' and item['protocol'] == 'smb':
                    fields = [('Username (optional)', item.get('user', ''))]
                self.form = dict(action=action,fields=fields,index=0,value='',entry=item,
                                  summary='Prepare ' + action + ' command for review and terminal authentication')
                if action == 'connect' and item['protocol'] == 'smb' and Network().mac:
                    self.form['summary'] = 'Open macOS SMB login and share selection · username optional · password stays in macOS'
        return None

    def event(self,event,value):
        if self.form and (event == 'key' and value == '\r' or event == 'click' and any(a == 'submit' and y == value[1] and l <= value[0] <= r for y,l,r,a,_ in self.zones)):
            form = self.form
            if form['action'] != 'add':
                try:
                    fields = form['fields']
                    values = dict(fields)
                    destination = values.get('Local mount directory', '')
                    if form['action'] == 'mount' and not destination.strip():
                        raise ValueError('Enter an existing empty local mount directory.')
                    username = values.get('Username (optional)', '')
                    item = dict(form['entry'])
                    if 'Share name' in values:
                        share = values['Share name'].strip().strip('/')
                        if not share or any(c in share for c in '/\\\n\r\0'):
                            raise ValueError('Enter the SMB share name, for example Public (not the server address).')
                        item['path'] = '/' + share
                    command = Network().command(form['action'],item,destination,username)
                    self.form = None
                    return ('network_command',command)
                except ValueError as exc:
                    self.message = str(exc)
                    form['summary'] = 'Cannot prepare command: ' + str(exc)
                    return None
        if event == 'key' and not self.form:
            if value == 'r':
                return self.activate('refresh')
            if value in 'sucfpPybtge':
                return None
        return super().event(event,value)
