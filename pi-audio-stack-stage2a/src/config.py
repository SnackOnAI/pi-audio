import os, re, yaml
from pathlib import Path
from .models import AppConfig

_env = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

def load_config(path: Path):
    text = path.read_text()
    text = _env.sub(lambda m: os.environ[m.group(1)], text)
    return AppConfig.from_yaml(yaml.safe_load(text))
