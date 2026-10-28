"""Small apparel vocabulary for traceable, nonnumeric display edits."""
import json
import re

from runtime import INSTALL_ROOT, profile as runtime_profile

dictionary_path = INSTALL_ROOT / 'pipeline/subtitle_dictionary.json'
NUMERIC = r'[零〇一二两兩三四五六七八九十百千万萬亿億\d]'


def plain(value, label, limit=100):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or re.search(r'[{}\\\x00-\x1f\x7f]', value)):
        raise ValueError(label + '须为不含控制符或ASS代码的文字')
    return value


def validate(dictionary):
    if not isinstance(dictionary, dict) or not isinstance(dictionary.get('replacements'), dict):
        raise ValueError('词典须保留replacements对象')
    if 'apparel' not in dictionary:
        return dictionary  # Existing formal-subtitle dictionaries remain valid.
    apparel = dictionary['apparel']
    if not isinstance(apparel, dict) or set(apparel) != {'terms', 'rules'}:
        raise ValueError('apparel词库须包含terms和rules')
    groups, rules = apparel['terms'], apparel['rules']
    if not isinstance(groups, dict) or not groups or not isinstance(rules, list) or len(rules) > 32:
        raise ValueError('女装词汇分组或规则列表无效')
    terms = []
    for group, values in groups.items():
        plain(group, '词汇分组', 30)
        if not isinstance(values, list) or not values:
            raise ValueError('词汇分组须为非空列表')
        terms.extend(plain(value, '行业词汇', 24) for value in values)
    if len(terms) > 80 or len(set(terms)) != len(terms):
        raise ValueError('行业词汇重复或超过80个')
    identifiers, sources = set(), set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {'id', 'from', 'to', 'reason'}:
            raise ValueError('规范化规则须包含id、from、to和reason')
        identifier = rule['id']
        if not isinstance(identifier, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', identifier):
            raise ValueError('规范化规则id无效')
        old, new = plain(rule['from'], '原词'), plain(rule['to'], '规范词')
        plain(rule['reason'], '规则原因', 300)
        if old == new or identifier in identifiers or old in sources:
            raise ValueError('规范化规则重复或没有变化')
        if re.search(NUMERIC, old + new):
            raise ValueError('行业词库不得改写含数字的短语')
        identifiers.add(identifier)
        sources.add(old)
    return dictionary


def load():
    return validate(json.loads(dictionary_path.read_text(encoding='utf-8-sig')))


def selected_profile(profile_name):
    name = runtime_profile()['name'] if profile_name is None else profile_name
    if name not in ('basic', 'apparel'):
        raise ValueError('profile只允许basic或apparel')
    return name


def normalize(text, dictionary=None, profile_name=None):
    """Apply nonoverlapping matches once; edit ranges always name the unchanged input."""
    if not isinstance(text, str):
        raise ValueError('字幕原文须为字符串')
    if selected_profile(profile_name) == 'basic':
        return text, []
    data = load() if dictionary is None else validate(dictionary)
    matches = []
    for rule in data.get('apparel', {}).get('rules', []):
        offset, occurrence = 0, 0
        while (start := text.find(rule['from'], offset)) >= 0:
            occurrence += 1
            offset = start + len(rule['from'])
            matches.append((start, offset, rule, occurrence))
    result, edits, previous = [], [], 0
    for start, end, rule, occurrence in sorted(matches, key=lambda item: (item[0], item[1])):
        if start < previous:
            raise ValueError('同一原文范围命中重叠词库规则，须先消除歧义')
        result.extend((text[previous:start], rule['to']))
        edits.append({'rule': rule['id'], 'from': rule['from'], 'to': rule['to'],
                      'occurrence': occurrence, 'range': [start, end], 'reason': rule['reason']})
        previous = end
    return ''.join(result) + text[previous:], edits
