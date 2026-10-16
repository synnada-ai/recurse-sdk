"""Minimal private compute-only example."""


def echo(message: str) -> dict[str, str]:
    """Return a message.

    Args:
        message: Message to return.

    Returns:
        The supplied message.
    """
    return {"message": message}
