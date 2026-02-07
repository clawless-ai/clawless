"""LLM Router — flexible abstraction for routing requests to any LLM provider.

Supports any provider that exposes an OpenAI-compatible chat completions API
(which includes OpenAI, Anthropic via proxy, Ollama, llama.cpp, vLLM, etc.).

Custom providers can be added by subclassing LLMProvider and registering them.

The router tries endpoints in priority order and falls back on failure.
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from clawless.user.config import LLMEndpoint

logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    """Standardized response from any LLM provider."""

    content: str
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    provider_name: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class LLMProvider(ABC):
    """Abstract base class for LLM providers.

    Subclass this to add support for non-OpenAI-compatible APIs.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier."""

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, str]],
        max_tokens: int = 1024,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> LLMResponse:
        """Send a chat completion request and return the response."""


class OpenAICompatibleProvider(LLMProvider):
    """Provider for any OpenAI-compatible chat completions API.

    Works with: OpenAI, Ollama (/v1), llama.cpp server, vLLM,
    LocalAI, LM Studio, text-generation-webui, and many more.
    """

    def __init__(self, endpoint: LLMEndpoint) -> None:
        self._endpoint = endpoint
        self._client = httpx.Client(timeout=endpoint.timeout)

    @property
    def name(self) -> str:
        return self._endpoint.name

    def chat(
        self,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> LLMResponse:
        url = f"{self._endpoint.base_url.rstrip('/')}/chat/completions"
        api_key = self._resolve_api_key()

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload: dict[str, Any] = {
            "model": self._endpoint.model,
            "messages": messages,
            "max_tokens": max_tokens or self._endpoint.max_tokens,
            "temperature": temperature,
        }
        payload.update(kwargs)

        response = self._client.post(url, json=payload, headers=headers)
        if not response.is_success:
            try:
                err_body = response.json()
            except Exception:
                err_body = response.text
            raise RuntimeError(f"{response.status_code} from {url}: {err_body}")
        data = response.json()

        content = ""
        tool_calls = []
        if "choices" in data and data["choices"]:
            choice = data["choices"][0]
            msg = choice.get("message", {})
            content = msg.get("content") or ""
            tool_calls = msg.get("tool_calls", [])

        return LLMResponse(
            content=content,
            model=data.get("model", self._endpoint.model),
            usage=data.get("usage", {}),
            raw=data,
            provider_name=self.name,
            tool_calls=tool_calls,
        )

    def _resolve_api_key(self) -> str:
        """Resolve API key, supporting ${ENV_VAR} syntax."""
        key = self._endpoint.api_key
        if key.startswith("${") and key.endswith("}"):
            env_name = key[2:-1]
            return os.environ.get(env_name, "")
        return key

    def close(self) -> None:
        self._client.close()


class AnthropicProvider(LLMProvider):
    """Provider for the Anthropic Messages API (Claude models).

    Uses the native Anthropic API format:
    - Auth via x-api-key header
    - POST to /v1/messages
    - System prompt in top-level 'system' field
    - Response content is an array of content blocks
    """

    API_VERSION = "2023-06-01"

    def __init__(self, endpoint: LLMEndpoint) -> None:
        self._endpoint = endpoint
        self._client = httpx.Client(timeout=endpoint.timeout)

    @property
    def name(self) -> str:
        return self._endpoint.name

    def chat(
        self,
        messages: list[dict[str, str]],
        max_tokens: int | None = None,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> LLMResponse:
        url = f"{self._endpoint.base_url.rstrip('/')}/v1/messages"
        api_key = self._resolve_api_key()

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": self.API_VERSION,
        }

        # Anthropic: system message goes in a separate top-level field
        system_text = ""
        chat_messages = []
        for msg in messages:
            if msg.get("role") == "system":
                system_text += msg.get("content", "") + "\n"
            elif msg.get("role") == "tool":
                # Translate OpenAI tool-result message to Anthropic format
                chat_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": msg.get("content", ""),
                    }],
                })
            elif msg.get("role") == "assistant" and msg.get("tool_calls"):
                # Translate assistant message with tool calls to Anthropic content blocks
                blocks: list[dict[str, Any]] = []
                if msg.get("content"):
                    blocks.append({"type": "text", "text": msg["content"]})
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    args = func.get("arguments", "{}")
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": func.get("name", ""),
                        "input": json.loads(args) if isinstance(args, str) else args,
                    })
                chat_messages.append({"role": "assistant", "content": blocks})
            else:
                chat_messages.append({"role": msg["role"], "content": msg["content"]})

        # Translate tools from OpenAI format to Anthropic format
        anthropic_tools = None
        if "tools" in kwargs:
            anthropic_tools = []
            for tool in kwargs.pop("tools"):
                func = tool.get("function", {})
                anthropic_tools.append({
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
                })

        payload: dict[str, Any] = {
            "model": self._endpoint.model,
            "messages": chat_messages,
            "max_tokens": max_tokens or self._endpoint.max_tokens,
            "temperature": temperature,
        }
        if system_text.strip():
            payload["system"] = system_text.strip()
        if anthropic_tools:
            payload["tools"] = anthropic_tools
        payload.update(kwargs)

        response = self._client.post(url, json=payload, headers=headers)
        if not response.is_success:
            try:
                err_body = response.json()
            except Exception:
                err_body = response.text
            raise RuntimeError(f"{response.status_code} from {url}: {err_body}")
        data = response.json()

        # Anthropic response: content is a list of blocks
        content = ""
        tool_calls = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                content += block.get("text", "")
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })

        return LLMResponse(
            content=content,
            model=data.get("model", self._endpoint.model),
            usage=data.get("usage", {}),
            raw=data,
            provider_name=self.name,
            tool_calls=tool_calls,
        )

    def _resolve_api_key(self) -> str:
        """Resolve API key, supporting ${ENV_VAR} syntax."""
        key = self._endpoint.api_key
        if key.startswith("${") and key.endswith("}"):
            env_name = key[2:-1]
            return os.environ.get(env_name, "")
        return key

    def close(self) -> None:
        self._client.close()


_PROVIDER_MAP: dict[str, type[LLMProvider]] = {
    "openai": OpenAICompatibleProvider,
    "anthropic": AnthropicProvider,
}


class LLMRouter:
    """Routes LLM requests to configured providers with priority-based fallback.

    Providers are tried in priority order (lowest number first).
    If a provider fails, the next one is attempted.
    """

    def __init__(self) -> None:
        self._providers: list[tuple[int, LLMProvider]] = []

    @property
    def provider_count(self) -> int:
        return len(self._providers)

    def add_provider(self, provider: LLMProvider, priority: int = 0) -> None:
        """Register a provider with a priority (lower = tried first)."""
        self._providers.append((priority, provider))
        self._providers.sort(key=lambda x: x[0])
        logger.info("Added LLM provider '%s' with priority %d", provider.name, priority)

    def add_endpoint(self, endpoint: LLMEndpoint) -> None:
        """Create and register a provider from an endpoint config.

        The provider type is selected by the endpoint's 'provider' field:
        "openai" (default) or "anthropic".
        """
        provider_cls = _PROVIDER_MAP.get(endpoint.provider)
        if provider_cls is None:
            raise ValueError(
                f"Unknown provider '{endpoint.provider}' for endpoint '{endpoint.name}'. "
                f"Supported: {', '.join(_PROVIDER_MAP)}"
            )
        provider = provider_cls(endpoint)
        self.add_provider(provider, endpoint.priority)

    def chat(
        self,
        messages: list[dict[str, str]],
        max_tokens: int = 1024,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> LLMResponse:
        """Send a chat request, trying providers in priority order.

        Raises RuntimeError if all providers fail or none are configured.
        """
        if not self._providers:
            raise RuntimeError(
                "No LLM providers configured. "
                "Add at least one endpoint in config/default.yaml or via CLAWLESS_ env vars."
            )

        errors: list[str] = []
        for priority, provider in self._providers:
            try:
                logger.debug("Trying LLM provider '%s' (priority %d)", provider.name, priority)
                response = provider.chat(
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    **kwargs,
                )
                logger.debug("Got response from '%s' (%d tokens)", provider.name, len(response.content))
                return response
            except Exception as e:
                error_msg = f"{provider.name}: {type(e).__name__}: {e}"
                errors.append(error_msg)
                logger.warning("Provider '%s' failed: %s", provider.name, e)

        raise RuntimeError(
            f"All {len(self._providers)} LLM providers failed:\n" + "\n".join(errors)
        )

    def close(self) -> None:
        """Close all provider HTTP clients."""
        for _, provider in self._providers:
            if hasattr(provider, "close"):
                provider.close()
