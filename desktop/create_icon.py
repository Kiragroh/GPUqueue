"""Deterministic original program icon, CPU-only; no downloaded artwork."""
from pathlib import Path
from PIL import Image, ImageDraw

image = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((8, 8, 248, 248), radius=52, fill=(31, 44, 61, 255))
draw.rounded_rectangle((65, 58, 191, 194), radius=16, outline='white', width=12)
for y in (93, 126, 159):
    draw.rounded_rectangle((90, y, 166, y+10), radius=4, fill='white')
for y in (83, 126, 169):
    draw.line((44, y, 61, y), fill='white', width=9)
    draw.line((196, y, 213, y), fill='white', width=9)
image.save(Path(__file__).with_name('GPUqueue.ico'), sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
