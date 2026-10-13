"""stdlib logging into the project's central `loil.log` (`<loil_home>/projects/<id>/`);
before an id is known, the global `<loil_home>/loil.log`. Never raises into a hook."""

import time

from loil.record.project import Project

_FORMAT = "%(asctime)s %(levelname)s %(module)s: %(message)s"


def get_logger(project: Project) -> "logging.Logger":
    import logging  # lazy: ~2 ms off every hook that has nothing to log
    # Keyed by the resolved log path, not the root: one root can be re-inited under a
    # new id in the same process, and one id can live under two LOI_HOMEs; a logger
    # cached per root (or per id) would keep a FileHandler on the wrong file.
    path = project.log_path if project.id is not None else project.loil_home / "loil.log"
    logger = logging.getLogger(f"loil.{path}")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    if any(isinstance(h, logging.FileHandler) for h in logger.handlers):
        return logger
    if not path.parent.is_dir():
        if not logger.handlers:
            logger.addHandler(logging.NullHandler())  # no directory creation as a side effect
        return logger
    for h in list(logger.handlers):  # the directory appeared since: upgrade the NullHandler
        logger.removeHandler(h)
    try:
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        logger.addHandler(logging.NullHandler())
        return logger
    fmt = logging.Formatter(_FORMAT)
    fmt.converter = time.gmtime
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    return logger
