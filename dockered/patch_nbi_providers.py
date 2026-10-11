"""Model-list patch for notebook-intelligence 6.2.0 against this lab's reality.

Two defects in the installed 6.2.0 wheel make every AI lane in the Jupyter
container 400 or silently no-op. Both are patched in place at image build,
mirroring patch_jupyterlab_pypi.py's exact-text, skip-if-changed approach:

1. GitHub Copilot chat dropdown (llm_providers/github_copilot_llm_provider.py)

   ``fetch_copilot_models()`` filters the live catalogue from
   https://api.githubcopilot.com/models on ``model_picker_enabled``. On the
   ``free_educational_quota`` SKU every entry arrives with that flag false,
   so the filter drops the whole catalogue and the hardcoded fallback list
   becomes the dropdown. That list is stale: every premium id on it
   (gpt-5.3-codex, claude-*, gemini-*) answers 400
   ``model_not_supported`` on this SKU. Verified 200 against
   /chat/completions on 2026-10-11 for this account: gpt-4.1, gpt-4o,
   gpt-4o-mini (plus their versioned variants). The fallback is replaced
   with exactly those. Paid SKUs unaffected: there picker-enabled entries
   exist and the dynamic catalogue still wins.

2. Native Ollama inline-completion ids (llm_providers/ollama_llm_provider.py)

   The provider hardcodes untagged model ids (qwen2.5-coder, codestral, ...)
   and the Ollama API only resolves exact tags: untagged means ``:latest``,
   which this Ollama host does not have -> every inline completion 404s
   ("model 'qwen2.5-coder' not found"). The list is replaced with the
   tag-qualified models actually installed on the host lane
   (192.168.56.1:11434), reusing the upstream FIM prompt templates:
   qwen2.5-coder:7b/14b/32b and codestral:22b. This lane is the working
   inline-completion path: the LiteLLM-compatible provider passes
   prompt/suffix to litellm.completion(), and litellm 1.105.0 dropped
   prompt-style completion (it sends an empty ``messages`` array), so inline
   completions through it return empty strings forever.

If notebook-intelligence is upgraded past 6.2.0, the exact-text hunks stop
matching, the script logs, and the build proceeds unpatched.
"""

from __future__ import annotations

import sys
from importlib.metadata import distribution
from pathlib import Path

COPILOT_FALLBACK_OLD = '''    # Used until the dynamic catalogue from https://api.githubcopilot.com/models
    # is populated. Lets pre-login users see a non-empty dropdown and
    # protects against API outages.
    _FALLBACK_CHAT_MODELS: list[tuple[str, str, int]] = [
        ("gpt-5-mini", "GPT-5 mini", 128000),
        ("gpt-4.1", "GPT-4.1", 128000),
        ("gpt-4o", "GPT-4o", 128000),
        ("gpt-5", "GPT-5", 128000),
        ("gpt-5.3-codex", "GPT-5.3-Codex", 128000),
        ("claude-haiku-4.5", "Claude Haiku 4.5", 144000),
        ("claude-sonnet-4.6", "Claude Sonnet 4.6", 144000),
        ("claude-sonnet-4.5", "Claude Sonnet 4.5", 144000),
        ("claude-sonnet-4", "Claude Sonnet 4", 80000),
        ("claude-opus-4.6", "Claude Opus 4.6", 144000),
        ("gemini-3.1-pro", "Gemini 3.1 Pro", 128000),
        ("gemini-2.5-pro", "Gemini 2.5 Pro", 128000),
    ]'''

COPILOT_FALLBACK_NEW = '''    # Used when the dynamic catalogue from https://api.githubcopilot.com/models
    # yields nothing usable. On the free_educational_quota SKU every entry
    # arrives with model_picker_enabled=false, so the picker filter in
    # fetch_copilot_models() drops them all and this list IS the dropdown.
    # Ids verified 200 against /chat/completions on that SKU (2026-10-11);
    # premium ids 400 with model_not_supported. Keep this list limited to
    # ids confirmed live for the signed-in account.
    _FALLBACK_CHAT_MODELS: list[tuple[str, str, int]] = [
        ("gpt-4.1", "GPT-4.1", 128000),
        ("gpt-4o", "GPT-4o", 128000),
        ("gpt-4o-mini", "GPT-4o mini", 128000),
    ]'''

OLLAMA_INLINE_OLD = '''        return [
            OllamaInlineCompletionModel(self, "deepseek-coder-v2", "deepseek-coder-v2", 163840, DEEPSEEK_INLINE_COMPL_PROMPT),
            OllamaInlineCompletionModel(self, "qwen2.5-coder", "qwen2.5-coder", 32768, QWEN_INLINE_COMPL_PROMPT),
            OllamaInlineCompletionModel(self, "codestral", "codestral", 32768, CODESTRAL_INLINE_COMPL_PROMPT),
            OllamaInlineCompletionModel(self, "starcoder2", "starcoder2", 16384, STARCODER_INLINE_COMPL_PROMPT),
            OllamaInlineCompletionModel(self, "codellama:7b-code", "codellama:7b-code", 16384, CODELLAMA_INLINE_COMPL_PROMPT),
        ]'''

OLLAMA_INLINE_NEW = '''        return [
            OllamaInlineCompletionModel(self, "qwen2.5-coder:7b", "qwen2.5-coder:7b", 32768, QWEN_INLINE_COMPL_PROMPT),
            OllamaInlineCompletionModel(self, "qwen2.5-coder:14b", "qwen2.5-coder:14b", 32768, QWEN_INLINE_COMPL_PROMPT),
            OllamaInlineCompletionModel(self, "qwen2.5-coder:32b", "qwen2.5-coder:32b", 32768, QWEN_INLINE_COMPL_PROMPT),
            OllamaInlineCompletionModel(self, "codestral:22b", "codestral:22b", 32768, CODESTRAL_INLINE_COMPL_PROMPT),
        ]'''


def locate_module(relpath: str) -> Path | None:
    """Return the installed notebook_intelligence file path, if any."""
    try:
        dist = distribution("notebook-intelligence")
    except Exception:
        return None
    for file in dist.files or []:
        if file and str(file) == f"notebook_intelligence/{relpath}":
            path = Path(dist.locate_file(file))
            if path.is_file():
                return path
    return None


def apply(path: Path | None, label: str, old: str, new: str) -> None:
    if path is None:
        print(f"patch_nbi_providers: {label}: target file not found; skipping")
        return
    text = path.read_text(encoding="utf-8")
    if new in text:
        print(f"patch_nbi_providers: {label}: already applied")
    elif old in text:
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        print(f"patch_nbi_providers: {label}: patched")
    else:
        print(
            f"patch_nbi_providers: {label}: pattern not found; skipping "
            "(installed notebook-intelligence differs from 6.2.0)"
        )


def main() -> int:
    apply(
        locate_module("llm_providers/github_copilot_llm_provider.py"),
        "copilot-free-tier-fallback",
        COPILOT_FALLBACK_OLD,
        COPILOT_FALLBACK_NEW,
    )
    apply(
        locate_module("llm_providers/ollama_llm_provider.py"),
        "ollama-inline-installed-tags",
        OLLAMA_INLINE_OLD,
        OLLAMA_INLINE_NEW,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())