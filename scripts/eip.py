"""Forward EIP commands with a temporary region/zone profile for SCO v2."""
import argparse
from pathlib import Path
import subprocess
import tempfile

import tomlkit

from scripts import cli


def main(args):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--zone', required=True)
    options, forwarded = parser.parse_known_args(args)
    if not forwarded:
        forwarded = ['--help']
    config = cli.load_config()
    env, executable = cli.runtime(config)
    profile = cli.string_value(config['sco'], 'profile') or 'default'
    if Path(profile).name != profile or profile in ('.', '..'):
        raise cli.ConfigError('SCO Profile 名称无效。')
    original = Path(env['SCO_CONFIG']) / 'profiles' / (profile + '.toml')
    try:
        document = tomlkit.parse(original.read_text())
    except tomlkit.exceptions.ParseError:
        raise cli.ConfigError('SCO Profile 格式无效。') from None
    region = cli.string_value(config['sco'], 'region')
    regions = config.get('regions', {})
    name = regions.get(region, region) if isinstance(regions, dict) else region
    if not name:
        name = document.get('default', {}).get('region')
    if not isinstance(name, str) or name not in document.get('regions', {}):
        raise cli.ConfigError('所选 Region 尚未在此 SCO Profile 初始化。')
    # SCO v2 rejects the legacy component's --zone before dispatching. Set only
    # the subprocess profile's zone; retain the original profile and credentials.
    document['regions'][name]['zone'] = options.zone
    with tempfile.TemporaryDirectory(prefix='slai-eip-') as directory:
        root = Path(directory)
        (root / 'profiles').mkdir()
        target = root / 'profiles' / (profile + '.toml')
        target.touch(mode=0o600)
        target.write_text(tomlkit.dumps(document))
        (root / 'active_profile').write_text(profile + '\n')
        env['SCO_CONFIG'] = directory
        command = [str(executable), '--profile', profile]
        if region:
            command.extend(['--region', region])
        command.extend(['eip', *forwarded])
        return subprocess.run(command, env=env, check=False).returncode
