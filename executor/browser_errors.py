"""Distinguish browser transport failures from site/proxy responses."""


def is_driver_connection_closed(error: object) -> bool:
    message = str(error or "").lower()
    return any(marker in message for marker in (
        "connection closed while reading from the driver",
        "connection closed while writing to the driver",
        "connection closed. most likely the playwright has been closed",
        "driver connection closed",
    ))


def is_browser_target_closed(error: object) -> bool:
    message = str(error or "").lower()
    return is_driver_connection_closed(error) or any(marker in message for marker in (
        "target page, context or browser has been closed",
        "target closed",
        "browser has been closed",
    ))
