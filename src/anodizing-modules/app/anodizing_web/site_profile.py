"""Optional operator-supplied site defaults.

The service ships no robot address, scene, project name, camera serial, joint
bounds or tool dimensions. An operator may place ``site-profile.json`` in the
allowed data directory to prefill reviewed values.
"""
import json
import math
from pathlib import Path


MAX_BYTES = 100_000


def load(root):
    """Return the profile object, or an empty object when none is usable."""
    path = Path(root) / 'site-profile.json'
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
            return {}
        value = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def section(root, name):
    """Return one named object section from the optional profile."""
    value = load(root).get(name)
    return value if isinstance(value, dict) else {}


def finite_numbers(value, size):
    """Validate a flat list of finite numbers without accepting booleans."""
    try:
        valid = (isinstance(value, list) and len(value) == size
                 and all(type(item) in (int, float) and math.isfinite(item) for item in value))
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError('Enter a finite number for each value.')
    return value
