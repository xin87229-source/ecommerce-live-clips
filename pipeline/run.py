"""Shared input and non-overwriting output helpers; no legacy rendering entry."""
import json
import math
import re
from pathlib import Path


def read_json(path):
    value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('JSON顶层必须是对象')
    return value


def write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def text_field(value, label):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError(f'{label}缺失或格式错误')
    return value.strip()


def component(value, label):
    value = text_field(value, label)
    devices = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))}
    if not re.fullmatch(r'[\w-]{1,64}', value) or value.upper() in devices:
        raise ValueError(f'{label}只允许1至64位字母、数字、中文、下划线或连字符，禁用设备名')
    return value


def absolute(value, label):
    path = Path(text_field(value, label))
    if not path.is_absolute():
        raise ValueError(f'{label}必须是绝对路径')
    return path.resolve()


def seconds(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{2,}:[0-5]\d:[0-5]\d(?:\.\d{1,6})?', value):
        raise ValueError('剪辑时间格式须为HH:MM:SS或HH:MM:SS.sss')
    h, m, s = value.split(':')
    result = int(h) * 3600 + int(m) * 60 + float(s)
    if not math.isfinite(result):
        raise ValueError('剪辑时间必须有限')
    return result
