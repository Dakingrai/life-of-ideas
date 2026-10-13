"""loi — back up the trace a coding agent leaves behind."""


def __getattr__(name: str):
    # PEP 562: importlib.metadata costs ~70 ms; only pay it when someone asks, once.
    if name == "__version__":
        from importlib.metadata import version

        globals()["__version__"] = v = version("loi-light")  # the distribution, not the import package
        return v
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
