"""Provider-agnostic Claude client.

One code path for every cloud: the agent always talks to `LLM`, and `LLM`
builds the right Anthropic SDK client for the configured provider:

* ``anthropic`` - Claude API (``Anthropic``)
* ``bedrock``   - Amazon Bedrock (``AnthropicBedrockMantle``, IAM credentials)
* ``vertex``    - Google Vertex AI (``AnthropicVertex``, Application Default Credentials)
* ``foundry``   - Microsoft Foundry (``AnthropicFoundry``, API key or Entra ID)

All of them expose the same ``beta.messages`` surface, so nothing downstream
needs to know which one is in use.

Refusal handling: when a safety classifier declines a request, the Claude API
can re-run it on a fallback model server-side (``fallbacks="default"``). Bedrock,
Vertex and Foundry don't support that parameter, so there the SDK's client-side
``BetaRefusalFallbackMiddleware`` does the same job.
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from typing import Any, TypeVar

import anthropic
from anthropic import BetaFallbackState, BetaRefusalFallbackMiddleware
from pydantic import BaseModel

from ap_agent.config import Settings

log = logging.getLogger(__name__)

SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"

T = TypeVar("T", bound=BaseModel)


def _provider_model_id(provider: str, model: str) -> str:
    # Bedrock model IDs carry an "anthropic." prefix; the others use the bare ID.
    if provider == "bedrock" and not model.startswith("anthropic."):
        return f"anthropic.{model}"
    return model


def build_client(settings: Settings) -> Any:
    provider = settings.llm_provider
    middleware = None
    if provider != "anthropic":
        middleware = [
            BetaRefusalFallbackMiddleware(
                [{"model": _provider_model_id(provider, settings.llm_fallback_model)}]
            )
        ]

    if provider == "anthropic":
        # Resolves ANTHROPIC_API_KEY / workload identity federation from the env
        # when no explicit key is configured.
        kwargs = {"api_key": settings.anthropic_api_key} if settings.anthropic_api_key else {}
        return anthropic.Anthropic(**kwargs)
    if provider == "bedrock":
        # Uses the task/instance IAM role (or AWS_* env vars) automatically.
        return anthropic.AnthropicBedrockMantle(aws_region=settings.aws_region, middleware=middleware)
    if provider == "vertex":
        # Uses the Cloud Run / GKE service account via Application Default Credentials.
        return anthropic.AnthropicVertex(
            project_id=settings.vertex_project_id,
            region=settings.vertex_region,
            middleware=middleware,
        )
    if provider == "foundry":
        kwargs: dict[str, Any] = {"resource": settings.foundry_resource, "middleware": middleware}
        if settings.foundry_api_key:
            kwargs["api_key"] = settings.foundry_api_key
        else:  # Managed identity / Entra ID
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            kwargs["azure_ad_token_provider"] = get_bearer_token_provider(
                DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
            )
        return anthropic.AnthropicFoundry(**kwargs)
    raise ValueError(f"Unknown llm_provider {provider!r}")


class LLM:
    """Thin wrapper that applies model, fallbacks and caching consistently."""

    def __init__(self, settings: Settings, client: Any | None = None):
        self.settings = settings
        self.client = client if client is not None else build_client(settings)
        self.model = _provider_model_id(settings.llm_provider, settings.llm_model)
        self._server_fallbacks = settings.llm_provider == "anthropic"

    def new_conversation_state(self) -> BetaFallbackState | None:
        """One state per conversation pins follow-up turns to the model that accepted."""
        return None if self._server_fallbacks else BetaFallbackState()

    def _common(self, params: dict[str, Any]) -> dict[str, Any]:
        params = {"model": self.model, **params}
        betas = list(params.pop("betas", []))
        if self._server_fallbacks:
            betas.append(SERVER_FALLBACK_BETA)
            params["fallbacks"] = "default"
        if betas:
            params["betas"] = betas
        if self._server_fallbacks and self.settings.llm_inference_geo:
            params["inference_geo"] = self.settings.llm_inference_geo  # data residency (Claude API)
        # Cache the stable prefix (tools + system prompt + earlier turns).
        params.setdefault("cache_control", {"type": "ephemeral"})
        return params

    def stream_message(self, state: BetaFallbackState | None = None, **params: Any):
        """Stream a request and return the final message (avoids HTTP timeouts on
        long inputs such as multi-page invoice PDFs)."""
        params = self._common(params)
        with state if state is not None else nullcontext():
            with self.client.beta.messages.stream(**params) as stream:
                return stream.get_final_message()

    def parse(self, output_format: type[T], state: BetaFallbackState | None = None, **params: Any):
        """Structured output call; returns the full response (use ``.parsed_output``)."""
        params = self._common(params)
        with state if state is not None else nullcontext():
            return self.client.beta.messages.parse(output_format=output_format, **params)
