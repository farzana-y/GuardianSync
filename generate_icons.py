# Run once: python generate_icons.py
from PIL import Image, ImageDraw
import os

os.makedirs("static", exist_ok=True)

for size in [192, 512]:
    img = Image.new('RGB', (size, size), '#dc2626')  # Red background
    draw = ImageDraw.Draw(img)
    # Draw a simple shield shape (white circle in center)
    margin = size // 4
    draw.ellipse([margin, margin, size-margin, size-margin], fill='white')
    img.save(f'static/icon-{size}.png')
    print(f'Created icon-{size}.png')