def safe_int(value: str, default=None):
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def safe_float(value: str, default=None):
    try:
        return float(value)
    except (ValueError, TypeError):
        return default