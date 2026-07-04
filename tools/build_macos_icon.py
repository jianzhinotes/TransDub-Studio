#!/usr/bin/env python3

import math
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image


def png_bytes(image: Image.Image, size: int) -> bytes:
    buffer = BytesIO()
    image.resize((size, size), Image.Resampling.LANCZOS).save(
        buffer, "PNG", optimize=True
    )
    return buffer.getvalue()


def build_icns(image: Image.Image, output: Path):
    # Modern ICNS files can embed PNG payloads directly.
    variants = [
        ("icp4", 16),
        ("icp5", 32),
        ("icp6", 64),
        ("ic07", 128),
        ("ic08", 256),
        ("ic09", 512),
        ("ic10", 1024),
        ("ic11", 32),
        ("ic12", 64),
        ("ic13", 256),
        ("ic14", 512),
    ]
    chunks = []
    for icon_type, size in variants:
        payload = png_bytes(image, size)
        chunks.append(icon_type.encode("ascii") + (len(payload) + 8).to_bytes(4, "big") + payload)
    body = b"".join(chunks)
    output.write_bytes(b"icns" + (len(body) + 8).to_bytes(4, "big") + body)


def squircle_alpha(size: int, exponent: float = 4.5, feather: float = 0.012):
    center = (size - 1) / 2
    radius = size / 2
    alpha = Image.new("L", (size, size))
    pixels = alpha.load()
    for y in range(size):
        ny = abs((y - center) / radius)
        for x in range(size):
            nx = abs((x - center) / radius)
            distance = (nx**exponent + ny**exponent) ** (1 / exponent)
            if distance <= 1 - feather:
                value = 255
            elif distance >= 1:
                value = 0
            else:
                value = round(255 * (1 - distance) / feather)
            pixels[x, y] = value
    return alpha


def add_macos_padding(image: Image.Image, scale: float = 0.82) -> Image.Image:
    canvas = Image.new("RGBA", image.size, (0, 0, 0, 0))
    target = round(image.width * scale)
    resized = image.resize((target, target), Image.Resampling.LANCZOS)
    offset = (image.width - target) // 2
    canvas.alpha_composite(resized, (offset, offset))
    return canvas


def main():
    source = Path(sys.argv[1])
    output = Path(sys.argv[2])
    image = Image.open(source).convert("RGBA")
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    image = image.crop((left, top, left + side, top + side))
    image.putalpha(squircle_alpha(side))
    image = image.resize((1024, 1024), Image.Resampling.LANCZOS)
    image = add_macos_padding(image)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, "PNG", optimize=True)
    if len(sys.argv) > 3:
        iconset = Path(sys.argv[3])
        iconset.mkdir(parents=True, exist_ok=True)
        variants = {
            "icon_16x16.png": 16,
            "icon_16x16@2x.png": 32,
            "icon_32x32.png": 32,
            "icon_32x32@2x.png": 64,
            "icon_128x128.png": 128,
            "icon_128x128@2x.png": 256,
            "icon_256x256.png": 256,
            "icon_256x256@2x.png": 512,
            "icon_512x512.png": 512,
            "icon_512x512@2x.png": 1024,
        }
        for name, size in variants.items():
            resized = image.resize((size, size), Image.Resampling.LANCZOS)
            resized.save(iconset / name, "PNG", optimize=True)
    if len(sys.argv) > 4:
        build_icns(image, Path(sys.argv[4]))


if __name__ == "__main__":
    main()
