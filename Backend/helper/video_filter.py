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
