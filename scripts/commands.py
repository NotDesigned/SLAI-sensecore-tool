"""Format display-only commands for POSIX shells or PowerShell 7.3+."""
import re
import shlex
import sys


def format_command(args):
    if sys.platform == 'win32':
        # PowerShell single-quoted literals; use call operator for executable paths.
        quoted = ["'" + str(arg).replace("'", "''") + "'" for arg in args]
        if re.fullmatch(r'[A-Za-z0-9_.-]+', str(args[0])):
            return str(args[0]) + ' ' + ' '.join(quoted[1:])
        return '& ' + ' '.join(quoted)
    return shlex.join(args)
