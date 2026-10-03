"""Replaceable LLM provider. Every option is free.

  ollama   - local model on your laptop (default; works offline)
  lmstudio - local model through LM Studio's OpenAI-compatible server
  groq / gemini / openrouter - free cloud tiers, OpenAI-compatible APIs
  rules    - no LLM at all; TOWER uses its rule-based compiler

Only the standard library is used for HTTP, so there is nothing extra to install.
"""
import json
import re
import time
import urllib.error
import urllib.request


class LLMError(Exception):
    pass


def _http_json(url, payload=None, headers=None, timeout=60):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"content-type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        raise LLMError(f"HTTP {e.code}: {body}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise LLMError(f"cannot reach {url}: {e}") from None


def extract_json(text):
    """Pull the first JSON object out of a model reply (handles code fences and think tags)."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    text = text.replace("```json", "```")
    if "```" in text:
        parts = text.split("```")
        if len(parts) >= 3:
            text = parts[1]
    start = text.find("{")
    if start < 0:
        raise LLMError("reply contained no JSON object")
    depth = 0
    in_str = esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError as e:
                    raise LLMError(f"reply was not valid JSON: {e}") from None
    raise LLMError("reply JSON was cut off")


# Small instruction-tuned text models follow JSON schemas best. First match wins.
_PREFERRED = ["qwen2.5:7b", "qwen2.5:3b", "qwen3:4b", "qwen3:8b", "llama3.2", "llama3.1",
              "gemma3", "phi4", "mistral", "qwen3-vl:4b-instruct", "qwen"]


class LLMClient:
    def __init__(self, settings):
        self.s = settings
        self.model = settings.model
        self._status = None
        self._status_t = 0.0

    # -------------------------------------------------------------- status
    def status(self, refresh=False):
        if self._status and not refresh and time.time() - self._status_t < 15:
            return self._status
        st = {"provider": self.s.provider, "kind": self.s.kind, "model": self.model or "",
              "base_url": self.s.base_url, "available": False, "detail": "", "cost": "free"}
        try:
            if self.s.kind == "rules":
                st.update(available=True, model="rule-based", detail="no LLM: keyword compiler")
            elif self.s.kind == "ollama":
                tags = _http_json(f"{self.s.base_url}/api/tags", timeout=3)
                names = [m["name"] for m in tags.get("models", [])]
                if not names:
                    st["detail"] = "Ollama is running but has no models. Run: ollama pull qwen2.5:3b"
                else:
                    if not self.model or self.model not in names:
                        picked = next((n for p in _PREFERRED for n in names if n.startswith(p)), names[0])
                        if self.model and self.model not in names:
                            st["detail"] = f"model '{self.model}' not pulled; using {picked}. "
                        self.model = picked
                    st.update(available=True, model=self.model, detail=st["detail"] + "local, offline")
            else:
                if not self.model:
                    st["detail"] = "set TOWER_LLM_MODEL for this provider"
                elif self.s.base_url.startswith(("http://localhost", "http://127.")):
                    _http_json(f"{self.s.base_url}/models", timeout=3)
                    st.update(available=True, detail="local server")
                elif not self.s.api_key:
                    st["detail"] = "no API key set (see .env.example)"
                else:
                    st.update(available=True, detail="free tier, rate-limited")
                st["model"] = self.model
        except LLMError as e:
            st["detail"] = str(e)
        self._status, self._status_t = st, time.time()
        return st

    @property
    def enabled(self):
        return self.s.kind != "rules" and self.status()["available"]

    # ---------------------------------------------------------------- calls
    def json_chat(self, system, user, schema):
        if self.s.kind == "rules":
            raise LLMError("rules mode")
        if not self.status()["available"]:
            raise LLMError(self.status()["detail"] or "LLM unavailable")
        if self.s.kind == "ollama":
            out = _http_json(f"{self.s.base_url}/api/chat", {
                "model": self.model, "stream": False, "format": schema,
                "options": {"temperature": 0, "num_ctx": 4096},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            }, timeout=self.s.llm_timeout_s)
            return extract_json(out.get("message", {}).get("content", ""))
        return extract_json(self._openai(system, user, json_mode=True))

    def text_chat(self, system, user, max_tokens=200):
        if self.s.kind == "rules" or not self.status()["available"]:
            raise LLMError("LLM unavailable")
        if self.s.kind == "ollama":
            out = _http_json(f"{self.s.base_url}/api/chat", {
                "model": self.model, "stream": False,
                "options": {"temperature": 0.2, "num_predict": max_tokens},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            }, timeout=self.s.llm_timeout_s)
            return re.sub(r"<think>.*?</think>", "", out.get("message", {}).get("content", ""), flags=re.S).strip()
        return self._openai(system, user, json_mode=False, max_tokens=max_tokens).strip()

    def _openai(self, system, user, json_mode, max_tokens=1500):
        headers = {"authorization": f"Bearer {self.s.api_key}"} if self.s.api_key else {}
        body = {"model": self.model, "temperature": 0, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            out = _http_json(f"{self.s.base_url}/chat/completions", body, headers, self.s.llm_timeout_s)
        except LLMError as e:
            if json_mode and "HTTP 400" in str(e):     # some models reject response_format
                body.pop("response_format")
                out = _http_json(f"{self.s.base_url}/chat/completions", body, headers, self.s.llm_timeout_s)
            else:
                raise
        try:
            return out["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"unexpected reply: {str(out)[:200]}") from None
