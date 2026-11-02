"""Pure helpers for deciding whether a video should be indexed."""


def is_below_minimum_video_size(
    file_size: int | None,
    minimum_size_mb: int,
    enabled: bool = True,
) -> bool:
    """Return True only when a known video size is strictly below the threshold."""
    if not enabled or file_size is None:
        return False
    try:
        size = int(file_size)
        minimum_mb = int(minimum_size_mb)
    except (TypeError, ValueError, OverflowError):
        return False
    if size < 0 or minimum_mb <= 0:
        return False
    return size < minimum_mb * 1024 * 1024



def normalize_minimum_video_size_enabled(value: object) -> bool:
    """Validate and normalize the setting value accepted by the settings API."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ValueError("minimum_video_size_enabled must be a boolean.")


def normalize_minimum_video_size_mb(value: object) -> int:
    """Validate a whole-number threshold in the supported 0–102400 MB range."""
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ValueError("Minimum video size must be a whole number of MB.")
    try:
        size_mb = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Minimum video size must be a whole number of MB.") from None
    if not 0 <= size_mb <= 102400:
        raise ValueError("Minimum video size must be between 0 and 102400 MB.")
    return size_mb



_VIDEO_EXTENSIONS = (
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".flv",
    ".webm", ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp", ".ogv", ".vob",
)


def is_video_file(mime_type: str | None, file_name: str | None) -> bool:
    """Recognize Telegram video documents by MIME type or common file extension."""
    if (mime_type or "").lower().startswith("video/"):
        return True
    return (file_name or "").lower().endswith(_VIDEO_EXTENSIONS)
