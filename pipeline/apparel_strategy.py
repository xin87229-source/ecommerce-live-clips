"""Read-only apparel shot guidance; consumes observations, never recognizes video."""
import argparse
import copy
import json
from pathlib import Path


def _presets():
    return json.loads((Path(__file__).resolve().parents[1] / 'presets' /
                       'apparel-strategies.json').read_text(encoding='utf-8'))


def strategy_for(category, style='auto'):
    """Return category guidance. Unknown category falls back; unknown style is retained."""
    data = _presets()
    requested = '' if category is None else str(category).strip()
    normalized = requested.casefold()
    key = next((key for key, item in data['categories'].items()
                if normalized in [key, *(alias.casefold() for alias in item['aliases'])]), 'other')
    result = copy.deepcopy(data['categories'][key])
    result.pop('aliases')
    style_name = 'auto' if style is None else str(style).strip() or 'auto'
    result.update(category=key, requested_category=requested, avoid=data['avoid'],
                  style={'name': style_name, 'known': style_name in data['styles'],
                         'guidance': data['styles'].get(style_name,
                             '保留用户风格原词；按原片实际内容表达，无法明确执行的要求待确认。')})
    return result


def evaluate_shots(category, observations):
    """Match exact focus tags with supplied evidence text; evidence is not verified here.

    observations: list of {"tag": focus_shot, "evidence": source/time/observation text}.
    Missing/unknown tags and empty evidence are ignored, never inferred from names.
    """
    if not isinstance(observations, (list, tuple)):
        raise ValueError('observations 必须是含 tag 和 evidence 的观察记录列表')
    strategy = strategy_for(category)
    focus = strategy['focus_shots']
    accepted, ignored = [], []
    for observation in observations:
        if (not isinstance(observation, dict) or observation.get('tag') not in focus
                or not isinstance(observation.get('evidence'), str)
                or not observation['evidence'].strip()):
            ignored.append(copy.deepcopy(observation))
        else:
            accepted.append({'tag': observation['tag'], 'evidence': observation['evidence'].strip()})
    matched = [tag for tag in focus if any(item['tag'] == tag for item in accepted)]
    return {'category': strategy['category'], 'matched': matched,
            'missing': [tag for tag in focus if tag not in matched],
            'evidence': accepted, 'ignored': ignored,
            'evidence_status': 'supplied_observations_not_independently_verified',
            'review_required': ['同款确认', '准确原话', '最终整片听看']}


def main(argv=None):
    parser = argparse.ArgumentParser(description='输出女装镜头策略，不读取或识别视频')
    parser.add_argument('--category', default='other')
    parser.add_argument('--style', default='auto')
    args = parser.parse_args(argv)
    # ASCII escapes keep redirected JSON portable across Windows console encodings.
    print(json.dumps(strategy_for(args.category, args.style), ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
