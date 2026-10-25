"""Trim rank artwork and fit every badge to Challenger's proportions.

Developer utility: requires Pillow. The bot only uses the generated PNGs.
"""

import argparse
from io import BytesIO
import json
from pathlib import Path
from urllib.request import Request, urlopen
from zipfile import ZipFile

from PIL import Image, ImageOps


BADGES = {
    'challenger': 1563990056312635454,
    'diamond': 1563989985252741120,
    'gold': 1563989937349459968,
    'iron': 1563989868458016819,
}


def normalize(images):
    cropped, bounds = {}, {}
    for name, image in images.items():
        image = image.convert('RGBA')
        box = image.getchannel('A').getbbox()
        if box is None:
            raise ValueError(f'{name} has no visible artwork')
        bounds[name] = box
        cropped[name] = image.crop(box)
    frame = cropped['challenger'].size
    result = {}
    for name, image in cropped.items():
        fitted = ImageOps.contain(image, frame, Image.Resampling.LANCZOS)
        canvas = Image.new('RGBA', frame)
        canvas.alpha_composite(fitted, ((frame[0] - fitted.width) // 2,
                                       (frame[1] - fitted.height) // 2))
        result[name] = canvas
    return result, bounds


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-archive', type=Path)
    args = parser.parse_args()
    output = Path(__file__).resolve().parents[1] / 'data' / 'rank_badges'
    output.mkdir(parents=True, exist_ok=True)
    images = {}
    for name, emoji_id in BADGES.items():
        if args.source_archive:
            with ZipFile(args.source_archive) as archive:
                content = archive.read(f'{name}.png')
        else:
            request = Request(f'https://cdn.discordapp.com/emojis/{emoji_id}.png?size=128',
                              headers={'User-Agent': 'WorldsBot badge preparation'})
            with urlopen(request, timeout=20) as response:
                content = response.read()
        images[name] = Image.open(BytesIO(content)).convert('RGBA')
    normalized, bounds = normalize(images)
    metadata = {}
    for name, image in normalized.items():
        image.save(output / f'{name}.png', optimize=True)
        metadata[name] = {'source_emoji': BADGES[name], 'source_size': images[name].size,
                          'trimmed_bounds': bounds[name], 'output_size': image.size,
                          'visible_bounds': image.getchannel('A').getbbox()}
    (output / 'manifest.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(metadata))


if __name__ == '__main__':
    main()
