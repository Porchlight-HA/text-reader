"""Constants for the Local OCR integration."""

from pathlib import Path

DOMAIN = "local_ocr"
MIN_API = 1
DEFAULT_PORT = 8738
SAMPLE_PATH = Path(__file__).parent / "samples" / "envelope.jpg"

SIGNAL_DURATION_UPDATED = f"{DOMAIN}_duration_updated"
