"""Declarative config surface for the sovereign Hindsight memory provider.

Pure data only: the dashboard renders this, it never imports the provider module (which
pulls the agent runtime). Mirrors the field kinds in ``plugins/memory/config_schema.py``.
"""

from __future__ import annotations

from plugins.memory.config_schema import (
    KIND_BOOL,
    KIND_SELECT,
    KIND_SECRET,
    KIND_TEXT,
    STORAGE_FLAT_JSON,
    ProviderConfigSchema,
    ProviderField,
    ProviderFieldOption,
)

CONFIG_SCHEMA = ProviderConfigSchema(
    name="hermes-memory-hindsight",
    label="Sovereign Memory (Hindsight)",
    storage=STORAGE_FLAT_JSON,
    docs_url="https://hindsight.vectorize.io/",
    fields=(
        ProviderField(
            key="base_url",
            label="Substrate URL",
            kind=KIND_TEXT,
            default="http://127.0.0.1:8888",
            description="Base URL of the Hindsight substrate.",
            group="Connection",
            inline=True,
        ),
        ProviderField(
            key="namespace",
            label="Namespace",
            kind=KIND_TEXT,
            default="hermes",
            description="'hermes' for the shared substrate, or 'erature/<owner>' per owner.",
            group="Connection",
            inline=True,
        ),
        ProviderField(
            key="budget",
            label="Recall budget",
            kind=KIND_SELECT,
            default="mid",
            options=(
                ProviderFieldOption(value="low", label="Low"),
                ProviderFieldOption(value="mid", label="Mid"),
                ProviderFieldOption(value="high", label="High"),
            ),
            group="Recall",
        ),
        ProviderField(
            key="commitment",
            label="Auto-commit decisions",
            kind=KIND_BOOL,
            default="true",
            description="Promote detected decisions into standing directives.",
            group="Recall",
        ),
        ProviderField(
            key="api_key",
            label="API key",
            kind=KIND_SECRET,
            env_key="HINDSIGHT_API_KEY",
            description="Bearer token for the substrate.",
            group="Credentials",
            inline=True,
        ),
    ),
)
