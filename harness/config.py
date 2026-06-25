"""Configuration for the Journey-Forge Local distillation harness.

All values come from the environment (the server passes SF_*/JFL_* through when
it spawns the harness), so there is no .env parsing here.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("JFL_DATA_DIR", str(REPO / "data")))

# State lives under the product data dir. HARNESS_ROOT == STATE_DIR so the
# registry's relative skill paths ("skills/<domain>/<cap>/SKILL.md") resolve.
STATE_DIR: Path = DATA_DIR / "harness"
HARNESS_ROOT: Path = STATE_DIR
TRACKS_DIR: Path = DATA_DIR / "traces"

# LLM — defaults to the Anthropic Messages API with the user's own key.
LLM_BASE: str = os.environ.get("SF_LLM_BASE", "https://api.anthropic.com").rstrip("/")
LLM_KEY: str = os.environ.get("SF_LLM_KEY", "")
# Allow self-signed / corporate-MITM LLM endpoints (skip TLS verification).
LLM_INSECURE: bool = os.environ.get("SF_LLM_INSECURE", "").lower() in ("1", "true", "yes")
DISTILL_MODEL: str = os.environ.get("SF_DISTILL_MODEL", "claude-opus-4-8")
# Classification/consolidation are cheap, high-volume calls — default to Haiku.
CLASSIFY_MODEL: str = os.environ.get("SF_CLASSIFY_MODEL", "claude-haiku-4-5")
BUCKET_MODEL: str = os.environ.get("SF_BUCKET_MODEL", CLASSIFY_MODEL)
REASONING_EFFORT: str = os.environ.get("REASONING_EFFORT", "high").lower()

# A capacity bucket distills once it has at least this many segments. The product
# defaults to 1 (a skill appears from a single recording and improves as more
# examples of the same capability accumulate); raise it to require consensus.
MIN_BUCKET_SIZE: int = int(os.environ.get("SF_MIN_BUCKET_SIZE", "1"))
MAX_SEGMENT_EVENTS: int = int(os.environ.get("SF_MAX_SEGMENT_EVENTS", "80"))
PARALLEL: int = int(os.environ.get("SF_PARALLEL", "4"))
DISTILL_MAX_TOKENS: int = int(os.environ.get("DISTILL_MAX_TOKENS", "16384"))
CLASSIFY_MAX_TOKENS: int = int(os.environ.get("CLASSIFY_MAX_TOKENS", "2048"))
LLM_TIMEOUT: float = float(os.environ.get("SF_LLM_TIMEOUT", "180"))
