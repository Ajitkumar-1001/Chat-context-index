import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

commands = [
    ['python', '-m', 'ruff', 'check', '--config', 'packages/python/pyproject.toml', 'packages/python/src'],
    ['python', '-m', 'mypy', '--config-file', 'packages/python/pyproject.toml', 'packages/python/src/cci', '--ignore-missing-imports'],
    ['npm', '--prefix', 'packages/typescript', 'run', 'build'],
    ['python', '-m', 'pytest', 'tests/conformance', 'tests/memory', 'tests/examples', 'tests/evaluations', '-q', '--junitxml=/out/tests.xml'],
    ['node', '--test', 'tests/memory/tree_memory.test.mjs', 'tests/memory/evidence_selection.test.mjs'],
    ['python', 'tests/packaging/verify_packages.py', '--report', '/out/package-memory.json', '--artifacts-out', '/out/artifacts'],
]
commands.append(['python', 'benchmarks/reference_fixture_benchmark.py', '--out', '/out/performance.json', '--label', 'evidence-selection Linux container; shared host'])
report = {'platform': platform.platform(), 'python': platform.python_version(),
          'scope': 'Local Linux arm64 Docker reproduction of CI commands; not GitHub CI or a dedicated performance runner.',
          'source_hashes': {}, 'checks': []}
for kind, suffix in [('python','py'),('typescript','ts')]:
    digest=hashlib.sha256()
    source=Path('packages')/kind/'src'
    if kind=='python': source=source/'cci'
    for path in sorted(source.glob('*.'+suffix)):
        digest.update(path.name.encode()); digest.update(path.read_bytes())
    report['source_hashes'][kind]=digest.hexdigest()
for number, command in enumerate(commands,1):
    with Path(f'/out/check-{number}.log').open('w') as log:
        try:
            result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=900)
            code=result.returncode
        except subprocess.TimeoutExpired:
            code=124
    report['checks'].append({'command':command,'exit_code':code,'log':f'check-{number}.log'})
    print(f'check {number}: exit {code}',flush=True)
    Path('/out/linux-release-checks.json').write_text(json.dumps(report,indent=2)+'\n')
report['status']='PASS' if all(check['exit_code']==0 for check in report['checks']) else 'FAIL'
Path('/out/linux-release-checks.json').write_text(json.dumps(report,indent=2)+'\n')
raise SystemExit(int(report['status']!='PASS'))
