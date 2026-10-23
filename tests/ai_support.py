"""Helpers for the AI tests: synthetic images (made in memory, never read from intake/) and the PNR
fixture texts a fake client answers with."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

FIXTURES = Path(__file__).parent / "fixtures" / "pnr"
GPS_MARK = b"GPS-SECRET-LOCATION"


def pnr(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text(encoding="utf-8")


def layover_pages() -> tuple[str, str]:
    """The two screens of the layover fixture, one for each image."""
    first, second = pnr("layover_two_pages").split("--- TST", 2)[1:]
    return "--- TST" + first, "--- TST" + second


def png(size: tuple[int, int] = (320, 200), color: tuple[int, int, int] = (20, 30, 40)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


def jpeg_with_exif(size: tuple[int, int] = (320, 200), orientation: int = 1) -> bytes:
    """A JPEG whose EXIF carries an orientation, a camera model and a GPS-like marker."""
    image = Image.new("RGB", size, (200, 10, 10))
    exif = Image.Exif()
    exif[0x0112] = orientation  # orientation
    exif[0x0110] = "SecretCamera 9000"  # model
    exif[0x010E] = GPS_MARK.decode()  # image description
    out = io.BytesIO()
    image.save(out, format="JPEG", exif=exif, comment=b"private comment")
    return out.getvalue()


def webp(size: tuple[int, int] = (64, 64)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (1, 2, 3)).save(out, format="WEBP")
    return out.getvalue()
