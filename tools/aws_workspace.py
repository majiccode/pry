"""Read-only AWS CLI discovery for profiles, URL-related resources and console links."""
import configparser
import json
import os
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

FALLBACK_REGIONS = [
    'us-east-1', 'us-east-2', 'us-west-1', 'us-west-2', 'ca-central-1',
    'sa-east-1', 'eu-west-1', 'eu-west-2', 'eu-west-3', 'eu-central-1',
    'eu-central-2', 'eu-north-1', 'eu-south-1', 'eu-south-2', 'ap-south-1',
    'ap-south-2', 'ap-northeast-1', 'ap-northeast-2', 'ap-northeast-3',
    'ap-southeast-1', 'ap-southeast-2', 'ap-southeast-3', 'ap-southeast-4',
    'ap-east-1', 'me-south-1', 'me-central-1', 'af-south-1', 'il-central-1',
]
TLD_LABELS = {
    'com', 'net', 'org', 'edu', 'gov', 'mil', 'int', 'info', 'biz', 'app',
    'dev', 'cloud', 'ai', 'co', 'io', 'us', 'uk', 'de', 'fr', 'nl', 'be',
    'ch', 'at', 'au', 'ca', 'jp', 'cn', 'in', 'br', 'se', 'no', 'fi', 'dk',
    'es', 'it', 'pl', 'ru',
}
GENERIC_LABELS = {
    'www', 'api', 'app', 'apps', 'web', 'docs', 'login', 'auth', 'sso', 'id',
    'admin', 'portal', 'dashboard', 'console', 'dev', 'prod', 'production',
    'stage', 'staging', 'test', 'qa', 'uat', 'preprod', 'beta', 'demo', 'rnd',
    'internal', 'intranet', 'static', 'cdn', 'assets', 'media', 'mail', 'ftp',
    'vpn', 'oauth', 'service', 'services', 'v1', 'v2', 'v3',
}
SERVICE_NAMES = {
    'acm': 'ACM', 'apigateway': 'API Gateway', 'bedrock': 'Bedrock',
    'bedrock-agent': 'Bedrock Agents', 'cloudfront': 'CloudFront',
    'cloudwatch': 'CloudWatch', 'dynamodb': 'DynamoDB', 'ec2': 'EC2',
    'ecs': 'ECS', 'eks': 'EKS', 'elasticache': 'ElastiCache',
    'elasticloadbalancing': 'Elastic Load Balancing', 'events': 'EventBridge',
    'kms': 'KMS', 'lambda': 'Lambda', 'logs': 'CloudWatch Logs', 'rds': 'RDS',
    'route53': 'Route 53', 's3': 'S3', 'secretsmanager': 'Secrets Manager',
    'sns': 'SNS', 'sqs': 'SQS', 'states': 'Step Functions',
}


class AwsError(RuntimeError):
    """An AWS CLI call failed, timed out or was cancelled."""


class AwsStopped(AwsError):
    """The operator cancelled the current panel operation."""


@dataclass(frozen=True)
class AwsProfile:
    name: str
    region: str
    sso: bool = False
    sso_session: str = ''
    account_id: str = ''
    role: str = ''
    role_arn: str = ''
    source_profile: str = ''

    def label(self):
        parts = [self.name, self.region or 'no region']
        if self.sso:
            parts.append('SSO')
        if self.account_id:
            parts.append(self.account_id)
        return ' · '.join(parts)


def aws_binary():
    return shutil.which('aws')


def _config_files():
    return [
        ('config', Path(os.environ.get('AWS_CONFIG_FILE', '~/.aws/config')).expanduser()),
        ('credentials', Path(os.environ.get('AWS_CREDENTIALS_FILE', '~/.aws/credentials')).expanduser()),
    ]


def load_profiles():
    """Read profiles without exposing credentials; environment overrides are respected."""
    records = {}
    for kind, path in _config_files():
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(path, encoding='utf-8')
        except (OSError, configparser.Error):
            continue
        for section in parser.sections():
            if kind == 'config' and section.startswith('sso-session'):
                continue
            if kind == 'config' and section.startswith('profile '):
                name = section[len('profile '):].strip()
            else:
                name = section.strip()
            if not name:
                continue
            record = records.setdefault(name, {})
            for key in ('region', 'sso_session', 'sso_start_url', 'sso_account_id',
                        'sso_role_name', 'role_arn', 'role_name', 'source_profile'):
                if parser.has_option(section, key):
                    record[key] = parser.get(section, key).strip()
    environment_profile = os.environ.get('AWS_PROFILE', '').strip()
    if environment_profile and environment_profile not in records:
        records[environment_profile] = {}
    default_region = (os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION')
                      or 'us-east-1')
    profiles = []
    for name, record in records.items():
        profiles.append(AwsProfile(
            name=name,
            region=record.get('region') or default_region,
            sso=bool(record.get('sso_session') or record.get('sso_start_url')
                     or record.get('sso_account_id')),
            sso_session=record.get('sso_session', ''),
            account_id=record.get('sso_account_id', ''),
            role=record.get('sso_role_name') or record.get('role_name', ''),
            role_arn=record.get('role_arn', ''),
            source_profile=record.get('source_profile', ''),
        ))
    profiles.sort(key=lambda profile: (profile.name != 'default', profile.name.casefold()))
    return profiles


def preferred_profile(profiles):
    wanted = os.environ.get('AWS_PROFILE', '').strip()
    for profile in profiles:
        if wanted and profile.name == wanted:
            return profile
    for profile in profiles:
        if profile.name == 'default':
            return profile
    return profiles[0] if len(profiles) == 1 else None


def q(value):
    return quote(str(value), safe='')


def region_console(region):
    return f'https://{region}.console.aws.amazon.com/console/home?region={q(region)}'


def ec2_instance_url(region, instance_id):
    return (f'https://{region}.console.aws.amazon.com/ec2/home?region={q(region)}'
            f'#InstanceDetails:instanceId={q(instance_id)}')


def s3_url(bucket, region='us-east-1'):
    return (f'https://s3.console.aws.amazon.com/s3/buckets/{q(bucket)}'
            f'?region={q(region)}&tab=objects')


def rds_instance_url(region, identifier):
    return (f'https://{region}.console.aws.amazon.com/rds/home?region={q(region)}'
            f'#database:id={q(identifier)};is-cluster=false')


def rds_cluster_url(region, identifier):
    return (f'https://{region}.console.aws.amazon.com/rds/home?region={q(region)}'
            f'#database:id={q(identifier)};is-cluster=true')


def elasticache_url(region, identifier, engine='redis'):
    section = 'memcached' if str(engine).lower().startswith('memcached') else 'redis'
    return (f'https://{region}.console.aws.amazon.com/elasticache/home?region={q(region)}'
            f'#/{section}/{q(identifier)}')


def load_balancer_url(region, arn):
    return (f'https://{region}.console.aws.amazon.com/ec2/home?region={q(region)}'
            f'#LoadBalancerDetails:loadBalancerArn={q(arn)}')


def lambda_url(region, name):
    return (f'https://{region}.console.aws.amazon.com/lambda/home?region={q(region)}'
            f'#/functions/{q(name)}')


def api_gateway_url(region, api_id):
    return (f'https://{region}.console.aws.amazon.com/apigateway/home?region={q(region)}'
            f'#/apis/{q(api_id)}')


def api_gateway_domain_url(region, domain):
    return (f'https://{region}.console.aws.amazon.com/apigateway/home?region={q(region)}'
            f'#/custom-domain-names/{q(domain)}')


def ecs_cluster_url(region, name):
    return (f'https://{region}.console.aws.amazon.com/ecs/home?region={q(region)}'
            f'#/clusters/{q(name)}')


def eks_cluster_url(region, name):
    return (f'https://{region}.console.aws.amazon.com/eks/home?region={q(region)}'
            f'#/clusters/{q(name)}')


def route53_url(zone_id):
    return f'https://console.aws.amazon.com/route53/v2/hostedzones#ListRecordSets/{q(zone_id)}'


def cloudfront_url(distribution_id):
    return f'https://console.aws.amazon.com/cloudfront/v4/home#/distributions/{q(distribution_id)}'


def acm_url(region, arn):
    return (f'https://{region}.console.aws.amazon.com/acm/home?region={q(region)}'
            f'#/certificates/{q(arn)}')


def bedrock_custom_model_url(region, arn):
    return (f'https://{region}.console.aws.amazon.com/bedrock/home?region={q(region)}'
            f'#/custom-models/{q(arn)}')


def bedrock_agent_url(region, agent_id):
    return (f'https://{region}.console.aws.amazon.com/bedrock/home?region={q(region)}'
            f'#/agents/{q(agent_id)}')


def bedrock_knowledge_base_url(region, knowledge_base_id):
    return (f'https://{region}.console.aws.amazon.com/bedrock/home?region={q(region)}'
            f'#/knowledge-bases/{q(knowledge_base_id)}')


def bedrock_provisioned_url(region):
    return (f'https://{region}.console.aws.amazon.com/bedrock/home?region={q(region)}'
            '#/provisioned-model-throughput')


def service_name(service):
    return SERVICE_NAMES.get(service, service.replace('-', ' ').replace('_', ' ').title() or 'AWS')


def arn_console_url(arn, fallback_region='us-east-1'):
    """Best-effort console link for a resource ARN, without calling AWS."""
    parts = str(arn).split(':', 5)
    if len(parts) < 6 or parts[0] != 'arn':
        return region_console(fallback_region)
    service, region, resource = parts[2], parts[3] or fallback_region, parts[5]
    name = resource.rsplit('/', 1)[-1].rsplit(':', 1)[-1]
    if service == 's3':
        return s3_url(resource, region)
    if service == 'ec2':
        kind, _, identifier = resource.partition('/')
        if kind == 'instance':
            return ec2_instance_url(region, identifier)
        if kind == 'volume':
            return (f'https://{region}.console.aws.amazon.com/ec2/home?region={q(region)}'
                    f'#Volumes:volumeId={q(identifier)}')
        if kind == 'security-group':
            return (f'https://{region}.console.aws.amazon.com/ec2/home?region={q(region)}'
                    f'#SecurityGroup:groupId={q(identifier)}')
        if kind == 'vpc':
            return (f'https://{region}.console.aws.amazon.com/ec2/home?region={q(region)}'
                    f'#VpcDetails:VpcId={q(identifier)}')
        return f'https://{region}.console.aws.amazon.com/ec2/home?region={q(region)}'
    if service == 'rds':
        if resource.startswith('cluster:'):
            return rds_cluster_url(region, name)
        return rds_instance_url(region, name)
    if service == 'elasticloadbalancing':
        return load_balancer_url(region, arn)
    if service == 'lambda':
        return lambda_url(region, name)
    if service == 'apigateway':
        return api_gateway_url(region, name)
    if service == 'ecs':
        if resource.startswith('cluster/'):
            return ecs_cluster_url(region, name)
        return f'https://{region}.console.aws.amazon.com/ecs/home?region={q(region)}'
    if service == 'eks':
        return eks_cluster_url(region, name)
    if service == 'route53':
        return route53_url(name)
    if service == 'cloudfront':
        return cloudfront_url(name)
    if service == 'acm':
        return acm_url(region, arn)
    if service == 'bedrock':
        if 'custom-model' in resource:
            return bedrock_custom_model_url(region, arn)
        if 'provisioned-model' in resource:
            return bedrock_provisioned_url(region)
        return f'https://{region}.console.aws.amazon.com/bedrock/home?region={q(region)}'
    if service == 'bedrock-agent':
        if 'agent' in resource:
            return bedrock_agent_url(region, name)
        if 'knowledge-base' in resource:
            return bedrock_knowledge_base_url(region, name)
        return f'https://{region}.console.aws.amazon.com/bedrock/home?region={q(region)}'
    if service == 'dynamodb':
        return (f'https://{region}.console.aws.amazon.com/dynamodbv2/home?region={q(region)}'
                f'#table?name={q(name)}')
    if service == 'sqs':
        return (f'https://{region}.console.aws.amazon.com/sqs/v2/home?region={q(region)}'
                f'#/queues/{q(arn)}')
    if service == 'sns':
        return (f'https://{region}.console.aws.amazon.com/sns/v3/home?region={q(region)}'
                f'#/topic/{q(arn)}')
    if service == 'states':
        return (f'https://{region}.console.aws.amazon.com/states/home?region={q(region)}'
                f'#/statemachines/view/{q(arn)}')
    if service == 'logs':
        return (f'https://{region}.console.aws.amazon.com/cloudwatch/home?region={q(region)}'
                f'#logsV2:log-groups/log-group/{q(name)}')
    if service == 'secretsmanager':
        return (f'https://{region}.console.aws.amazon.com/secretsmanager/home?region={q(region)}'
                f'#/secret?name={q(name)}')
    if service == 'kms':
        return (f'https://{region}.console.aws.amazon.com/kms/home?region={q(region)}'
                f'#/kms/keys/{q(name)}/')
    return f'https://{region}.console.aws.amazon.com/{q(service)}/home?region={q(region)}'


def target_terms(url):
    """Turn a URL or hostname into distinctive and supporting match terms."""
    text = str(url).strip()
    if not text:
        raise ValueError('Enter a URL or hostname to look up.')
    if '://' not in text:
        text = 'https://' + text
    parsed = urlparse(text)
    host = (parsed.hostname or '').lower().strip('.')
    if not host or '.' not in host:
        raise ValueError('Enter a full hostname, for example https://docs.example.com/login.')
    labels = [label for label in host.split('.') if label]
    strong, weak = set(), set()
    for index, label in enumerate(labels):
        if index == len(labels) - 1 and label in TLD_LABELS:
            continue
        parts = [part for part in re.split(r'[-_.]', label) if part]
        for part in parts:
            if len(part) < 3:
                continue
            (weak if part in GENERIC_LABELS else strong).add(part)
        if len(label) >= 4 and label not in GENERIC_LABELS:
            strong.add(label)
    domain = '.'.join(labels[-2:]) if len(labels) >= 2 else host
    if not strong:
        strong.add(labels[-2] if len(labels) >= 2 else host)
    return dict(host=host, domain=domain, labels=labels,
                strong=sorted(strong), weak=sorted(weak))


def match_score(terms, text):
    text = str(text).lower()
    score = 0
    if terms['host'] and terms['host'] in text:
        score += 120
    if terms['domain'] and terms['domain'] in text:
        score += 25
    strong_hits = 0
    for token in terms['strong']:
        if token and token in text:
            strong_hits += 1
            score += 14
    for token in terms['weak']:
        if token and token in text:
            score += 3
    if not strong_hits and terms['host'] not in text and terms['domain'] not in text:
        return 0
    return score


def _text(*values):
    parts = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, dict):
            parts.append(json.dumps(value, sort_keys=True, default=str))
        elif isinstance(value, (list, tuple, set)):
            parts.append(' '.join(str(item) for item in value))
        else:
            parts.append(str(value))
    return ' '.join(parts).lower()


def _first_line(text, limit=360):
    line = str(text).strip().splitlines()[0] if str(text).strip() else 'No diagnostic output.'
    return line[:limit]


class AwsCli:
    """Run bounded AWS CLI calls with cancellation and no credential handling."""

    def __init__(self, profile, region='', cancel=None, timeout=30, max_output=16 * 1024 * 1024):
        self.profile = profile
        self.region = region or 'us-east-1'
        self.cancel = cancel
        self.timeout = timeout
        self.max_output = max_output
        self.binary = aws_binary()

    def _argv(self, args, region, json_output):
        argv = [self.binary, '--profile', self.profile, '--region', region or self.region]
        if json_output:
            argv += ['--output', 'json']
        return argv + list(args)

    def run(self, args, *, region=None, json_output=True, timeout=None):
        if not self.binary:
            raise AwsError('AWS CLI not found. Install AWS CLI v2 and ensure "aws" is on PATH.')
        timeout = self.timeout if timeout is None else timeout
        env = dict(os.environ)
        env.update(AWS_PAGER='', AWS_CLI_AUTO_PROMPT='off')
        process = subprocess.Popen(self._argv(args, region, json_output), env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        stdout, stderr = bytearray(), bytearray()
        reason = ''
        exit_seen = None
        try:
            with selectors.DefaultSelector() as selector:
                for stream, name in ((process.stdout, 'stdout'), (process.stderr, 'stderr')):
                    if stream is not None:
                        selector.register(stream, selectors.EVENT_READ, name)
                deadline = time.monotonic() + max(1, timeout)
                while selector.get_map() or process.poll() is None:
                    if self.cancel is not None and self.cancel.is_set():
                        reason = 'Cancelled'
                        break
                    if time.monotonic() >= deadline:
                        reason = f'Timed out after {timeout:g}s'
                        break
                    events = selector.select(.05)
                    for key, _ in events:
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        target = stdout if key.data == 'stdout' else stderr
                        target.extend(chunk)
                        if len(target) > self.max_output:
                            reason = 'AWS CLI output limit exceeded'
                            break
                    if reason:
                        break
                    if process.poll() is not None:
                        exit_seen = exit_seen or time.monotonic()
                        if time.monotonic() - exit_seen > .2 and not events:
                            break
                    else:
                        exit_seen = None
            if reason:
                self._signal(process)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._signal(process, signal.SIGKILL)
                process.wait(timeout=3)
        finally:
            if process.poll() is None:
                self._signal(process, signal.SIGKILL)
                process.wait(timeout=3)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        out = stdout.decode('utf-8', 'replace').strip()
        err = stderr.decode('utf-8', 'replace').strip()
        if reason:
            raise AwsStopped(reason) if reason == 'Cancelled' else AwsError(reason)
        if process.returncode:
            detail = err or out or f'AWS CLI exited with status {process.returncode}.'
            raise AwsError(_first_line(detail))
        if not json_output:
            return out
        if not out:
            return {}
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise AwsError('AWS CLI returned invalid JSON: ' + _first_line(out)) from exc

    @staticmethod
    def _signal(process, sig=signal.SIGTERM):
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                process.send_signal(sig)
            except ProcessLookupError:
                pass

    def identity(self):
        return self.run(['sts', 'get-caller-identity'])

    def available_regions(self):
        try:
            data = self.run(['ec2', 'describe-regions', '--all-regions',
                             '--query', 'Regions[].RegionName'])
        except AwsStopped:
            raise
        except AwsError:
            return list(FALLBACK_REGIONS)
        if isinstance(data, list):
            regions = [str(region) for region in data if region]
            if regions:
                return sorted(dict.fromkeys(regions))
        return list(FALLBACK_REGIONS)


class AwsDiscovery:
    """Correlate a hostname with AWS resources and AWS console destinations."""

    def __init__(self, profile, cancel=None, progress=None):
        self.profile = profile
        self.cancel = cancel
        self.progress = progress
        self.terms = None
        self.account = ''
        self.resources = []
        self.seen = {}
        self.diagnostics = []
        self.s3_hints = set()
        self.cli = AwsCli(profile.name, profile.region, cancel)

    def _progress(self, text):
        if self.progress is not None and not (self.cancel is not None and self.cancel.is_set()):
            self.progress(text)

    def _diagnose(self, label, exc):
        entry = f'{label}: {_first_line(exc)}'
        if entry not in self.diagnostics and len(self.diagnostics) < 40:
            self.diagnostics.append(entry)

    def _call(self, cli, label, args):
        self._progress(label)
        try:
            return cli.run(args)
        except AwsStopped:
            raise
        except AwsError as exc:
            self._diagnose(label, exc)
            return None

    def _add(self, *, service, kind, name, region, identifier='', arn='', url='',
             details=None, relation='', score=0, keys=(), text='', force=False):
        if not score and text:
            score = match_score(self.terms, text)
        if score <= 0 and not force:
            return None
        canonical = arn or f'{service}|{region}|{kind}|{identifier or name}'
        lookup = [canonical]
        for key in keys:
            if not key:
                continue
            normalized = key.lower().rstrip('.') if str(key).startswith('dns:') else key
            if ':' in str(normalized) and not str(normalized).split(':', 1)[1]:
                continue
            lookup.append(normalized)
        existing = next((self.seen[key] for key in lookup if key in self.seen), None)
        details = dict(details or {})
        if existing is not None:
            if score > existing['score']:
                existing['score'] = score
            if existing['kind'] in ('Alias target', 'Origin') and kind not in ('Alias target', 'Origin'):
                existing.update(service=service, kind=kind, region=region)
                if name:
                    existing['name'] = name
                if identifier:
                    existing['identifier'] = identifier
                if url:
                    existing['url'] = url
                existing['details'].update(details)
            else:
                for key, value in details.items():
                    existing['details'].setdefault(key, value)
            if arn and not existing['arn']:
                existing['arn'] = arn
            if url and not existing['url']:
                existing['url'] = url
            if relation and relation not in existing['relation']:
                existing['relation'] = '; '.join(filter(None, (existing['relation'], relation)))
            for key in lookup:
                self.seen[key] = existing
            return existing
        resource = dict(service=service, kind=kind, name=name or identifier or '(unnamed)',
                        region=region, identifier=identifier, arn=arn, url=url,
                        details=details, relation=relation, score=score)
        self.resources.append(resource)
        for key in lookup:
            self.seen[key] = resource
        return resource

    def _route53(self):
        zone_data = self._call(self.cli, 'Searching Route 53',
                               ['route53', 'list-hosted-zones', '--max-items', '500'])
        if not zone_data:
            return
        if zone_data.get('NextToken'):
            self._diagnose('Route 53', 'Hosted zone listing was truncated; only the first 500 were searched.')
        host = self.terms['host']
        zones = []
        for zone in zone_data.get('HostedZones', []):
            zone_name = str(zone.get('Name', '')).rstrip('.').lower()
            if zone_name and (host == zone_name or host.endswith('.' + zone_name)):
                zones.append(zone)
        for zone in zones:
            zone_id = str(zone.get('Id', '')).rsplit('/', 1)[-1]
            if not zone_id:
                continue
            parent = '.'.join(host.split('.')[1:]) + '.'
            names = [host + '.']
            if parent:
                names.append('*.' + parent)
            records = []
            for start in dict.fromkeys(names):
                data = self._call(self.cli, f'Searching Route 53 records in {zone.get("Name", zone_id)}',
                                  ['route53', 'list-resource-record-sets', '--hosted-zone-id', zone_id,
                                   '--start-record-name', start, '--max-items', '100'])
                if data:
                    records.extend(data.get('ResourceRecordSets', []))
            seen_records = set()
            for record in records:
                name = str(record.get('Name', '')).rstrip('.')
                lowered = name.lower()
                wildcard = lowered.startswith('*.') and host.endswith(lowered[1:])
                if lowered != host and not wildcard:
                    continue
                record_key = (lowered, record.get('Type'))
                if record_key in seen_records:
                    continue
                seen_records.add(record_key)
                values = [item.get('Value', '') for item in record.get('ResourceRecords', [])]
                alias = record.get('AliasTarget') or {}
                dns_name = str(alias.get('DNSName', '')).rstrip('.')
                details = {'Hosted zone': str(zone.get('Name', '')).rstrip('.'),
                           'Type': record.get('Type', ''),
                           'Values': ', '.join(values) or dns_name or 'Alias record'}
                if alias:
                    details['Alias target'] = dns_name
                    details['Alias zone'] = alias.get('HostedZoneId', '')
                self._add(service='Route 53', kind='DNS record', name=name,
                          region='global', identifier=str(record.get('Type', '')),
                          url=route53_url(zone_id), details=details,
                          relation='DNS record for the requested hostname', score=220,
                          keys=(f'route53:{zone_id}:{lowered}:{record.get("Type")}',))
                if dns_name:
                    self._add_alias_target(dns_name, zone_id, name)
                if str(record.get('Type', '')).upper() in ('CNAME', 'A', 'AAAA'):
                    for value in values:
                        value = str(value).strip().rstrip('.')
                        if not value:
                            continue
                        if re.fullmatch(r'\d{1,3}(?:\.\d{1,3}){3}', value):
                            self._add(service='AWS', kind='Alias target', name=value,
                                      region=self.profile.region, identifier=value,
                                      details={'IP address': value, 'DNS record': name},
                                      relation='DNS record target', score=75,
                                      keys=(f'ip:{value}',), force=True)
                        elif '.' in value:
                            self._add_alias_target(value, zone_id, name)

    def _add_alias_target(self, dns_name, zone_id, record_name):
        lowered = dns_name.lower()
        if lowered.endswith('.elb.amazonaws.com') or '.elb.' in lowered:
            service, kind = 'Elastic Load Balancing', 'Alias target'
        elif lowered.endswith('cloudfront.net'):
            service, kind = 'CloudFront', 'Alias target'
        elif 's3-website' in lowered or lowered.endswith('s3.amazonaws.com'):
            service, kind = 'S3', 'Alias target'
        elif 'execute-api' in lowered:
            service, kind = 'API Gateway', 'Alias target'
        elif lowered.endswith('elasticbeanstalk.com'):
            service, kind = 'Elastic Beanstalk', 'Alias target'
        else:
            service, kind = 'AWS', 'Alias target'
        region = self.profile.region
        extra_keys = [f'dns:{lowered}']
        if service == 'S3':
            bucket = lowered.split('.s3', 1)[0]
            extra_keys.append(f's3:{bucket}')
            self.s3_hints.add(bucket)
        self._add(service=service, kind=kind, name=dns_name, region=region,
                  identifier=dns_name, url=self._dns_service_url(service, lowered),
                  details={'DNS name': dns_name, 'Alias for': record_name,
                           'Route 53 hosted zone': zone_id},
                  relation='Route 53 alias target', score=80,
                  keys=tuple(extra_keys), force=True)

    def _dns_service_url(self, service, dns_name):
        if service == 'CloudFront':
            return 'https://console.aws.amazon.com/cloudfront/v4/home#/distributions'
        if service == 'S3':
            bucket = dns_name.split('.s3', 1)[0]
            return s3_url(bucket, self.profile.region)
        if service == 'API Gateway':
            return (f'https://{self.profile.region}.console.aws.amazon.com/apigateway/home'
                    f'?region={q(self.profile.region)}#/custom-domain-names')
        if service == 'Elastic Load Balancing':
            return (f'https://{self.profile.region}.console.aws.amazon.com/ec2/home'
                    f'?region={q(self.profile.region)}#LoadBalancers')
        return region_console(self.profile.region)

    def _cloudfront(self):
        data = self._call(self.cli, 'Searching CloudFront',
                          ['cloudfront', 'list-distributions', '--max-items', '200'])
        if not data:
            return
        listing = data.get('DistributionList', {})
        if listing.get('IsTruncated'):
            self._diagnose('CloudFront', 'Distribution listing was truncated.')
        for distribution in listing.get('Items', []):
            aliases = distribution.get('Aliases', {}).get('Items', [])
            origins = distribution.get('Origins', {}).get('Items', [])
            origin_names = [origin.get('DomainName', '') for origin in origins]
            origin_ids = [origin.get('Id', '') for origin in origins]
            text = _text(distribution.get('Id'), distribution.get('ARN'),
                         distribution.get('DomainName'), distribution.get('Comment'),
                         aliases, origin_names, origin_ids)
            alias_match = self.terms['host'] in [str(alias).lower() for alias in aliases]
            score = 240 if alias_match else match_score(self.terms, text)
            resource = self._add(
                service='CloudFront', kind='Distribution',
                name=distribution.get('Comment') or distribution.get('DomainName') or distribution.get('Id'),
                region='global', identifier=distribution.get('Id', ''),
                arn=distribution.get('ARN', ''), url=cloudfront_url(distribution.get('Id', '')),
                details={'Domain': distribution.get('DomainName', ''),
                         'Aliases': ', '.join(aliases) or 'None',
                         'Status': distribution.get('Status', ''),
                         'Enabled': distribution.get('Enabled', ''),
                         'Origins': ', '.join(filter(None, origin_ids))},
                relation='CloudFront distribution', score=score,
                keys=(f'dns:{str(distribution.get("DomainName", "")).lower()}',), text=text)
            if not resource:
                continue
            for origin in origins:
                domain = str(origin.get('DomainName', '')).rstrip('.')
                if not domain:
                    continue
                bucket = domain.split('.s3', 1)[0] if '.s3' in domain else ''
                if bucket:
                    self.s3_hints.add(bucket)
                    self._add(service='S3', kind='Origin', name=bucket,
                              region=self.profile.region, identifier=bucket,
                              arn=f'arn:aws:s3:::{bucket}', url=s3_url(bucket, self.profile.region),
                              details={'CloudFront origin': origin.get('Id', ''),
                                       'Domain': domain},
                              relation='CloudFront origin', score=resource['score'],
                              keys=(f'dns:{domain.lower()}', f's3:{bucket}'), force=True)
                else:
                    self._add(service='AWS', kind='Origin', name=domain,
                              region=self.profile.region, identifier=domain,
                              url='', details={'CloudFront origin': origin.get('Id', '')},
                              relation='CloudFront origin', score=resource['score'],
                              keys=(f'dns:{domain.lower()}',), force=True)

    def _s3(self, resource_groups_buckets=None):
        data = self._call(self.cli, 'Searching S3', ['s3api', 'list-buckets'])
        if not data:
            return
        hints = set(resource_groups_buckets or set()) | self.s3_hints
        for bucket in data.get('Buckets', []):
            name = str(bucket.get('Name', ''))
            if not name:
                continue
            if match_score(self.terms, name) <= 0 and name not in hints:
                continue
            location = self._call(self.cli, f'Locating S3 bucket {name}',
                                  ['s3api', 'get-bucket-location', '--bucket', name])
            region = 'us-east-1'
            if location:
                constraint = location.get('LocationConstraint')
                if constraint and constraint not in ('US', 'us-east-1'):
                    region = 'eu-west-1' if constraint == 'EU' else constraint
            self._add(service='S3', kind='Bucket', name=name, region=region,
                      identifier=name, arn=f'arn:aws:s3:::{name}', url=s3_url(name, region),
                      details={'Creation date': bucket.get('CreationDate', ''),
                               'Location': region},
                      relation='S3 bucket', score=match_score(self.terms, name),
                      keys=(f's3:{name}',), text=name)

    def _acm(self, cli, region):
        data = self._call(cli, f'Searching ACM in {region}',
                          ['acm', 'list-certificates', '--max-items', '500'])
        if not data:
            return
        for certificate in data.get('CertificateSummaryList', []):
            domain = str(certificate.get('DomainName', ''))
            text = _text(domain, certificate.get('CertificateArn'), certificate.get('Type'))
            if match_score(self.terms, text) <= 0:
                continue
            self._add(service='ACM', kind='Certificate', name=domain, region=region,
                      identifier=certificate.get('CertificateArn', ''),
                      arn=certificate.get('CertificateArn', ''),
                      url=acm_url(region, certificate.get('CertificateArn', '')),
                      details={'Status': certificate.get('Status', ''),
                               'Type': certificate.get('Type', ''),
                               'Not after': certificate.get('NotAfter', '')},
                      relation='TLS certificate', score=match_score(self.terms, text), text=text)

    def _ec2(self, cli, region):
        data = self._call(cli, f'Searching EC2 in {region}',
                          ['ec2', 'describe-instances', '--max-items', '2000'])
        if not data:
            return
        for reservation in data.get('Reservations', []):
            for instance in reservation.get('Instances', []):
                instance_id = instance.get('InstanceId', '')
                tags = {tag.get('Key', ''): tag.get('Value', '')
                        for tag in instance.get('Tags', []) if tag.get('Key')}
                name = tags.get('Name') or instance_id
                text = _text(instance_id, name, tags, instance.get('PublicDnsName'),
                             instance.get('PrivateDnsName'), instance.get('PublicIpAddress'),
                             instance.get('PrivateIpAddress'), instance.get('InstanceType'),
                             instance.get('State', {}).get('Name'),
                             [group.get('GroupName') for group in instance.get('SecurityGroups', [])])
                if match_score(self.terms, text) <= 0:
                    continue
                arn = f'arn:aws:ec2:{region}:{self.account}:instance/{instance_id}'
                self._add(service='EC2', kind='Instance', name=name, region=region,
                          identifier=instance_id, arn=arn,
                          url=ec2_instance_url(region, instance_id),
                          details={'State': instance.get('State', {}).get('Name', ''),
                                   'Type': instance.get('InstanceType', ''),
                                   'Private IP': instance.get('PrivateIpAddress', ''),
                                   'Public IP': instance.get('PublicIpAddress', ''),
                                   'Private DNS': instance.get('PrivateDnsName', ''),
                                   'Public DNS': instance.get('PublicDnsName', ''),
                                   'Availability zone': instance.get('Placement', {}).get('AvailabilityZone', ''),
                                   'VPC': instance.get('VpcId', ''),
                                   'Subnet': instance.get('SubnetId', ''),
                                   'Launch time': instance.get('LaunchTime', ''),
                                   'Tags': tags},
                          relation='EC2 instance', score=match_score(self.terms, text),
                          keys=(f'dns:{str(instance.get("PublicDnsName", "")).lower().rstrip(".")}',
                                f'dns:{str(instance.get("PrivateDnsName", "")).lower().rstrip(".")}',
                                f'ip:{instance.get("PublicIpAddress", "")}',
                                f'ip:{instance.get("PrivateIpAddress", "")}'),
                          text=text)

    def _rds(self, cli, region):
        data = self._call(cli, f'Searching RDS instances in {region}',
                          ['rds', 'describe-db-instances', '--max-items', '1000'])
        if data:
            for instance in data.get('DBInstances', []):
                identifier = instance.get('DBInstanceIdentifier', '')
                tags = {tag.get('Key', ''): tag.get('Value', '')
                        for tag in instance.get('TagList', []) if tag.get('Key')}
                endpoint = instance.get('Endpoint', {}) or {}
                text = _text(identifier, instance.get('DbiResourceId'), instance.get('Engine'),
                             instance.get('DBName'), endpoint.get('Address'), tags,
                             instance.get('DBClusterIdentifier'))
                if match_score(self.terms, text) <= 0:
                    continue
                self._add(service='RDS', kind='DB instance', name=identifier, region=region,
                          identifier=identifier, arn=instance.get('DBInstanceArn', ''),
                          url=rds_instance_url(region, identifier),
                          details={'Engine': instance.get('Engine', ''),
                                   'Engine version': instance.get('EngineVersion', ''),
                                   'Class': instance.get('DBInstanceClass', ''),
                                   'Status': instance.get('DBInstanceStatus', ''),
                                   'Endpoint': endpoint.get('Address', ''),
                                   'Port': endpoint.get('Port', ''),
                                   'Database': instance.get('DBName', ''),
                                   'Cluster': instance.get('DBClusterIdentifier', ''),
                                   'Multi-AZ': instance.get('MultiAZ', ''),
                                   'Tags': tags},
                          relation='RDS instance', score=match_score(self.terms, text),
                          keys=(f'dns:{str(endpoint.get("Address", "")).lower()}',
                                f'rds:{identifier}'), text=text)
        data = self._call(cli, f'Searching RDS clusters in {region}',
                          ['rds', 'describe-db-clusters', '--max-items', '500'])
        if data:
            for cluster in data.get('DBClusters', []):
                identifier = cluster.get('DBClusterIdentifier', '')
                tags = {tag.get('Key', ''): tag.get('Value', '')
                        for tag in cluster.get('TagList', []) if tag.get('Key')}
                endpoint = cluster.get('Endpoint', '')
                reader = cluster.get('ReaderEndpoint', '')
                text = _text(identifier, cluster.get('DbClusterResourceId'), cluster.get('Engine'),
                             cluster.get('DatabaseName'), endpoint, reader, tags)
                if match_score(self.terms, text) <= 0:
                    continue
                self._add(service='RDS', kind='DB cluster', name=identifier, region=region,
                          identifier=identifier, arn=cluster.get('DBClusterArn', ''),
                          url=rds_cluster_url(region, identifier),
                          details={'Engine': cluster.get('Engine', ''),
                                   'Engine version': cluster.get('EngineVersion', ''),
                                   'Status': cluster.get('Status', ''),
                                   'Writer endpoint': endpoint,
                                   'Reader endpoint': reader,
                                   'Database': cluster.get('DatabaseName', ''),
                                   'Members': len(cluster.get('DBClusterMembers', [])),
                                   'Tags': tags},
                          relation='RDS cluster', score=match_score(self.terms, text),
                          keys=(f'dns:{str(endpoint).lower()}', f'dns:{str(reader).lower()}',
                                f'rds-cluster:{identifier}'), text=text)

    def _elasticache(self, cli, region):
        data = self._call(cli, f'Searching ElastiCache in {region}',
                          ['elasticache', 'describe-cache-clusters', '--show-cache-node-info',
                           '--max-items', '500'])
        if not data:
            return
        for cluster in data.get('CacheClusters', []):
            identifier = cluster.get('CacheClusterId', '')
            engine = cluster.get('Engine', '')
            endpoint = ''
            for node in cluster.get('CacheNodes', []):
                endpoint = endpoint or (node.get('Endpoint') or {}).get('Address', '')
            text = _text(identifier, engine, endpoint, cluster.get('CacheNodeType'),
                         cluster.get('CacheSubnetGroupName'))
            if match_score(self.terms, text) <= 0:
                continue
            self._add(service='ElastiCache', kind='Cache cluster', name=identifier, region=region,
                      identifier=identifier, arn=cluster.get('ARN', ''),
                      url=elasticache_url(region, identifier, engine),
                      details={'Engine': engine,
                               'Engine version': cluster.get('EngineVersion', ''),
                               'Node type': cluster.get('CacheNodeType', ''),
                               'Status': cluster.get('CacheClusterStatus', ''),
                               'Endpoint': endpoint,
                               'Nodes': len(cluster.get('CacheNodes', []))},
                      relation='ElastiCache cluster', score=match_score(self.terms, text),
                      keys=(f'dns:{endpoint.lower()}', f'elasticache:{identifier}'), text=text)

    def _load_balancers(self, cli, region):
        data = self._call(cli, f'Searching load balancers in {region}',
                          ['elbv2', 'describe-load-balancers', '--max-items', '500'])
        if not data:
            return
        for balancer in data.get('LoadBalancers', []):
            name = balancer.get('LoadBalancerName', '')
            arn = balancer.get('LoadBalancerArn', '')
            dns = str(balancer.get('DNSName', '')).rstrip('.')
            text = _text(name, arn, dns, balancer.get('Scheme'), balancer.get('Type'),
                         balancer.get('VpcId'))
            if match_score(self.terms, text) <= 0 and dns.lower() != self.terms['host']:
                continue
            state = balancer.get('State', {})
            self._add(service='Elastic Load Balancing', kind='Load balancer', name=name,
                      region=region, identifier=dns or name, arn=arn,
                      url=load_balancer_url(region, arn),
                      details={'DNS name': dns,
                               'Scheme': balancer.get('Scheme', ''),
                               'Type': balancer.get('Type', ''),
                               'State': state.get('Code', ''),
                               'VPC': balancer.get('VpcId', ''),
                               'Availability zones': ', '.join(
                                   zone.get('ZoneName', '')
                                   for zone in balancer.get('AvailabilityZones', []))},
                      relation='Load balancer', score=match_score(self.terms, text),
                      keys=(f'dns:{dns.lower()}',), text=text)

    def _lambda(self, cli, region):
        data = self._call(cli, f'Searching Lambda in {region}',
                          ['lambda', 'list-functions', '--max-items', '1000'])
        if not data:
            return
        for function in data.get('Functions', []):
            name = function.get('FunctionName', '')
            arn = function.get('FunctionArn', '')
            text = _text(name, arn, function.get('Runtime'), function.get('Description'),
                         function.get('Handler'))
            if match_score(self.terms, text) <= 0:
                continue
            details = {'Runtime': function.get('Runtime', ''),
                       'Memory': function.get('MemorySize', ''),
                       'Timeout': function.get('Timeout', ''),
                       'State': function.get('State', ''),
                       'Last modified': function.get('LastModified', ''),
                       'Description': function.get('Description', '')}
            url_config = self._call(cli, f'Checking Lambda URL for {name}',
                                    ['lambda', 'get-function-url-config', '--function-name', name])
            if url_config and url_config.get('FunctionUrl'):
                details['Function URL'] = url_config['FunctionUrl']
            self._add(service='Lambda', kind='Function', name=name, region=region,
                      identifier=name, arn=arn, url=lambda_url(region, name), details=details,
                      relation='Lambda function', score=match_score(self.terms, text), text=text)

    def _api_gateway(self, cli, region):
        data = self._call(cli, f'Searching API Gateway in {region}',
                          ['apigateway', 'get-rest-apis', '--max-items', '500'])
        if data:
            for api in data.get('items', []):
                api_id = api.get('id', '')
                name = api.get('name', '')
                text = _text(api_id, name, api.get('description'), api.get('endpointConfiguration'))
                if match_score(self.terms, text) <= 0:
                    continue
                self._add(service='API Gateway', kind='REST API', name=name, region=region,
                          identifier=api_id, url=api_gateway_url(region, api_id),
                          details={'ID': api_id, 'Description': api.get('description', ''),
                                   'Created': api.get('createdDate', '')},
                          relation='API Gateway API', score=match_score(self.terms, text),
                          text=text)
        data = self._call(cli, f'Searching API Gateway domains in {region}',
                          ['apigateway', 'get-domain-names', '--max-items', '500'])
        if data:
            for domain in data.get('items', []):
                name = str(domain.get('domainName', ''))
                text = _text(name, domain.get('certificateName'), domain.get('regionalDomainName'),
                             domain.get('distributionDomainName'))
                if not (name.lower() == self.terms['host'] or match_score(self.terms, text) > 0):
                    continue
                self._add(service='API Gateway', kind='Custom domain', name=name, region=region,
                          identifier=name, url=api_gateway_domain_url(region, name),
                          details={'Regional domain': domain.get('regionalDomainName', ''),
                                   'CloudFront domain': domain.get('distributionDomainName', ''),
                                   'Certificate': domain.get('certificateName', '')},
                          relation='API Gateway custom domain',
                          score=220 if name.lower() == self.terms['host'] else match_score(self.terms, text),
                          keys=(f'dns:{name.lower()}',), text=text)

    def _ecs(self, cli, region):
        data = self._call(cli, f'Searching ECS in {region}',
                          ['ecs', 'list-clusters', '--max-items', '200'])
        if not data:
            return
        arns = data.get('clusterArns', [])
        for start in range(0, len(arns), 100):
            described = self._call(cli, f'Inspecting ECS clusters in {region}',
                                   ['ecs', 'describe-clusters', '--clusters', *arns[start:start + 100]])
            if not described:
                continue
            for cluster in described.get('clusters', []):
                name = cluster.get('clusterName', '')
                arn = cluster.get('clusterArn', '')
                text = _text(name, arn, cluster.get('status'), cluster.get('statistics'))
                if match_score(self.terms, text) <= 0:
                    continue
                self._add(service='ECS', kind='Cluster', name=name, region=region,
                          identifier=name, arn=arn, url=ecs_cluster_url(region, name),
                          details={'Status': cluster.get('status', ''),
                                   'Running tasks': cluster.get('runningTasksCount', ''),
                                   'Pending tasks': cluster.get('pendingTasksCount', ''),
                                   'Services': cluster.get('activeServicesCount', ''),
                                   'Registered instances': cluster.get('registeredContainerInstancesCount', '')},
                          relation='ECS cluster', score=match_score(self.terms, text), text=text)

    def _eks(self, cli, region):
        data = self._call(cli, f'Searching EKS in {region}',
                          ['eks', 'list-clusters', '--max-items', '200'])
        if not data:
            return
        for name in data.get('clusters', []):
            name = str(name)
            if match_score(self.terms, name) <= 0:
                continue
            arn = f'arn:aws:eks:{region}:{self.account}:cluster/{name}'
            self._add(service='EKS', kind='Cluster', name=name, region=region,
                      identifier=name, arn=arn, url=eks_cluster_url(region, name),
                      details={'Cluster name': name}, relation='EKS cluster',
                      score=match_score(self.terms, name), text=name)

    def _bedrock(self, cli, region):
        data = self._call(cli, f'Searching Bedrock models in {region}',
                          ['bedrock', 'list-custom-models', '--max-items', '200'])
        if data:
            for model in data.get('modelSummaries', []):
                name = model.get('modelName', '')
                arn = model.get('modelArn', '')
                text = _text(name, arn, model.get('baseModelArn'), model.get('modelStatus'))
                if match_score(self.terms, text) <= 0:
                    continue
                self._add(service='Bedrock', kind='Custom model', name=name, region=region,
                          identifier=arn, arn=arn, url=bedrock_custom_model_url(region, arn),
                          details={'Status': model.get('modelStatus', ''),
                                   'Base model': model.get('baseModelArn', ''),
                                   'Created': model.get('creationTime', '')},
                          relation='Bedrock custom model', score=match_score(self.terms, text),
                          text=text)
        data = self._call(cli, f'Searching Bedrock provisioned throughput in {region}',
                          ['bedrock', 'list-provisioned-model-throughputs', '--max-items', '100'])
        if data:
            for throughput in data.get('provisionedModelSummaries', []):
                name = throughput.get('provisionedModelName', '')
                arn = throughput.get('provisionedModelArn', '')
                text = _text(name, arn, throughput.get('modelArn'), throughput.get('status'))
                if match_score(self.terms, text) <= 0:
                    continue
                self._add(service='Bedrock', kind='Provisioned throughput', name=name,
                          region=region, identifier=arn, arn=arn,
                          url=bedrock_provisioned_url(region),
                          details={'Status': throughput.get('status', ''),
                                   'Model': throughput.get('modelArn', ''),
                                   'Model units': throughput.get('modelUnits', ''),
                                   'Commitment': throughput.get('commitmentDuration', '')},
                          relation='Bedrock capacity', score=match_score(self.terms, text),
                          text=text)
        data = self._call(cli, f'Searching Bedrock agents in {region}',
                          ['bedrock-agent', 'list-agents', '--max-items', '100'])
        if data:
            for agent in data.get('agentSummaries', []):
                agent_id = agent.get('agentId', '')
                name = agent.get('agentName', '')
                text = _text(agent_id, name, agent.get('agentStatus'), agent.get('description'))
                if match_score(self.terms, text) <= 0:
                    continue
                self._add(service='Bedrock Agents', kind='Agent', name=name, region=region,
                          identifier=agent_id, url=bedrock_agent_url(region, agent_id),
                          details={'ID': agent_id, 'Status': agent.get('agentStatus', ''),
                                   'Description': agent.get('description', ''),
                                   'Updated': agent.get('updatedAt', '')},
                          relation='Bedrock agent', score=match_score(self.terms, text),
                          text=text)
        data = self._call(cli, f'Searching Bedrock knowledge bases in {region}',
                          ['bedrock-agent', 'list-knowledge-bases', '--max-items', '100'])
        if data:
            for knowledge in data.get('knowledgeBaseSummaries', []):
                knowledge_id = knowledge.get('knowledgeBaseId', '')
                name = knowledge.get('name', '')
                text = _text(knowledge_id, name, knowledge.get('status'), knowledge.get('description'))
                if match_score(self.terms, text) <= 0:
                    continue
                self._add(service='Bedrock Agents', kind='Knowledge base', name=name,
                          region=region, identifier=knowledge_id,
                          url=bedrock_knowledge_base_url(region, knowledge_id),
                          details={'ID': knowledge_id, 'Status': knowledge.get('status', ''),
                                   'Description': knowledge.get('description', ''),
                                   'Updated': knowledge.get('updatedAt', '')},
                          relation='Bedrock knowledge base',
                          score=match_score(self.terms, text), text=text)

    def _resource_groups(self, cli, region):
        data = self._call(cli, f'Searching tagged resources in {region}',
                          ['resourcegroupstaggingapi', 'get-resources', '--max-items', '1000'])
        if not data:
            return set()
        buckets = set()
        for mapping in data.get('ResourceTagMappingList', []):
            arn = str(mapping.get('ResourceARN', ''))
            if not arn:
                continue
            parts = arn.split(':', 5)
            if len(parts) < 6:
                continue
            service = parts[2]
            resource = parts[5]
            tags = {tag.get('Key', ''): tag.get('Value', '')
                    for tag in mapping.get('Tags', []) if tag.get('Key')}
            name = tags.get('Name') or tags.get('name') or resource.rsplit('/', 1)[-1].rsplit(':', 1)[-1]
            text = _text(name, arn, tags)
            if service == 's3':
                buckets.add(resource)
                continue
            if match_score(self.terms, text) <= 0:
                continue
            self._add(service=service_name(service), kind='Resource', name=name,
                      region=region, identifier=resource, arn=arn,
                      url=arn_console_url(arn, region), details={'Tags': tags},
                      relation='Tagged AWS resource', score=match_score(self.terms, text),
                      keys=(f'{service}:{resource}',), text=text)
        return buckets

    def _scan_region(self, region):
        cli = AwsCli(self.profile.name, region, self.cancel)
        self._ec2(cli, region)
        self._rds(cli, region)
        self._elasticache(cli, region)
        self._load_balancers(cli, region)
        self._lambda(cli, region)
        self._api_gateway(cli, region)
        self._ecs(cli, region)
        self._eks(cli, region)
        self._bedrock(cli, region)
        self._acm(cli, region)
        return self._resource_groups(cli, region) or set()

    def find(self, url, regions=None):
        self.terms = target_terms(url)
        self.resources, self.seen, self.diagnostics = [], {}, []
        self.s3_hints = set()
        self._progress('Checking AWS identity')
        try:
            identity = self.cli.identity()
        except AwsStopped:
            raise
        except AwsError as exc:
            raise AwsError(f'Authentication failed for profile {self.profile.name}. '
                           'Use SSO login or refresh the profile credentials. ' + str(exc)) from exc
        self.account = str(identity.get('Account', ''))
        self._progress('Loading AWS regions')
        available = self.cli.available_regions()
        requested = [str(region).strip() for region in (regions or []) if str(region).strip()]
        if not requested:
            requested = [self.profile.region]
        if any(region.lower() in ('all', '*') for region in requested):
            searched = list(available)
        else:
            searched = list(dict.fromkeys(requested))
        for region in searched:
            if self.cancel is not None and self.cancel.is_set():
                raise AwsStopped('Cancelled')
        self._route53()
        self._cloudfront()
        buckets = set()
        for region in searched:
            buckets.update(self._scan_region(region))
        self._s3(buckets)
        self.resources.sort(key=lambda item: (-item['score'], item['service'].casefold(),
                                              item['name'].casefold(), item['region']))
        return dict(url=str(url).strip(), host=self.terms['host'], terms=self.terms,
                    profile=self.profile, identity=identity, account=self.account,
                    resources=self.resources, searched_regions=searched,
                    available_regions=available, diagnostics=self.diagnostics)


def sso_command(profile):
    binary = aws_binary() or 'aws'
    return f'{shlex.quote(binary)} sso login --profile {shlex.quote(profile.name)}'
