class LoilError(Exception):
    """A user-facing loil failure; commands print str(exc) and exit 1."""


class SchemaError(LoilError):
    """A loil file declares a schema this version does not understand."""
