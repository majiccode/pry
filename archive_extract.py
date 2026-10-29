"""Extract ZIP/tar files into a chosen directory without escaping it."""
import argparse
import shutil
import stat
import tarfile
import zipfile
from pathlib import Path


def extract(source, destination):
    root = Path(destination).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(source) if zipfile.is_zipfile(source) else tarfile.open(source) as archive:
        members = archive.infolist() if isinstance(archive, zipfile.ZipFile) else archive.getmembers()
        for member in members:
            zipped = isinstance(archive, zipfile.ZipFile)
            name = member.filename if zipped else member.name
            target = (root / name).resolve()
            if Path(name).is_absolute() or not target.is_relative_to(root):
                raise ValueError('Archive entry escapes the destination: ' + name)
            directory = member.is_dir() if zipped else member.isdir()
            regular = not stat.S_ISLNK(member.external_attr >> 16) if zipped else member.isfile()
            if not directory and not regular:
                raise ValueError('Unsupported archive link or special file: ' + name)
            if directory:
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            handle = archive.open(member) if zipped else archive.extractfile(member)
            with handle, target.open('xb') as output:
                shutil.copyfileobj(handle, output)
            count += 1
    return count


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive')
    parser.add_argument('destination')
    args = parser.parse_args()
    try:
        print(f'Extracted {extract(args.archive, args.destination)} files to {args.destination}')
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        parser.exit(1, str(exc) + '\n')
