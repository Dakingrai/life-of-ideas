class LoiError(Exception):
    """A user-facing loi failure; commands print str(exc) and exit 1."""


class SchemaError(LoiError):
    """A loi file declares a schema this version does not understand."""
