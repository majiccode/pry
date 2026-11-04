"""Fenced-code classification, explicit parameters and conservative previews."""
import re
import shlex

SHELLS = {'bash', 'zsh', 'sh', 'shell', 'console', 'linux', ''}


def parse_blocks(text):
    blocks = []
    for match in re.finditer(r'```([^\n`]*)\n(.*?)```', text, re.S):
        language, code = match.group(1).strip().lower(), match.group(2).strip()
        if code:
            blocks.append(dict(language=language, code=code, executable=language in SHELLS))
    return blocks


def parameter_names(text):
    return list(dict.fromkeys(re.findall(r'\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}', text)))


def fill_parameters(text, values):
    missing = [name for name in parameter_names(text) if name not in values]
    if missing:
        raise ValueError('Missing parameters: ' + ', '.join(missing) + '. Supply name=value arguments.')
    # Placeholders must represent whole shell words, never fragments in quotes.
    for match in re.finditer(r'\{\{[A-Za-z_][A-Za-z0-9_]*\}\}', text):
        before, after = text[:match.start()], text[match.end():]
        if before and before[-1] not in ' \t\n=|' or after and after[0] not in ' \t\n;|&':
            raise ValueError('Place {{parameters}} as unquoted whole words.')
        lexer = shlex.shlex(before, posix=True)
        try:
            list(lexer)
        except ValueError:
            raise ValueError('Place {{parameters}} outside quotes.')
    return re.sub(r'\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}', lambda m: shlex.quote(values[m.group(1)]), text)


def preview(command):
    if any(c in command for c in ';|&<>`$()\n'):
        raise ValueError('Preview is available only for simple rsync commands.')
    words = shlex.split(command)
    if not words or words[0] != 'rsync':
        raise ValueError('No reliable preview transformation for this command. Ask for a read-only diagnostic.')
    return shlex.join([words[0], '--dry-run', '--itemize-changes'] + words[1:])
