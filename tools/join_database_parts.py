"""Join database byte parts, checking each checksum and the complete archive."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

BUFFER = 4 * 1024 * 1024

def join_parts(manifest_path, parts_dir, output):
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    archive, parts = manifest['archive'], manifest['parts']
    name = archive['name']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*\.tar\.zst', name):
        raise ValueError('Unsafe archive name')
    if not parts or [p['index'] for p in parts] != list(range(1, len(parts) + 1)):
        raise ValueError('Parts must be present and ordered without gaps')
    for record in [archive, *parts]:
        if type(record['size']) is not int or record['size'] <= 0:
            raise ValueError('Invalid positive file size')
        if not re.fullmatch(r'[0-9a-f]{64}', record['sha256']) or not re.fullmatch(r'[0-9a-f]{32}', record['md5']):
            raise ValueError('Invalid checksum')
    if sum(p['size'] for p in parts) != archive['size']:
        raise ValueError('Part sizes do not add up to the archive size')
    root = Path(parts_dir).resolve()
    target = Path(output).absolute()
    temporary = target.with_name(target.name + '.partial')
    if target.exists() or target.is_symlink() or temporary.exists() or temporary.is_symlink():
        raise FileExistsError('Output or temporary file exists; overwrite is refused')
    paths = []
    for part in parts:
        expected = name + '.part' + str(part['index']).zfill(3)
        if part['name'] != expected:
            raise ValueError('Unexpected part filename')
        path = root / expected
        if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError('Part must not be a link or reparse point')
        if path.stat().st_size != part['size']:
            raise ValueError('Part size differs from the manifest: ' + expected)
        paths.append(path)
    probe = target.parent
    while not probe.exists():
        probe = probe.parent
    if shutil.disk_usage(probe).free < archive['size'] + 256 * 1024 * 1024:
        raise OSError('Joining needs room for another compressed archive plus 256 MiB')
    target.parent.mkdir(parents=True, exist_ok=True)
    whole_sha, whole_md5, written = hashlib.sha256(), hashlib.md5(), 0
    with temporary.open('xb') as destination:
        for part, path in zip(parts, paths):
            sha, md5, size = hashlib.sha256(), hashlib.md5(), 0
            with path.open('rb') as source:
                while True:
                    block = source.read(BUFFER)
                    if not block:
                        break
                    destination.write(block)
                    sha.update(block); md5.update(block)
                    whole_sha.update(block); whole_md5.update(block)
                    size += len(block); written += len(block)
            if size != part['size'] or sha.hexdigest() != part['sha256'] or md5.hexdigest() != part['md5']:
                raise ValueError('Part checksum differs from the manifest: ' + path.name)
    if written != archive['size'] or whole_sha.hexdigest() != archive['sha256'] or whole_md5.hexdigest() != archive['md5']:
        raise ValueError('Combined archive checksum differs from the manifest')
    # Exclusive hard-link creation atomically refuses an output created meanwhile.
    # On a filesystem without hard links the verified .partial remains intact.
    os.link(temporary, target)
    temporary.unlink()
    return {'file': str(target), 'size': written, 'sha256': whole_sha.hexdigest(),
            'md5': whole_md5.hexdigest(), 'verified': True}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--parts-dir', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(join_parts(args.manifest, args.parts_dir, args.output), ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
