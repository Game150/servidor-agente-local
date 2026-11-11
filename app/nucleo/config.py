import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]

MODELS_DIR = Path(
    os.getenv(
        "MODELS_DIR",
        r"\\192.168.10.254\Home-Server\LLMs"
    )
)

AGENTS_DIR = BASE_DIR / "config" / "agentes"
