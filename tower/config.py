"""Runtime settings, read from environment variables or a .env file.

Every option has a free default: the LLM is a local Ollama model, and if Ollama
is not running TOWER falls back to its rule-based compiler.
"""
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv():
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

# Free LLM presets. Every one of these costs nothing to use: Ollama and LM Studio
# run on your laptop; Groq, Gemini and OpenRouter have free tiers with rate
# limits. Model names on cloud free tiers change often: if one stops working,
# pick a current one from the provider's model list and set TOWER_LLM_MODEL.
PRESETS = {
    "ollama":     {"kind": "ollama", "base_url": "http://localhost:11434", "model": "", "key_env": ""},
    "lmstudio":   {"kind": "openai", "base_url": "http://localhost:1234/v1", "model": "", "key_env": ""},
    "groq":       {"kind": "openai", "base_url": "https://api.groq.com/openai/v1",
                   "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY"},
    "gemini":     {"kind": "openai", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                   "model": "gemini-2.5-flash", "key_env": "GEMINI_API_KEY"},
    "openrouter": {"kind": "openai", "base_url": "https://openrouter.ai/api/v1",
                   "model": "", "key_env": "OPENROUTER_API_KEY"},
    "rules":      {"kind": "rules", "base_url": "", "model": "", "key_env": ""},
}


@dataclass
class Settings:
    provider: str
    kind: str
    base_url: str
    model: str
    api_key: str
    llm_explain: bool
    llm_timeout_s: float
    scenario: str
    host: str
    port: int
    ask_timeout_s: float
    seed: int
    access_key: str


def load_settings() -> Settings:
    provider = os.environ.get("TOWER_LLM_PROVIDER", "ollama").strip().lower()
    preset = PRESETS.get(provider, PRESETS["ollama"])
    key_env = preset["key_env"]
    return Settings(
        provider=provider if provider in PRESETS else "ollama",
        kind=preset["kind"],
        base_url=os.environ.get("TOWER_LLM_BASE_URL", preset["base_url"]).rstrip("/"),
        model=os.environ.get("TOWER_LLM_MODEL", preset["model"]),
        api_key=os.environ.get("TOWER_LLM_API_KEY", os.environ.get(key_env, "") if key_env else ""),
        llm_explain=os.environ.get("TOWER_LLM_EXPLAIN", "0") == "1",
        llm_timeout_s=float(os.environ.get("TOWER_LLM_TIMEOUT", "120")),
        scenario=os.environ.get("TOWER_SCENARIO", str(ROOT / "scenarios" / "landslide_v1.json")),
        # cloud hosts (Render, Railway, Koyeb...) pass PORT and need the server on all interfaces
        host=os.environ.get("TOWER_HOST", "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"),
        port=int(os.environ.get("TOWER_PORT") or os.environ.get("PORT") or "8000"),
        ask_timeout_s=float(os.environ.get("TOWER_ASK_TIMEOUT", "60")),
        seed=int(os.environ.get("TOWER_SEED", "7")),
        access_key=os.environ.get("TOWER_ACCESS_KEY", ""),
    )
