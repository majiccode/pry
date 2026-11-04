"""AWS profiles, SSO handoff and URL-related resource discovery in a terminal panel."""
import queue
import threading
import webbrowser
from collections import Counter

from aws_workspace import (AwsCli, AwsDiscovery, AwsError, AwsStopped, aws_binary,
                           load_profiles, preferred_profile, region_console, sso_command)
from dashboard_panel import DashboardPanel
from git_panel import visible
from tui_theme import ACCENT, SECONDARY, SUCCESS, WARNING


class AwsPanel(DashboardPanel):
    tabs = [('resources', 'Resources'), ('profiles', 'Profiles'), ('regions', 'Regions')]
    list_title = 'Resources / profiles / regions'
    output_title = 'AWS details'
    footer = ('Enter inspect | Ctrl+Click open console | Tab actions | r refresh | '
              'Ctrl+F find | Esc details/close')
    hints = {
        'use': 'Use this AWS profile for authentication and URL lookup.',
        'auth': 'Check the selected profile with STS without changing anything.',
        'sso': 'Prepare an AWS SSO login command for the /run terminal handoff.',
        'lookup': 'Find resources related to a URL or hostname.',
        'regions': 'Set the regions to search: one region, a comma-separated list, or all.',
        'open': 'Open the selected AWS console page in your web browser.',
        'inspect': 'Show the selected resource details and console link.',
        'profiles': 'Return to AWS profile selection.',
    }

    def __init__(self, directory, url=''):
        super().__init__(directory)
        self.profiles = load_profiles()
        self.profile = preferred_profile(self.profiles)
        self.identity = None
        self.result = None
        self.resources = []
        self.available_regions = []
        self.regions = [self.profile.region] if self.profile else []
        self.last_url = ''
        self.pending_url = str(url).strip()
        self.tab = 'profiles'
        self.empty_message = 'No AWS profiles found in ~/.aws/config or ~/.aws/credentials.'
        self.output_visible = False
        self.output = self.overview()
        self._update_cards()
        if not self.profiles:
            self.message = 'Failed · no AWS profiles found. Configure AWS CLI, then reopen this panel.'
        elif self.profile is None:
            self.message = 'Select a profile, then authenticate or look up a URL.'
        else:
            self.message = f'Ready · using profile {self.profile.name} · Ctrl+Click a resource to open its console link'
        if self.pending_url:
            if self.profile is None:
                self.message = 'Select a profile first. The URL is prefilled for Look up URL.'
            else:
                self.start('lookup', self.pending_url)

    def heading(self):
        if not self.profile:
            return 'AWS', f'{len(self.profiles)} profile(s) found in the AWS CLI configuration'
        account = (self.identity or {}).get('Account') or self.profile.account_id or 'account not checked'
        if any(region.lower() in ('all', '*') for region in self.regions):
            scope = 'all regions'
        elif not self.regions:
            scope = self.profile.region
        elif len(self.regions) <= 3:
            scope = ', '.join(self.regions)
        else:
            scope = f'{len(self.regions)} regions'
        return f'AWS | {self.profile.name}', f'{account} · {scope} · read-only AWS CLI discovery'

    def overview(self):
        lines = [
            'AWS calls are read-only and use the AWS CLI with the selected profile.',
            'SSO login is prepared as a command for the /run terminal handoff; Pry never sees credentials.',
            'URL lookup correlates Route 53, CloudFront, EC2, RDS, ElastiCache, load balancers,',
            'Lambda, API Gateway, ECS, EKS, S3, ACM, Bedrock and tagged resources.',
        ]
        if not self.profiles:
            lines += ['', 'No profiles were found. Run "aws configure" or "aws configure sso" first.']
        if self.profile:
            lines += ['', f'Profile: {self.profile.name}',
                      f'Region from profile: {self.profile.region}',
                      f'SSO: {"yes" if self.profile.sso else "no"}']
            if self.identity:
                lines += [f'Account: {self.identity.get("Account", "")}',
                          f'Identity: {self.identity.get("Arn", "")}']
        if self.result:
            result = self.result
            counts = Counter(resource['service'] for resource in self.resources)
            lines += ['', f'URL: {visible(result["url"])}',
                      f'Hostname: {result["host"]}',
                      f'Searched regions: {", ".join(result["searched_regions"]) or "none"}',
                      f'Matching resources: {len(self.resources)}']
            if counts:
                lines.append('By service: ' + ', '.join(
                    f'{service} ({count})' for service, count in counts.most_common()))
            if result.get('diagnostics'):
                lines += ['', 'Some services were unavailable or denied:']
                lines += ['- ' + visible(item) for item in result['diagnostics'][:12]]
                if len(result['diagnostics']) > 12:
                    lines.append(f'... and {len(result["diagnostics"]) - 12} more diagnostics.')
            lines += ['', 'Enter inspects a row. Ctrl+Click a row opens its AWS console page.']
        else:
            lines += ['', 'Select a profile, authenticate if needed, then choose Look up URL.',
                      'Searched regions default to the profile region; use Regions to search more.']
        return '\n'.join(lines)

    def actions(self):
        common = [('Look up URL', 'lookup'), ('Regions', 'regions'), ('Refresh', 'refresh')]
        if self.tab == 'profiles':
            return ([('Use profile', 'use'), ('Authenticate', 'auth'), ('SSO login', 'sso')]
                    + common + [('Close', 'close')])
        return ([('Inspect', 'inspect'), ('Open console', 'open')] + common
                + [('Profiles', 'profiles'), ('Close', 'close')])

    def entry_action(self):
        if self.tab == 'profiles':
            return 'use'
        if self.tab == 'regions':
            return 'open'
        return 'inspect'

    def entries(self):
        if self.tab == 'profiles':
            self.empty_message = 'No AWS profiles found in ~/.aws/config or ~/.aws/credentials.'
            return [dict(label=(profile.label() +
                                (' · selected' if self.profile and profile.name == self.profile.name else '')),
                         profile=profile, color=ACCENT if self.profile and profile.name == self.profile.name else SECONDARY)
                    for profile in self.profiles]
        if self.tab == 'regions':
            self.empty_message = 'Select a profile to load AWS regions.'
            available = self.available_regions or self.regions
            counts = Counter(resource['region'] for resource in self.resources)
            entries = []
            for region in available:
                searched = (any(item.lower() in ('all', '*') for item in self.regions)
                            or region in self.regions)
                entries.append(dict(
                    label=(f'{region} · {"searched" if searched else "not searched"} · '
                           f'{counts.get(region, 0)} regional match(es)'),
                    region=region, color=ACCENT if searched else SECONDARY))
            return sorted(entries, key=lambda entry: (not entry['region'] in self.regions,
                                                      entry['region']))
        self.empty_message = ('No lookup yet. Choose Look up URL, or select a profile first.'
                              if not self.result else 'No AWS resources matched this URL.')
        return [dict(label=(f'{resource["service"]} · {resource["kind"]} · '
                            f'{resource["name"]} · {resource["region"]}'),
                     resource=resource, color=ACCENT)
                for resource in self.resources]

    def _update_cards(self):
        account = (self.identity or {}).get('Account') or (self.profile.account_id if self.profile else '')
        if any(region.lower() in ('all', '*') for region in self.regions):
            scope = 'all regions'
        elif not self.regions:
            scope = self.profile.region if self.profile else '-'
        elif len(self.regions) <= 2:
            scope = ', '.join(self.regions)
        else:
            scope = f'{len(self.regions)} regions'
        self.cards = [
            ('Profile', self.profile.name if self.profile else 'none', ACCENT),
            ('Account', account or 'not checked', SECONDARY),
            ('Search scope', scope, SECONDARY),
            ('Matches', str(len(self.resources)), SUCCESS if self.resources else WARNING),
        ]

    def _present(self):
        self._update_cards()
        self.output = self.overview()
        self.offset = 0

    def start(self, action, value='', entry=None):
        if self.busy:
            return
        if action == 'regions':
            text = value.strip()
            if not text:
                self.message = 'Enter one region, a comma-separated list, or all.'
                return
            if text.lower() in ('all', '*'):
                self.regions = ['all']
            else:
                regions = [item.strip() for item in text.split(',') if item.strip()]
                if not all(item.replace('-', '').isalnum() for item in regions):
                    self.message = 'Region names may contain letters, numbers and hyphens only.'
                    return
                self.regions = list(dict.fromkeys(regions))
            self.message = 'Search scope updated. Choose Look up URL, or Refresh to repeat the last lookup.'
            self._present()
            return
        if action not in ('auth', 'lookup'):
            return
        if self.profile is None:
            self.message = 'Select a profile first.'
            return
        if action == 'lookup':
            target = value.strip()
            if not target:
                self.message = 'Enter a URL or hostname.'
                return
            self.last_url = target
        regions = list(self.regions)
        profile = self.profile
        self.cancel.clear()
        self.busy = True
        self.message = ('Checking AWS identity · Ctrl+C cancels' if action == 'auth' else
                        f'Looking up {self.last_url} · Ctrl+C cancels')

        def worker():
            try:
                if action == 'auth':
                    cli = AwsCli(profile.name, profile.region, self.cancel)
                    identity = cli.identity()
                    regions = cli.available_regions()
                    self.results.put(('identity', identity))
                    self.results.put(('regions', regions))
                else:
                    discovery = AwsDiscovery(
                        profile, self.cancel,
                        progress=lambda text: self.results.put(('progress', text)))
                    self.results.put(('lookup', discovery.find(self.last_url, regions)))
            except AwsStopped:
                self.results.put(('cancelled', None))
            except (AwsError, ValueError) as exc:
                self.results.put(('error', visible(exc)))
            except Exception as exc:
                self.results.put(('error', visible(exc)))

        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()

    def poll(self):
        updated = False
        while True:
            try:
                kind, value = self.results.get_nowait()
            except queue.Empty:
                break
            updated = True
            if kind == 'progress':
                self.message = f'Working · {value} · Ctrl+C cancels'
            elif kind == 'identity':
                self.busy = False
                self.identity = value
                self.message = (f'Finished · authenticated as {value.get("Arn", value.get("Account", "AWS identity"))}'
                                if not self.cancel.is_set() else 'Cancelled')
                self._present()
            elif kind == 'regions':
                self.available_regions = value
                if not self.regions and self.profile:
                    self.regions = [self.profile.region]
                self._update_cards()
            elif kind == 'lookup':
                self.busy = False
                if self.cancel.is_set():
                    self.message = 'Cancelled · previous results retained'
                    continue
                self.result = value
                self.resources = value['resources']
                self.available_regions = value['available_regions']
                self.regions = value['searched_regions']
                self.tab, self.index, self.top = 'resources', 0, 0
                self.output_visible = True
                self._present()
                self.message = (f'Finished · {len(self.resources)} matching resource(s) · '
                                'Ctrl+Click a row to open its AWS console')
            elif kind == 'cancelled':
                self.busy = False
                self.message = 'Cancelled · previous results retained'
            elif kind == 'error':
                self.busy = False
                self.message = 'Failed · ' + str(value).splitlines()[0][:180]
                self.output = visible(value) + '\n\n' + self.overview()
                self.offset = 0
                self.output_visible = True
                self.focus = 'output'
                self._update_cards()
        return updated

    def activate(self, action):
        if action == 'close':
            if self.busy:
                self.message = 'Lookup running. Ctrl+C cancels; close after it finishes.'
                return None
            return ('close', '')
        if self.busy:
            return None
        if action == 'profiles':
            self.tab, self.index, self.top = 'profiles', 0, 0
            self._present()
            return None
        if action == 'use':
            profile = self.selected().get('profile') if self.selected() else None
            if profile is None:
                self.message = 'Select an AWS profile first.'
                return None
            self.profile = profile
            self.identity = None
            self.result = None
            self.resources = []
            self.available_regions = []
            self.regions = [profile.region]
            self.tab, self.index, self.top = 'profiles', 0, 0
            self._present()
            self.start('auth')
            return None
        if action == 'auth':
            self.start('auth')
        elif action == 'sso':
            if self.profile is None:
                self.message = 'Select an AWS profile first.'
            elif not aws_binary():
                self.message = 'Failed · AWS CLI not found on PATH.'
            else:
                self.message = 'SSO login command prepared. Review it and press Enter to authenticate.'
                return ('aws_command', sso_command(self.profile))
        elif action == 'lookup':
            if self.profile is None:
                self.message = 'Select a profile first.'
            else:
                default = self.pending_url or self.last_url
                self.pending_url = ''
                self.form = dict(action='lookup', fields=[('URL or hostname', default)], index=0,
                                 value='', entry=None,
                                 summary='Find AWS resources related to a URL; read-only AWS CLI calls.')
        elif action == 'regions':
            if self.profile is None:
                self.message = 'Select a profile first.'
            else:
                all_regions = (any(item.lower() in ('all', '*') for item in self.regions)
                               or (self.available_regions and
                                   len(self.regions) == len(self.available_regions)))
                current = 'all' if all_regions else \
                    ','.join(self.regions) if self.regions else self.profile.region
                self.form = dict(action='regions',
                                 fields=[('Regions (comma separated, or all)', current)], index=0,
                                 value='', entry=None,
                                 summary='Limit or expand the URL search scope.')
        elif action == 'refresh':
            if self.busy:
                return None
            if self.profile is None:
                self.message = 'Select a profile first.'
            elif self.last_url:
                self.start('lookup', self.last_url)
            else:
                self.start('auth')
        elif action == 'open':
            selected = self.selected()
            if not selected:
                self.message = 'Select a resource or region first.'
            else:
                url = ((selected.get('resource') or {}).get('url') if selected.get('resource')
                       else region_console(selected.get('region', '')) if selected.get('region') else '')
                self._open_url(url)
        elif action == 'inspect':
            selected = self.selected()
            resource = selected.get('resource') if selected else None
            if resource is None:
                self.message = 'Select a resource first.'
            else:
                self.output = self._resource_details(resource)
                self.offset = 0
                self.output_visible = True
                self.focus = 'output'
        return None

    def _resource_details(self, resource):
        lines = [f'{resource["service"]} · {resource["kind"]}',
                 f'Name: {visible(resource["name"])}',
                 f'Region: {resource["region"]}']
        if resource.get('relation'):
            lines.append(f'Relationship: {visible(resource["relation"])}')
        if resource.get('identifier'):
            lines.append(f'Identifier: {visible(resource["identifier"])}')
        if resource.get('arn'):
            lines.append(f'ARN: {visible(resource["arn"])}')
        if resource.get('url'):
            lines.append(f'Console: {visible(resource["url"])}')
        details = resource.get('details') or {}
        if details:
            lines.append('')
            for key, value in details.items():
                if isinstance(value, dict):
                    value = ', '.join(f'{name}={item}' for name, item in value.items())
                elif isinstance(value, (list, tuple, set)):
                    value = ', '.join(str(item) for item in value)
                lines.append(f'{key}: {visible(value)}')
        lines += ['', 'Ctrl+Click the resource row, or choose Open console, to open this link.']
        return '\n'.join(lines)

    def _open_url(self, url):
        if not url:
            self.message = 'No AWS console link is available for this selection.'
            return
        if not str(url).startswith(('https://', 'http://')):
            self.message = 'Refusing to open a non-HTTP console link.'
            return
        self.message = 'Opening the AWS console in your browser.'
        threading.Thread(target=webbrowser.open, args=(str(url),), daemon=True).start()

    def event(self, event, value):
        if self.form:
            return super().event(event, value)
        if event == 'link_click':
            x, y = value
            for row, left, right, action, argument in self.zones:
                if row == y and left <= x <= right and action == 'entry':
                    self.index = argument
                    self.focus = 'list'
                    if self.tab == 'profiles':
                        return self.activate('use')
                    if self.tab == 'regions':
                        return self.activate('open')
                    return self.activate('open')
            return None
        if event == 'key' and isinstance(value, str) and len(value) == 1:
            shortcuts = {'u': 'use', 'a': 'auth', 's': 'sso', 'l': 'lookup',
                         'g': 'regions', 'o': 'open', 'p': 'profiles', 'i': 'inspect'}
            if value in shortcuts:
                return self.activate(shortcuts[value])
        return super().event(event, value)
