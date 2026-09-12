"""Small, literal .env loader: no shell expansion, execution, or logging."""
import os
from pathlib import Path

ALLOWED = {"DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL", "OLLAMA_MODEL",
           "OLLAMA_BASE_URL", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
           "DEEPSEEK_INPUT_USD_PER_MILLION", "DEEPSEEK_CACHE_USD_PER_MILLION", "DEEPSEEK_OUTPUT_USD_PER_MILLION"}


ALLOWED.update({"TUNIU_API_KEY", "TUNIU_FLIGHT_URL", "TUNIU_TRAIN_URL", "AMAP_API_KEY"})

def load_env(path=".env"):
    source = Path(path)
    if not source.is_file():
        return
    for line in source.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key in ALLOWED and value:
            os.environ.setdefault(key, value)
