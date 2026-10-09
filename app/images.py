"""Bounded, request-scoped image decoding; raw media never goes into chat storage."""

import io
import warnings
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from app.domain import ImageInput

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000


def load_image(path: Path) -> ImageInput:
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_IMAGE_BYTES + 1)
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ValueError("Please send an image smaller than 8 MiB.")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"JPEG", "PNG", "WEBP"}:
                    raise ValueError("Please send a JPEG, PNG or static WebP image.")
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ValueError("Please send a smaller image or a crop (under 20 megapixels).")
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError("Please send a still image rather than an animation.")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                oriented = ImageOps.exif_transpose(image).convert("RGB")
                # Copy pixels to strip EXIF, comments, and other embedded metadata.
                clean = Image.frombytes("RGB", oriented.size, oriented.tobytes())
                output = io.BytesIO()
                clean.save(output, format="PNG")
            normalized = output.getvalue()
            if len(normalized) > MAX_IMAGE_BYTES:
                raise ValueError("This image is too large to process. Please send a smaller crop.")
            return ImageInput(normalized, "image/png")
    except (
        FileNotFoundError,
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise ValueError(
            "I could not read that image. Please resend it as a clear photo."
        ) from error
    finally:
        path.unlink(missing_ok=True)
