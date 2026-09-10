"""Maintain a small, idempotent SCO block in Bash and Zsh startup files."""
import os
from pathlib import Path
import shlex

START = '# >>> SLAI-tool SCO environment >>>'
END = '# <<< SLAI-tool SCO environment <<<'


def configure(env):
    home = Path.home()
    zdir = Path(os.environ.get('ZDOTDIR') or home).expanduser()
    login = next((home / name for name in ('.bash_profile', '.bash_login', '.profile')
                  if (home / name).exists()), home / '.bash_profile')
    paths = list(dict.fromkeys([home / '.bashrc', login, zdir / '.zshrc', zdir / '.zprofile']))
    block = '\n'.join([START, *[f'export {key}={shlex.quote(env[key])}'
                     for key in ('SCO_HOME', 'SCO_DATA_HOME', 'SCO_CONFIG')],
                     'case ":$PATH:" in', '  *":$SCO_HOME/bin:"*) ;;',
                     '  *) export PATH="$SCO_HOME/bin:$PATH" ;;', 'esac', END])
    edits = []
    for path in paths:
        text = path.read_text() if path.exists() else ''
        if START in text or END in text:
            if text.count(START) != 1 or text.count(END) != 1 or text.index(END) < text.index(START):
                raise ValueError(f'{path} 中的 SCO 环境配置标记异常，请先检查。')
            before, rest = text.split(START, 1)
            _, after = rest.split(END, 1)
            updated = before + block + after
        else:
            updated = text + ('\n' if text and not text.endswith('\n') else '') + block + '\n'
        edits.append((path, text, updated))
    for path, original, updated in edits:
        if original != updated:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(updated)
    return paths
