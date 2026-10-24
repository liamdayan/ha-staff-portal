"""Logo handling and home-screen icon generation."""

import io
import os

from PIL import Image, ImageDraw, ImageFont, ImageOps

MAX_LOGO_PX = 1024
# Refuse huge images before decoding them: a small compressed file can expand to gigabytes.
MAX_SOURCE_PIXELS = 25_000_000
# Formats a logo may be uploaded in. GIF isn't one: logos are stored as PNG anyway, and GIF
# decoding has had denial-of-service bugs.
LOGO_FORMATS = ["PNG", "JPEG", "WEBP"]


def hex_to_rgb(value: str, fallback=(255, 255, 255)):
    value = (value or "").lstrip("#")
    if len(value) != 6:
        return fallback
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return fallback


def initials(name: str) -> str:
    words = [w for w in (name or "").split() if w[:1].isalnum()]
    return "".join(w[0] for w in words[:2]).upper() or "S"


class Branding:
    def __init__(self, data_dir: str):
        self.logo_path = os.path.join(data_dir, "logo.png")
        self._cache: dict[tuple, bytes] = {}

    def invalidate(self):
        self._cache.clear()

    def has_logo(self) -> bool:
        return os.path.exists(self.logo_path)

    @staticmethod
    def clean_logo(raw: bytes) -> bytes:
        """Re-encode an image as PNG so only clean image data is ever stored and served back."""
        img = Image.open(io.BytesIO(raw), formats=LOGO_FORMATS)
        if img.width * img.height > MAX_SOURCE_PIXELS:
            raise ValueError("image too large")
        img.draft("RGB", (MAX_LOGO_PX, MAX_LOGO_PX))  # JPEG: decode at reduced size
        img = ImageOps.exif_transpose(img).convert("RGBA")
        img.thumbnail((MAX_LOGO_PX, MAX_LOGO_PX))
        out = io.BytesIO()
        img.save(out, "PNG")
        return out.getvalue()

    def save_logo(self, raw: bytes) -> None:
        self.write_logo(self.clean_logo(raw))

    def write_logo(self, png: bytes) -> None:
        """Store a logo that has already been through clean_logo."""
        with open(self.logo_path, "wb") as f:
            f.write(png)
        self.invalidate()

    def remove_logo(self) -> None:
        if self.has_logo():
            os.remove(self.logo_path)
        self.invalidate()

    def logo_png(self) -> bytes | None:
        if not self.has_logo():
            return None
        with open(self.logo_path, "rb") as f:
            return f.read()

    def icon(self, size: int, settings: dict) -> bytes:
        """Square, opaque icon (iOS turns transparency black on the home screen)."""
        key = (size, settings.get("icon_background"), settings.get("accent_color"),
               settings.get("property_name"), self.has_logo())
        if key in self._cache:
            return self._cache[key]

        if self.has_logo():
            bg = hex_to_rgb(settings.get("icon_background"))
            canvas = Image.new("RGB", (size, size), bg)
            logo = Image.open(self.logo_path).convert("RGBA")
            inner = int(size * 0.78)
            logo.thumbnail((inner, inner), Image.LANCZOS)
            canvas.paste(logo, ((size - logo.width) // 2, (size - logo.height) // 2), logo)
        else:
            bg = hex_to_rgb(settings.get("accent_color"), (47, 111, 79))
            canvas = Image.new("RGB", (size, size), bg)
            draw = ImageDraw.Draw(canvas)
            text = initials(settings.get("property_name"))
            font = ImageFont.load_default(size=int(size * 0.42))
            box = draw.textbbox((0, 0), text, font=font)
            w, h = box[2] - box[0], box[3] - box[1]
            draw.text(((size - w) / 2 - box[0], (size - h) / 2 - box[1]), text,
                      fill=(255, 255, 255), font=font)

        buf = io.BytesIO()
        canvas.save(buf, "PNG")
        self._cache[key] = buf.getvalue()
        return self._cache[key]
