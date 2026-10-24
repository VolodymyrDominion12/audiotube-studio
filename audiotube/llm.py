"""Мінімальний клієнт до LLM без зайвих залежностей (stdlib urllib).

Провайдери: Ollama (локально), OpenAI-сумісний API, Anthropic.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Protocol

from audiotube.config import LLMCfg

RETRIABLE_STATUS = {408, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    name: str

    def complete(self, messages: list[dict[str, str]], json_mode: bool = True) -> str: ...


def _http_json(
    url: str, payload: dict, headers: dict[str, str], timeout: int, retries: int, what: str
) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json", **headers}, method="POST"
    )
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:400]
            e.close()
            last_error = LLMError(f"{what}: HTTP {e.code} — {detail}")
            if e.code not in RETRIABLE_STATUS or attempt == retries:
                raise last_error from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_error = LLMError(f"{what}: немає з'єднання — {e}")
            if attempt == retries:
                raise last_error from e
        time.sleep(min(2 ** (attempt + 1), 20))
    raise last_error or LLMError(what)  # pragma: no cover


def _read_env(name: str) -> str:
    return os.environ.get(name, "").strip()


class OllamaClient:
    def __init__(self, cfg: LLMCfg):
        self.cfg = cfg
        self.name = "ollama"
        self.base = (cfg.base_url or "http://127.0.0.1:11434").rstrip("/")

    def complete(self, messages: list[dict[str, str]], json_mode: bool = True) -> str:
        payload: dict = {
            "model": self.cfg.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": self.cfg.temperature, "num_predict": self.cfg.max_tokens},
        }
        if json_mode:
            payload["format"] = "json"
        data = _http_json(
            f"{self.base}/api/chat", payload, {}, self.cfg.timeout, self.cfg.retries, "Ollama"
        )
        content = (data.get("message") or {}).get("content")
        if not content:
            raise LLMError(f"Ollama повернув порожню відповідь: {str(data)[:300]}")
        return content

    def ping(self) -> tuple[bool, str]:
        try:
            with urllib.request.urlopen(f"{self.base}/api/tags", timeout=5) as resp:
                models = [m.get("name", "") for m in json.loads(resp.read().decode()).get("models", [])]
        except Exception as e:  # noqa: BLE001 — діагностика не повинна падати
            return False, f"{self.base} недоступний ({e}); запустіть `ollama serve`"
        if not models:
            return False, f"Ollama працює, але моделей немає; виконайте `ollama pull {self.cfg.model}`"
        short = ", ".join(m for m in models if m)[:120]
        ok = any(self.cfg.model.split(":")[0] in m for m in models)
        return ok, f"моделі: {short}" + ("" if ok else f" (немає {self.cfg.model})")


class OpenAIClient:
    def __init__(self, cfg: LLMCfg):
        self.cfg = cfg
        self.name = "openai"
        self.base = (cfg.base_url or "https://api.openai.com/v1").rstrip("/")
        self.key_env = cfg.api_key_env or "OPENAI_API_KEY"
        self.key = _read_env(self.key_env)

    def complete(self, messages: list[dict[str, str]], json_mode: bool = True) -> str:
        if not self.key and not self.cfg.base_url:
            raise LLMError(f"не задано {self.key_env} для OpenAI (або llm.base_url для локального сервера)")
        payload: dict = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.key or 'local'}"}
        data = _http_json(
            f"{self.base}/chat/completions", payload, headers, self.cfg.timeout, self.cfg.retries, "LLM"
        )
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"неочікувана відповідь LLM: {str(data)[:300]}") from e


class AnthropicClient:
    def __init__(self, cfg: LLMCfg):
        self.cfg = cfg
        self.name = "anthropic"
        self.base = (cfg.base_url or "https://api.anthropic.com/v1").rstrip("/")
        self.key_env = cfg.api_key_env or "ANTHROPIC_API_KEY"
        self.key = _read_env(self.key_env)

    def complete(self, messages: list[dict[str, str]], json_mode: bool = True) -> str:
        if not self.key:
            raise LLMError(f"не задано {self.key_env} для Anthropic")
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        dialogue = [m for m in messages if m["role"] != "system"]
        payload: dict = {
            "model": self.cfg.model,
            "max_tokens": self.cfg.max_tokens,
            "temperature": self.cfg.temperature,
            "messages": dialogue,
        }
        if system:
            payload["system"] = system
        if not json_mode:
            payload.pop("temperature", None)
        headers = {"x-api-key": self.key, "anthropic-version": "2023-06-01"}
        data = _http_json(
            f"{self.base}/messages", payload, headers, self.cfg.timeout, self.cfg.retries, "Anthropic"
        )
        try:
            return "".join(block.get("text", "") for block in data["content"])
        except (KeyError, TypeError) as e:
            raise LLMError(f"неочікувана відповідь Anthropic: {str(data)[:300]}") from e


def build_client(cfg: LLMCfg) -> LLMClient:
    if cfg.provider == "none":
        raise LLMError("LLM вимкнено (llm.provider: none)")
    if cfg.provider == "ollama":
        return OllamaClient(cfg)
    if cfg.provider == "openai":
        return OpenAIClient(cfg)
    if cfg.provider == "anthropic":
        return AnthropicClient(cfg)
    raise LLMError(f"невідомий провайдер LLM: {cfg.provider}")


def provider_report(cfg: LLMCfg) -> tuple[bool, str]:
    """Стан LLM для `audiotube doctor` (без платних запитів)."""
    if cfg.provider == "none":
        return False, "вимкнено (llm.provider: none)"
    if cfg.provider == "ollama":
        return OllamaClient(cfg).ping()
    if cfg.provider == "openai":
        client = OpenAIClient(cfg)
        if client.key:
            return True, f"ключ {client.key_env} задано"
        if cfg.base_url:
            return True, f"локальний/сумісний сервер: {cfg.base_url} (без ключа)"
        return False, f"не задано {client.key_env} (або llm.base_url)"
    client = AnthropicClient(cfg)
    return (True, f"ключ {client.key_env} задано") if client.key else (False, f"не задано {client.key_env}")
