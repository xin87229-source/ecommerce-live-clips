"""Build a local candidate from an exact source allowlist. Never publish."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import zipfile

INSTALL = Path(__file__).resolve().parents[1]
TOP_FILES = {'VERSION', 'README.md', 'THIRD_PARTY_NOTICES.md', 'LICENSE',
             'requirements.txt', 'release-files.txt', '.gitignore', 'cli.py', 'CHANGELOG.md'}
DIRECTORIES = {'docs', 'pipeline', 'presets', 'tests', 'tools', 'examples', '.agents'}
EXTENSIONS = {'.py', '.md', '.txt', '.json', '.yaml', '.yml'}
PRIVATE_PARTS = {'outputs', 'private-context', 'workspace', 'runtime', 'models',
                 '__pycache__', '.venv', '.git', 'materials', 'handoff'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def scan(name, data):
    text = data.decode('utf-8-sig')
    findings = []
    if re.search(r'[A-Za-z]:[\\/]+(?:Users|CodexWork)[\\/]+', text, re.I):
        findings.append('private_machine_path')
    if re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', text):
        findings.append('private_key')
    if re.search(r'(?i)(?:api_key|password|access_token)[\"\x27]?\s*[=:]\s*[\"\x27][A-Za-z0-9_\-/+]{16,}', text):
        findings.append('credential_assignment')
    return [{'file': name, 'rule': rule} for rule in findings]


def source_files(source):
    whitelist = source / 'release-files.txt'
    paths = [row.strip() for row in whitelist.read_text('utf-8-sig').splitlines()
             if row.strip() and not row.lstrip().startswith('#')]
    if len(paths) != len(set(paths)) or not {'VERSION', 'cli.py', 'README.md', 'release-files.txt'} <= set(paths):
        raise ValueError('白名单重复或缺少必要入口')
    files, findings = {}, []
    for name in sorted(paths):
        rel = PurePosixPath(name)
        if ('\\' in name or ':' in name or rel.is_absolute() or '..' in rel.parts
                or str(rel) != name or any(part in PRIVATE_PARTS for part in rel.parts)
                or (len(rel.parts) == 1 and name not in TOP_FILES)
                or (len(rel.parts) > 1 and rel.parts[0] not in DIRECTORIES)
                or (name not in TOP_FILES and rel.suffix not in EXTENSIONS)):
            raise ValueError('白名单路径或文件类型不允许: ' + name)
        path = source / Path(name)
        current = path
        while current != source:
            if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
                raise ValueError('候选不接受符号链接或目录联接: ' + name)
            current = current.parent
        if not path.is_file() or not path.resolve().is_relative_to(source):
            raise ValueError('白名单文件缺失或超出源码: ' + name)
        data = path.read_bytes()
        findings.extend(scan(name, data))
        files[name] = data
    if findings:
        raise ValueError('隐私检查待处理: ' + json.dumps(findings, ensure_ascii=False))
    return files


def build(source, output_root):
    source, output_root = Path(source).resolve(strict=True), Path(output_root).resolve()
    if output_root == source or output_root.is_relative_to(source):
        raise ValueError('候选输出须在源码目录外')
    files = source_files(source)
    version = files['VERSION'].decode('utf-8-sig').strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?', version):
        raise ValueError('VERSION 格式无效')
    rows = [{'path': name, 'bytes': len(data), 'sha256': digest(data)} for name, data in files.items()]
    identity = digest(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode())
    candidate_id = f'ecommerce-live-clips-{version}-{identity[:12]}'
    folder = output_root / candidate_id
    archive_path = output_root / (candidate_id + '.zip')
    if folder.exists() or archive_path.exists():
        raise FileExistsError('同版本同内容候选已存在，不覆盖')
    files['SOURCE-MANIFEST.json'] = encoded({'schema_version': 1, 'version': version,
        'source_manifest_sha256': identity, 'files': rows,
        'scope': 'exact release-files.txt entries; generated metadata excluded from source digest'})
    files['CANDIDATE.json'] = encoded({'id': candidate_id, 'version': version,
        'source_manifest_sha256': identity, 'status': 'LOCAL_CANDIDATE_NOT_PUBLISHED',
        'license_decision': 'see_LICENSE' if 'LICENSE' in files else 'pending_owner_choice',
        'public_release_authorized': False,
        'external_operator_trials': 'not_verified', 'new_computer_installation': 'not_verified',
        'business_acceptance': 'pending_real_review', 'state_reference': 'docs/release-status.md'})
    files['PRIVACY-SCAN.json'] = encoded({'status': 'automated_checks_passed',
        'files_scanned': len(rows), 'findings': [], 'scope': 'allowlisted UTF-8 source only',
        'limitations': 'Pattern scan is not a guarantee; inspect the exact manifest before public release.'})
    folder.mkdir(parents=True, exist_ok=False)
    for name, data in files.items():
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(data)
    with zipfile.ZipFile(archive_path, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            info = zipfile.ZipInfo(candidate_id + '/' + name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    with zipfile.ZipFile(archive_path) as archive:
        if archive.testzip() is not None:
            raise ValueError('ZIP 校验失败，保留现场')
        for name, data in files.items():
            if archive.read(candidate_id + '/' + name) != data or (folder / name).read_bytes() != data:
                raise ValueError('候选内容回读不一致，保留现场')
    return {'id': candidate_id, 'folder': str(folder), 'archive': str(archive_path),
            'archive_sha256': digest(archive_path.read_bytes()), 'source_files': len(rows),
            'source_manifest_sha256': identity, 'published': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(build(INSTALL, args.output_root), ensure_ascii=False, indent=2))
    except (OSError, ValueError) as error:
        print(json.dumps({'status': 'ERROR', 'error': str(error)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
