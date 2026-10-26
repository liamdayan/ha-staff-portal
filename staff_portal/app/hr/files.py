"""Files people add to the HR portal (documents, card photos), made safe before anything keeps
or shows them.

- The type comes from the file's first bytes, never its name: JPEG, PNG, WebP or PDF only.
  Apple's HEIC photos are refused with a plain explanation (retake the photo with the camera).
- Images are re-encoded: turned upright, shrunk to at most 3,000 pixels on the long side, and
  saved fresh, so location and every other piece of metadata is gone and a booby-trapped file
  can't come through as it was.
- PDFs are kept byte for byte, and only ever offered as downloads.
- Every file is served with a sandbox policy, nosniff and no caching (file_response).
- Image work runs in worker threads, a couple at a time, so it never stalls doors or wristbands.
"""

import asyncio
import io
import re

from aiohttp import web

PHOTO_LIMIT = 15 * 1024 * 1024        # photos and employees' uploads
DOCUMENT_LIMIT = 25 * 1024 * 1024     # HR's documents (scanned paperwork can be larger)
MAX_SIDE = 3000
# Pictures bigger than these are refused rather than decoded. A JPEG (every phone photo) is
# decoded at a half, quarter or eighth of its size (draft mode), so even a 200-megapixel photo
# needs about 100 MB at most; PNG and WebP can't be, so their limit is lower (also about 100 MB).
MAX_PIXELS = 200_000_000
MAX_FULL_PIXELS = 25_000_000
IMAGE_WORKERS = 2
MIME = {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "pdf": "application/pdf"}
EXTENSION = {"jpeg": "jpg", "png": "png", "webp": "webp", "pdf": "pdf"}
HEIC = ("That photo is in Apple's HEIC format, which the HR portal can't check. Take the photo "
        "again with the camera button, or change your iPhone's camera to “Most Compatible”.")
NOT_ALLOWED = "Only photos (JPEG, PNG or WebP) and PDF files can be added."
UNREADABLE = "That picture couldn't be read. Take the photo again, or choose another file."
TOO_LARGE = ("That picture is too large. Take the photo again at a lower resolution (for example "
             "12 megapixels), or choose a smaller file.")


class FileRefused(ValueError):
    pass


def sniff(data: bytes) -> str | None:
    """"jpeg", "png", "webp" or "pdf" from the first bytes; None for anything else."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:5] == b"%PDF-":
        return "pdf"
    return None


def is_heic(data: bytes) -> bool:
    return data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc", b"hevx", b"mif1",
                                                    b"msf1", b"heim", b"heis", b"avif")


def check_type(data: bytes, limit: int = PHOTO_LIMIT) -> str:
    """The file's kind, or FileRefused with a message for the person."""
    if len(data) > limit:
        raise FileRefused(f"That file is too big (more than {limit // 2**20} MB).")
    kind = sniff(data)
    if kind is None:
        raise FileRefused(HEIC if is_heic(data) else NOT_ALLOWED)
    return kind


def clean_image(data: bytes, max_side: int = MAX_SIDE) -> tuple[bytes, str]:
    """A fresh copy of a photo with no metadata, upright and at most max_side pixels: (bytes,
    MIME type). JPEG stays JPEG; PNG and WebP become PNG (pictures with transparency) or JPEG.
    Raises FileRefused. Slow: use clean_image_async."""
    from PIL import Image, ImageOps   # only loaded when a picture comes in
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.format not in ("JPEG", "PNG", "WEBP"):
                raise FileRefused(NOT_ALLOWED)
            limit = MAX_PIXELS if img.format == "JPEG" else MAX_FULL_PIXELS
            if img.width * img.height > limit:
                raise FileRefused(TOO_LARGE)
            # Shrink first (a JPEG is decoded at a fraction of its size), then turn it upright,
            # so the full-size picture is never held in memory, let alone copied.
            scale = min(1, max_side / max(img.size))
            img.draft(None, (round(img.width * scale), round(img.height * scale)))
            img.thumbnail((max_side, max_side))
            img = ImageOps.exif_transpose(img)
            alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
            out = io.BytesIO()
            if alpha:
                img.convert("RGBA").save(out, "PNG", optimize=True)
                return out.getvalue(), MIME["png"]
            img.convert("RGB").save(out, "JPEG", quality=88, optimize=True)
            return out.getvalue(), MIME["jpeg"]
    except FileRefused:
        raise
    except Image.DecompressionBombError:   # far over the limit: Pillow won't even open it
        raise FileRefused(TOO_LARGE) from None
    except Exception:  # noqa: BLE001 - a broken or hostile file is just refused
        raise FileRefused(UNREADABLE) from None


_slots: dict = {}


def image_slots() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if loop not in _slots:
        _slots.clear()
        _slots[loop] = asyncio.Semaphore(IMAGE_WORKERS)
    return _slots[loop]


async def clean_image_async(data: bytes, max_side: int = MAX_SIDE) -> tuple[bytes, str]:
    async with image_slots():
        return await asyncio.to_thread(clean_image, data, max_side)


async def clean_upload(data: bytes, limit: int = PHOTO_LIMIT) -> tuple[bytes, str]:
    """An upload made safe: (bytes, MIME type). Images re-encoded, PDFs as they are."""
    kind = check_type(data, limit)
    if kind == "pdf":
        return data, MIME["pdf"]
    return await clean_image_async(data)


def safe_filename(name: str, kind: str) -> str:
    """A download name with only plain characters, and the extension its real type has."""
    base = re.sub(r"[^A-Za-z0-9 ._()-]+", "", str(name or "")).strip(" .")[:80] or "file"
    base = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", base) or "file"
    return f"{base}.{EXTENSION.get(kind, 'bin')}"


def file_response(data: bytes, mime: str, name: str) -> web.Response:
    """Serve a file from the HR portal: sandboxed, never sniffed, never cached. PDFs (and anything
    that isn't a picture) are always downloads, never opened inside the portal."""
    kind = next((k for k, m in MIME.items() if m == mime), None)
    inline = kind in ("jpeg", "png", "webp")
    filename = safe_filename(name, kind or "bin")
    return web.Response(body=data, content_type=mime if kind else "application/octet-stream",
                        headers={
                            "Content-Disposition": f'{"inline" if inline else "attachment"}; '
                                                   f'filename="{filename}"',
                            "Content-Security-Policy": "sandbox; default-src 'none'; "
                                                       "img-src 'self' data:; style-src 'unsafe-inline'",
                            "X-Content-Type-Options": "nosniff",
                            "Cache-Control": "no-store",
                            "Cross-Origin-Resource-Policy": "same-origin"})
