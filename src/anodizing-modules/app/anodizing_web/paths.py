"""Constrain browser-selected paths to the service data directory."""
from pathlib import Path


def inside(root, value, *, required=True):
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise ValueError('Enter a path inside the IPC data directory.')
    root = Path(root).resolve()
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_relative_to(root):
        raise ValueError('Path is outside the IPC data directory.')
    if path != root and path.relative_to(root).parts[0] == '.jobs':
        raise ValueError('The job store is not an operator data path.')
    if required and not path.exists():
        raise ValueError('The IPC path does not exist.')
    return path


def check_tree(root, directory):
    directory = inside(root, str(directory), required=False)
    if directory == Path(root).resolve():
        raise ValueError('Choose a subdirectory of the IPC data directory.')
    if directory.exists():
        for index, entry in enumerate(directory.rglob('*')):
            if index >= 10000:
                raise ValueError('Directory contains too many files.')
            if entry.is_symlink():
                raise ValueError('Project and session directories cannot contain symbolic links.')
    return directory
