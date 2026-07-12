from dataclasses import dataclass

@dataclass
class LoggingConfig:
    directory: str = "./logs"
    filename: str = "audio-stack.log"

@dataclass
class AppConfig:
    logging: LoggingConfig

    @classmethod
    def from_yaml(cls, data):
        return cls(logging=LoggingConfig())
