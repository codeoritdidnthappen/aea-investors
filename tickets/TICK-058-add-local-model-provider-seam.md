---
id: TICK-058
title: "feat(llm): dispatch on LLM_PROVIDER and add an OpenAI-compatible local client"
type: feature
epic: EPIC-09
priority: P1
estimate: M
depends_on: []
labels: [llm, backend]
source: [FR-33]
status: todo
remote_url: https://github.com/codeoritdidnthappen/aea-investors/issues/123
builder_commit: null
---
## Context

`docs/LOCAL_LLM_SPEC.md` D7. The config surface for this already exists and does
nothing: `.env.example` declares `LLM_PROVIDER=ollama  # groq | gemini | ollama`,
and the only code that reads it is `ai_server/app/health.py:30`, which reports it.
Nothing dispatches on it.

The seam is genuinely small. `GroqClient` (`ai_server/llm/groq.py`) is a Protocol
with two methods, `complete()` and `stream()`. `GroqSettings.endpoint` is already a
field defaulting to Groq's OpenAI-compatible URL, and the request body is the
ordinary chat-completions shape. A local server speaking the same API slots in
behind the same Protocol.

One caution the code already documents: `_strict_schema()` exists because Groq's
strict structured-output mode requires *every* property in `required`, with
optionality expressed as a nullable union -- discovered live, per its docstring.
That is a vendor quirk. It must not be assumed to generalise to the local backend,
and the adapter boundary is where that difference belongs.

This ticket delivers the seam and the Ollama adapter only. It changes no routing:
after it lands, the chat behaves exactly as it does today.

## Acceptance Criteria

- [ ] `LLM_PROVIDER` selects the client at startup. An unrecognised value fails at
      boot with a message naming the accepted values, rather than defaulting
      silently to a provider the operator did not ask for.
- [ ] A local OpenAI-compatible client implements the same Protocol as the Groq
      client. Base URL, model id, and any API key are configuration, not constants.
- [ ] Provider-specific request shaping lives behind the adapter. `_strict_schema()`
      and anything else vendor-specific applies to the provider that needs it and
      not to the one that does not.
- [ ] Streaming works on both clients, since the chat already streams.
- [ ] Choosing a provider that is not reachable is reported as unavailable at the
      point of use, not as a crash at import or a hung request.
- [ ] No behaviour change with `LLM_PROVIDER=groq`: the existing chat and
      scheduling tests pass untouched.

## Testing

Unit tests over provider selection (each accepted value, an unrecognised value,
absent), and over the local client's request construction and streaming against a
stubbed OpenAI-compatible endpoint. Assert the Groq path's request body is byte-for-byte
unchanged, so the seam cannot silently alter what an existing provider receives.
CI must be green.

## Out of Scope

Routing any turn to the local model (TICK-063). The vLLM adapter (TICK-067). Running
a model server in the local topology (TICK-059). Removing anything from `groq.py`.
