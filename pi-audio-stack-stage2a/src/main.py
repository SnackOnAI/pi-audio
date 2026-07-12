from pathlib import Path
from .config import load_config
from .log_setup import configure_logging

def main():
    cfg = load_config(Path("config.yaml"))
    log = configure_logging(cfg.logging)
    log.info("Stage 2A foundation loaded")

if __name__ == "__main__":
    main()
