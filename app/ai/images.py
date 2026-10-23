"""An uploaded image, made safe and small, in memory only.

``prepare_image`` takes the bytes of an upload and returns a clean JPEG:

1. size: at most ``MAX_IMAGE_BYTES`` (8 MB);
2. the real format is read from the bytes (never the file name or the declared type): JPEG, PNG or
   WebP. HEIC (iPhone "high efficiency") is recognised and refused with a message that says what
   to do; nothing else is ever handed to a decoder (``Image.open(..., formats=...)``);
3. the pixel count is read from the header and refused BEFORE any pixel is decoded: over
   ``MAX_PIXELS`` (16 MP) for PNG and WebP, which decode at full size (a mostly blank 36 MP PNG is
   a few KB and over 100 MB decoded); over ``MAX_JPEG_PIXELS`` (36 MP) for JPEG, which is decoded
   at reduced size (``draft``). ``Image.MAX_IMAGE_PIXELS`` is set to the larger bound as a second
   guard against a decompression bomb;
4. the pixels are decoded (one full-size copy at most), shrunk at once by an integer factor
   toward ``MAX_EDGE``, and only then turned the right way up from the EXIF orientation (a phone
   photo is often rotated by a flag) and flattened on white if there is transparency: every later
   copy is small. The metadata is dropped (EXIF, GPS, ICC profile, XMP, comments);
5. downscaled so the long edge is at most ``MAX_EDGE`` (1568 px, the size the model reads best;
   larger images are scaled by the API anyway and only cost upload time), and re-encoded as JPEG.
   At most two images are decoded at the same time in the process (``_DECODE_SLOTS``).

Everything is in memory: no temporary file, and the caller drops the bytes after the call.

WHY NOT A SUBPROCESS (as for the Excel parse): the Excel sandbox exists for zip and XML bombs in a
format the application parses itself. Here the decoder is Pillow's, on three allow-listed formats,
with the size and the pixel count bounded before decoding, the caller authenticated, and the
volume rate-limited. A child process would add about a second of start-up and a second copy of the
pixels for each image. The remaining risk is a memory-safety bug in a native decoder (libjpeg,
libpng, libwebp), which a subprocess with an address-space cap would contain; it is accepted here
and listed in the design note, to be revisited if the feature opens to less trusted users.
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 16_000_000  # PNG / WebP (decoded at full size): 4000 x 4000
MAX_JPEG_PIXELS = 36_000_000  # 6000 x 6000; a 48 MP phone photo is refused with a clear message
MAX_EDGE = 1568
JPEG_QUALITY = 85
ALLOWED_FORMATS = ("JPEG", "PNG", "WEBP")
OUTPUT_MEDIA_TYPE = "image/jpeg"

Image.MAX_IMAGE_PIXELS = MAX_JPEG_PIXELS  # PIL raises DecompressionBombError at twice this
# At most two decodes at once in the process: each can hold tens of MB of pixels.
_DECODE_SLOTS = threading.BoundedSemaphore(2)
# EXIF orientation -> the transpose that shows the image upright (what ``exif_transpose`` does)
_UPRIGHT = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}
_ORIENTATION_TAG = 0x0112

_HEIC_BRANDS = (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif")


class ImageRejected(Exception):
    """The image cannot be used; ``str(exc)`` is a French sentence (no "Image n :" prefix)."""


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    media_type: str
    width: int
    height: int


def _is_heic(head: bytes) -> bool:
    return len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in _HEIC_BRANDS


def prepare_image(raw: bytes) -> PreparedImage:
    if not raw:
        raise ImageRejected("le fichier est vide.")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ImageRejected(f"le fichier dépasse {MAX_IMAGE_BYTES // (1024 * 1024)} Mo.")
    if _is_heic(raw[:16]):
        raise ImageRejected(
            "le format HEIC n'est pas pris en charge : envoyez une photo JPEG "
            "(réglage « Le plus compatible » de l'appareil photo, ou une capture d'écran)."
        )
    try:
        with _DECODE_SLOTS:
            return _prepare(raw)
    except ImageRejected:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):  # Pillow's own guard
        raise ImageRejected("les dimensions de l'image sont trop grandes.") from None
    except UnidentifiedImageError:
        raise ImageRejected(
            "le format n'est pas pris en charge (JPEG, PNG ou WebP) ou l'image est abîmée."
        ) from None
    except Exception:  # noqa: BLE001 - whatever a codec does, it is a refused image, not a 500
        raise ImageRejected("l'image est illisible ou abîmée.") from None


def _prepare(raw: bytes) -> PreparedImage:
    with Image.open(io.BytesIO(raw), formats=list(ALLOWED_FORMATS)) as source:
        width, height = source.size
        is_jpeg = source.format == "JPEG"
        limit = MAX_JPEG_PIXELS if is_jpeg else MAX_PIXELS
        if width < 1 or height < 1 or width * height > limit:
            raise ImageRejected("les dimensions de l'image sont trop grandes.")
        orientation = source.getexif().get(_ORIENTATION_TAG)
        if is_jpeg:  # decode at reduced size when far larger than needed
            source.draft("RGB", (MAX_EDGE, MAX_EDGE))
        source.load()  # the one full-size copy
        small = _shrink(source)
    upright = small.transpose(_UPRIGHT[orientation]) if orientation in _UPRIGHT else small
    clean = _flatten(upright)
    clean.info = {}  # no EXIF, ICC profile, XMP or comment can follow into the output
    clean.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    clean.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return PreparedImage(out.getvalue(), OUTPUT_MEDIA_TYPE, clean.width, clean.height)


def _shrink(image: Image.Image) -> Image.Image:
    """The image reduced by a whole factor so its long edge stays at or above ``MAX_EDGE`` (the
    final resize is then cheap and sharp). The original is closed as soon as the copy exists."""
    factor = max(image.size) // MAX_EDGE
    if factor < 2:
        return image.copy()
    if image.mode not in ("L", "LA", "RGB", "RGBA"):  # palette, bilevel, 16-bit, CMYK: reduce
        transparent = image.mode == "PA" or "transparency" in image.info
        image = image.convert("RGBA" if transparent else "RGB")
    reduced = image.reduce(factor)
    image.close()
    return reduced


def _flatten(image: Image.Image) -> Image.Image:
    """The image as RGB; transparency is laid on white (a transparent screenshot would go black)."""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        base = Image.new("RGB", rgba.size, (255, 255, 255))
        base.paste(rgba, mask=rgba.getchannel("A"))
        return base
    return image.convert("RGB")
