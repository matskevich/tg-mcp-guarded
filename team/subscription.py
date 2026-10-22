#!/usr/bin/env python3
"""Subscribe to the owner's stable tg-mcp channel; verify local code and project."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import urllib.request

CHANNEL = 'https://raw.githubusercontent.com/matskevich/tg-mcp-guarded/main/team/stable.json'
REMOTE = 'https://github.com/matskevich/gconf-core'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def inspect(operator, project, channel):
    root = Path.home() / 'Library/Application Support/gconf-telegram' / operator
    runtime = root / 'runtime'
    mismatches = [name for name, expected in channel['runtime_files'].items()
                  if not (runtime / name).is_file()
                  or (runtime / name).is_symlink()
                  or hashlib.sha256((runtime / name).read_bytes()).hexdigest() != expected]
    skill = Path.home() / '.codex/skills/gconf-personal-sales-telegram'
    skill_hashes = channel.get('global_skill_files', {})
    skill_mismatches = [name for name, expected in skill_hashes.items()
                        if not (skill / name).is_file()
                        or hashlib.sha256((skill / name).read_bytes()).hexdigest() != expected]
    try:
        remote = subprocess.check_output(['git', '-C', str(project), 'remote', 'get-url', 'origin'],
                                         text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError:
        remote = ''
    project_ok = remote.removesuffix('.git').replace('git@github.com:', 'https://github.com/') == REMOTE
    subscription = root / 'subscription.json'
    subscribed = subscription.exists() and json.loads(subscription.read_text()).get('channel') == CHANNEL
    plist = Path.home() / 'Library/LaunchAgents' / f'ai.gconf.telegram.{operator}.updates.plist'
    loaded = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/ai.gconf.telegram.{operator}.updates'],
                            capture_output=True).returncode == 0 if sys.platform == 'darwin' else False
    installed = {}
    if shutil.which('codex'):
        rows = json.loads(subprocess.check_output(['codex', 'mcp', 'list', '--json'], text=True))
        for lane in ['read', 'actions']:
            name = f'gconf-{operator}-{lane}'
            row = next((r for r in rows if r['name'] == name), None)
            # Never echo configuration, which can contain credentials.
            installed[lane] = bool(row and str(runtime) in json.dumps(row))
    return {'operator': operator, 'channel': CHANNEL, 'channel_version': channel['version'],
            'runtime_matches_owner_release': not mismatches, 'mismatched_files': mismatches,
            'scope': 'gconf personal sales only',
            'global_skill_matches_release': bool(skill_hashes) and not skill_mismatches,
            'global_skill_mismatched_files': skill_mismatches,
            'project_origin_verified': project_ok, 'project_path': str(project),
            'subscription_registered': subscribed, 'daily_check_file': plist.exists(),
            'daily_check_loaded': loaded, 'mcp_config_points_to_runtime': installed,
            'live_account_and_tool_call': 'requires live mcp receipt; configuration is not usage'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['subscribe', 'check'])
    parser.add_argument('--operator', required=True, choices=['anna', 'karina', 'natasha'])
    parser.add_argument('--project', type=Path, required=True)
    args = parser.parse_args()
    root = Path.home() / 'Library/Application Support/gconf-telegram' / args.operator
    try:
        request = urllib.request.Request(CHANNEL, headers={'Cache-Control': 'no-cache'})
        channel = json.loads(urllib.request.urlopen(request, timeout=20).read())
        assert channel['repository'] == 'matskevich/tg-mcp-guarded'
        assert all(not Path(p).is_absolute() and '..' not in Path(p).parts
                   for p in [*channel['runtime_files'], *channel.get('global_skill_files', {})])
    except Exception as error:
        write_json(root / 'update-status.json', {'channel_check': 'unavailable', 'error_type': type(error).__name__})
        raise SystemExit('channel check unavailable; do not claim current version')
    if args.phase == 'subscribe':
        if sys.platform != 'darwin':
            raise SystemExit('macos launchagent required')
        destination = root / 'subscription.py'
        if Path(__file__).resolve() != destination.resolve():
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(__file__, destination)
            destination.chmod(0o600)
        launchdir = Path.home() / 'Library/LaunchAgents'
        launchdir.mkdir(parents=True, exist_ok=True)
        plist = launchdir / f'ai.gconf.telegram.{args.operator}.updates.plist'
        desired = {'Label': f'ai.gconf.telegram.{args.operator}.updates',
                   'ProgramArguments': [sys.executable, str(destination), 'check', '--operator', args.operator,
                                        '--project', str(args.project.resolve())],
                   'StartInterval': 86400, 'RunAtLoad': True}
        if plist.exists() and plistlib.loads(plist.read_bytes()) != desired:
            raise SystemExit('existing subscription differs; inspect before replacing')
        if not plist.exists():
            plist.write_bytes(plistlib.dumps(desired)); plist.chmod(0o600)
        domain = f'gui/{os.getuid()}'
        present = subprocess.run(['launchctl', 'print', domain + '/' + desired['Label']], capture_output=True)
        if present.returncode:
            subprocess.run(['launchctl', 'bootstrap', domain, str(plist)], check=True, capture_output=True)
        write_json(root / 'subscription.json', {'channel': CHANNEL, 'operator': args.operator,
                    'project': str(args.project.resolve()), 'mode': 'daily check and agent preflight; no unattended code replacement'})
    report = inspect(args.operator, args.project.resolve(), channel)
    write_json(root / 'update-status.json', report)
    print(json.dumps(report, ensure_ascii=False))
    if (not report['runtime_matches_owner_release'] or not report['project_origin_verified']
            or not report['global_skill_matches_release']):
        raise SystemExit(10)


if __name__ == '__main__':
    main()
