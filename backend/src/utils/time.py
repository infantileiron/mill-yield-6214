from datetime import datetime, timezone
def utcnow():
    return datetime.now(timezone.utc)
def aware(value):
    """Normalise datetimes read back from databases that may drop tzinfo."""
    if value is None:return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
