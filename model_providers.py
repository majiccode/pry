"""Standard-library streaming clients for OpenAI-compatible and Anthropic APIs."""

import http.client
import json
import os
import re
import unicodedata
from collections.abc import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Do not forward credentials or replay/change a chat POST through a redirect.
urlopen = build_opener(_NoRedirect()).open


DEFAULT_SYSTEM = (
    'You are a minimalist macOS and Linux terminal and systems expert. Answer only '
    'questions about terminal commands, bash/zsh, coreutils, POSIX, networking, '
    'systems, and infrastructure. For other topics say: "I am scoped strictly to '
    'macOS and Linux systems." Be concise and direct, without filler. Put exact '
    'commands or scripts in Markdown code blocks and explain them in 1-2 sentences. '
    'During investigations distinguish observed facts from hypotheses and cite '
    'supplied evidence IDs. You may say "insufficient evidence" or "no further '
    'command needed" without a command block. Never claim a fix worked without '
    'verification.'
)


def validate_profile(profile):
    """Return the six normalized public fields, or raise a safe ValueError."""
    if not isinstance(profile, Mapping):
        raise ValueError('Provider profile must be a mapping.')
    result = {}
    for field in ('id', 'label', 'protocol', 'base_url', 'model', 'api_key_env'):
        value = profile.get(field, '' if field == 'api_key_env' else None)
        if not isinstance(value, str) or any(
            unicodedata.category(char).startswith('C') for char in value
        ):
            raise ValueError(f'Provider {field} must be text without control characters.')
        result[field] = value.strip()
        if field != 'api_key_env' and not result[field]:
            raise ValueError(f'Provider {field} must not be empty.')
    result['protocol'] = result['protocol'].lower()
    if result['protocol'] not in ('openai', 'anthropic'):
        raise ValueError('Provider protocol must be openai or anthropic.')
    if result['api_key_env'] and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', result['api_key_env']):
        raise ValueError('Provider api_key_env must be an environment variable identifier.')
    url = result['base_url']
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
        valid = (
            parts.scheme in ('http', 'https') and host
            and parts.username is None and parts.password is None
            and not any(char.isspace() for char in url)
            and not any(char in url for char in ('?', '#', '\\'))
            and (port is None or 0 < port <= 65535)
        )
        if not valid:
            raise ValueError
    except ValueError:
        raise ValueError('Provider base_url must be an HTTP(S) URL without credentials, query, or fragment.') from None
    if parts.scheme == 'http' and host.lower() not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('Provider base_url requires HTTPS except for localhost, 127.0.0.1, or ::1.')
    result['base_url'] = url.rstrip('/')
    return result


def _sse_events(response):
    """Join data fields per SSE event; ignore comments and other SSE fields."""
    data = []
    event = ''
    for raw in response:
        try:
            line = raw.decode('utf-8').rstrip('\r\n')
        except UnicodeError:
            raise ValueError('Provider returned an invalid UTF-8 event stream.') from None
        if line.startswith('\ufeff'):
            line = line[1:]
        if not line:
            if data or event:
                yield event, '\n'.join(data)
            data, event = [], ''
        elif not line.startswith(':'):
            field, separator, value = line.partition(':')
            if separator and value.startswith(' '):
                value = value[1:]
            if field == 'data':
                data.append(value)
            elif field == 'event':
                event = value
    if data or event:
        yield event, '\n'.join(data)


def _http_error(status):
    explanation = {
        301: 'Use the final API base URL; redirects are disabled to protect credentials.',
        302: 'Use the final API base URL; redirects are disabled to protect credentials.',
        303: 'Use the final API base URL; redirects are disabled to protect credentials.',
        307: 'Use the final API base URL; redirects are disabled to protect credentials.',
        308: 'Use the final API base URL; redirects are disabled to protect credentials.',
        400: 'Check the model, messages, and provider settings.',
        401: 'Check the configured API key and its environment variable.',
        403: 'Check API key permissions and model access.',
        404: 'Check the API base URL (including /v1) and model name.',
        408: 'The provider timed out; try again when ready.',
        429: 'Check provider rate limits, quota, and billing.',
    }.get(status, 'Check provider availability and settings.')
    return ValueError(f'Provider HTTP {status}. {explanation}')


def stream_chat(profile, messages):
    """Yield text deltas; raise ValueError on invalid input or incomplete replies.

    Messages are role/content mappings with string content. An explicit system
    message replaces DEFAULT_SYSTEM. Keys are resolved when iteration starts.
    No failed POST is retried; errors never include remote bodies or messages.
    """
    profile = validate_profile(profile)
    env = profile['api_key_env']
    key = os.environ.get(env) if env else None
    if env and not key:
        raise ValueError(f'Set the {env} environment variable before requesting this provider.')
    if key and (not key.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in key)):
        raise ValueError(f'The {env} environment variable is not a valid HTTP API key.')
    conversation = []
    try:
        for message in messages:
            role, content = message['role'], message['content']
            if role not in ('system', 'user', 'assistant') or not isinstance(content, str):
                raise ValueError
            conversation.append({'role': role, 'content': content})
    except (TypeError, KeyError, ValueError):
        raise ValueError('Messages must contain system, user, or assistant roles and text content.') from None
    if not any(message['role'] != 'system' for message in conversation):
        raise ValueError('At least one user or assistant message is required.')
    if not any(message['role'] == 'system' for message in conversation):
        conversation.insert(0, {'role': 'system', 'content': DEFAULT_SYSTEM})
    payload = {'model': profile['model'], 'max_tokens': 1800, 'stream': True}
    headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream'}
    if profile['protocol'] == 'openai':
        endpoint = '/chat/completions'
        payload['messages'] = conversation
        if key:
            headers['Authorization'] = f'Bearer {key}'
    else:
        endpoint = '/messages'
        headers['anthropic-version'] = '2023-06-01'
        if key:
            headers['x-api-key'] = key
        payload['system'] = '\n\n'.join(m['content'] for m in conversation if m['role'] == 'system')
        grouped = []
        for message in conversation:
            if message['role'] == 'system':
                continue
            if grouped and grouped[-1]['role'] == message['role']:
                grouped[-1]['content'] += '\n\n' + message['content']
            else:
                grouped.append(dict(message))
        payload['messages'] = grouped
    request = Request(profile['base_url'] + endpoint,
                      data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
    completed = False
    has_text = False
    try:
        with urlopen(request, timeout=60) as response:
            for event, data in _sse_events(response):
                if event == 'error':
                    raise ValueError('Provider reported an API error; check model access, quota, and settings.')
                if not data.strip():
                    continue
                if data.strip() == '[DONE]':
                    if profile['protocol'] == 'openai':
                        completed = True
                        break
                    raise ValueError('Provider returned an unexpected completion event.')
                try:
                    chunk = json.loads(data)
                    if not isinstance(chunk, dict):
                        raise ValueError
                    if 'error' in chunk or chunk.get('type') == 'error':
                        raise ValueError('api_error')
                    text = None
                    if profile['protocol'] == 'openai':
                        choices = chunk.get('choices', [])
                        if choices:
                            choice = choices[0]
                            text = choice.get('delta', {}).get('content')
                            completed = choice.get('finish_reason') is not None
                    else:
                        kind = chunk.get('type', event)
                        if kind == 'content_block_delta':
                            delta = chunk['delta']
                            if delta.get('type') == 'text_delta':
                                text = delta['text']
                        completed = kind == 'message_stop'
                    if text is not None and not isinstance(text, str):
                        raise ValueError
                except (ValueError, KeyError, TypeError, AttributeError, IndexError):
                    raise ValueError('Provider returned an API error or invalid stream data; check provider settings.') from None
                if text:
                    has_text = has_text or bool(text.strip())
                    yield text
                if completed:
                    break
    except HTTPError as error:
        error.close()
        raise _http_error(error.code) from None
    except (URLError, OSError, http.client.HTTPException):
        raise ValueError('Cannot reach provider or read its stream; check connectivity and the API base URL. The request was not retried.') from None
    if not completed:
        raise ValueError('Provider stream ended before completion; the reply may be incomplete. The request was not retried.')
    if not has_text:
        raise ValueError('Provider completed without a text reply; check the model and provider settings.')
