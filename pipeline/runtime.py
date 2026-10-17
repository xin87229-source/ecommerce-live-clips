"""Installed resources and one explicitly selected local work directory."""
import json
import math
import os
from pathlib import Path

INSTALL_ROOT = Path(__file__).resolve().parent.parent
_selected = Path(os.environ.get('ECLIP_WORKSPACE') or INSTALL_ROOT / 'workspace').expanduser()
if not _selected.is_absolute():
    raise ValueError('ECLIP_WORKSPACE必须是绝对路径')
ROOT = _selected.resolve()


def config():
    path = ROOT / 'config.json'
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('config.json顶层必须是对象')
    return value


def stage_config(path=None):
    """Stage compatibility argument may only name this workspace's config."""
    expected = (ROOT / 'config.json').resolve()
    if path is not None and Path(path).expanduser().resolve() != expected:
        raise ValueError('配置必须来自当前工作目录的config.json')
    return config()


def profile():
    settings = config()
    name = settings.get('profile', 'basic')
    if name not in ('basic', 'apparel'):
        raise ValueError('profile只允许basic或apparel')
    value = json.loads((INSTALL_ROOT / 'presets' / f'{name}.json').read_text(encoding='utf-8'))
    maximum = settings.get('max_duration_seconds', value['max_duration_seconds'])
    if type(maximum) not in (int, float) or not math.isfinite(maximum) or not 0 < maximum <= 70:
        raise ValueError('max_duration_seconds须大于0且不超过70')
    value['max_duration_seconds'] = maximum
    return value
