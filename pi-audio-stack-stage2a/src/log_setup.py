import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

def configure_logging(cfg):
    Path(cfg.directory).mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("audio_stack")
    log.setLevel(logging.INFO)
    if not log.handlers:
        h = RotatingFileHandler(Path(cfg.directory)/cfg.filename, maxBytes=10_000_000, backupCount=5)
        log.addHandler(h)
    return log
