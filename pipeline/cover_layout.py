"""Letterbox an actual product image and set factual text in a separate panel."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _supported(font, text):
    missing = font.getmask(chr(0x10ffff))
    marker = (missing.size, bytes(missing))
    for char in text:
        if char.isspace():
            continue
        glyph = font.getmask(char)
        if (glyph.size, bytes(glyph)) == marker:
            raise ValueError(f'字体缺少字符：{char}')


def render(source, destination, config, ratio, font_path):
    spec = config['formats'][ratio]
    width, height = spec['size']
    if ratio == '3x4':
        area = (36, 32, width-36, 1090)
        text_area = (72, 1120, width-72, height-48)
    elif ratio == '4x3':
        area = (32, 30, 920, height-30)
        text_area = (960, 80, width-55, height-80)
    else:
        raise ValueError('封面比例仅支持3x4或4x3')
    lines = config['lines']
    if not isinstance(lines, list) or not 1 <= len(lines) <= 3 or any(
            not isinstance(t, str) or not t.strip() or any(ord(c)<32 for c in t) for t in lines):
        raise ValueError('封面需要1至3行有效文字')
    canvas = Image.new('RGB', (width, height), config.get('background', '#17202a'))
    with Image.open(source) as image:
        image = image.convert('RGB')
        aw, ah = area[2]-area[0], area[3]-area[1]
        scale = min(aw/image.width, ah/image.height)
        iw, ih = round(image.width*scale), round(image.height*scale)
        image = image.resize((iw, ih), Image.Resampling.LANCZOS)
        x, y = area[0]+(aw-iw)//2, area[1]+(ah-ih)//2
        canvas.paste(image, (x, y))
    draw = ImageDraw.Draw(canvas)
    text_boxes = []
    for size in range(min(65 if ratio == '3x4' else 67, (text_area[3]-text_area[1])//len(lines)), 23, -1):
        font = ImageFont.truetype(str(font_path), size)
        for line in lines:
            _supported(font, line)
        spacing = round(size*.35)
        total = len(lines)*size + (len(lines)-1)*spacing
        boxes = []
        yy = text_area[1]+(text_area[3]-text_area[1]-total)//2
        for line in lines:
            box = draw.textbbox((text_area[0], yy), line, font=font, anchor='lt')
            boxes.append(box)
            yy += size+spacing
        if boxes[-1][3] <= text_area[3] and all(b[2] <= text_area[2] for b in boxes):
            text_boxes = boxes
            break
    if not text_boxes:
        raise ValueError('封面文字超出独立文字区')
    for line, box in zip(lines, text_boxes):
        draw.text((box[0], box[1]), line, font=font, anchor='lt', fill=config.get('color', '#ffffff'))
    if destination is not None:
        with Path(destination).open('xb') as stream:
            canvas.save(stream, format='JPEG', quality=95, subsampling=0)
    return {'image_box': (x,y,x+iw,y+ih), 'text_area': text_area, 'text_boxes': text_boxes,
            'font_size': size}
