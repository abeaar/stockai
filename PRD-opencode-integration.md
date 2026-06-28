# PRD: OpenCode AI Provider Integration for StockAI

**Project:** Replace Google Gemini with OpenCode as AI backend for StockAI
**Author:** Abraar / Hermes Agent
**Date:** 2026-01-24
**Status:** Draft

---

## 1. Problem Statement

### Current State
- StockAI uses **Google Gemini** via `ChatGoogleGenerativeAI` (LangChain)
- API key required in `.env` (`GOOGLE_API_KEY`)
- User has **OpenCode subscription** but cannot use it as AI backend
- Gemini pricing model means user pays Google separately from their OpenCode subscription

### Pain Points
1. **Duplicate payments** — User pays for OpenCode but also needs Google API credits
2. **Gemini limitations** — User believes OpenCode provides better models for trading analysis
3. **Hardcoded provider** — AI provider is not abstracted; switching requires code changes
4. **No local proxy** — OpenCode could act as a gateway but isn't configured

### User Goal
Use **OpenCode** as the AI backend instead of Google Gemini, leveraging existing subscription.

---

## 2. Proposed Solution

### Option A: OpenCode as Local Proxy (Recommended)
Run OpenCode as a **local API proxy** that StockAI calls via `localhost`. OpenCode routes to whichever backend (OpenAI, Claude, etc.) based on configuration.

```
StockAI CLI
    ↓ (HTTP)
localhost:8080 (OpenCode Gateway)
    ↓ (OpenAI / Claude / etc.)
Your AI Backend via OpenCode
```

**Pros:**
- No code changes to StockAI's AI layer
- OpenCode handles provider routing
- Works with any OpenCode-compatible model
- Preserves existing StockAI code

**Cons:**
- Requires OpenCode to run locally as a service
- Additional setup complexity

### Option B: Swap LangChain Model Classes
Replace `ChatGoogleGenerativeAI` with `ChatOpenAI` or Anthropic's client throughout the codebase.

**Pros:**
- Direct connection to OpenAI/Anthropic
- No proxy layer needed

**Cons:**
- Code modifications throughout `agents/` and `agent/` directories
- Maintenance burden on each StockAI update
- Provider credentials in code vs. centralized config

---

## 3. Technical Analysis

### Files That Need Changes

#### Option A (Proxy) — Minimal Changes
| File | Change |
|------|--------|
| `src/stockai/config.py` | Add `OPENAI_API_KEY` usage pattern |
| `.env` | Point to localhost proxy |

**Estimated Effort:** Low

#### Option B (Direct Swap) — Major Changes
| File | Change |
|------|--------|
| `src/stockai/agents/orchestrator.py` | Replace `ChatGoogleGenerativeAI` with `ChatOpenAI` |
| `src/stockai/agent/orchestrator.py` | Replace `ChatGoogleGenerativeAI` with `ChatOpenAI` |
| `src/stockai/config.py` | Update MODEL_MAP, add OpenAI key support |
| `src/stockai/agents/config.py` | Update default model |
| `src/stockai/tools/stock_tools.py` | Check for API dependency |
| `pyproject.toml` | May need `langchain-openai` package |

**Estimated Effort:** High (10+ files, multiple code changes)

### Current LangChain Usage
```python
# src/stockai/agents/orchestrator.py (line 181)
from langchain_google_genai import ChatGoogleGenerativeAI

return ChatGoogleGenerativeAI(
    model=model_id,
    google_api_key=settings.google_api_key,
    temperature=self.config.temperature,
    max_tokens=self.config.max_tokens,
)
```

### Target LangChain Usage (Option B)
```python
from langchain_openai import ChatOpenAI

return ChatOpenAI(
    model="gpt-4o",  # or user's choice
    api_key=settings.openai_api_key,  # or OpenCode proxy key
    temperature=self.config.temperature,
    max_tokens=self.config.max_tokens,
)
```

---

## 4. Implementation Plan

### Phase 1: Research & Setup
- [ ] Confirm OpenCode local proxy setup requirements
- [ ] Identify exact endpoint format OpenCode exposes
- [ ] Test connectivity to OpenCode from local environment

### Phase 2: Configuration Layer
- [ ] Update `config.py` to support OpenAI-compatible endpoint
- [ ] Add `OPENAI_API_KEY` as primary key (user already has OpenCode)
- [ ] Create `.env` template with OpenCode settings

### Phase 3: Agent Integration
- [ ] Create abstract LLM factory pattern (future-proofing)
- [ ] Replace `ChatGoogleGenerativeAI` with configurable LLM
- [ ] Test with `stockai analyze BBCA`

### Phase 4: Validation
- [ ] Run `stockai quality BBCA` with OpenCode backend
- [ ] Run `stockai autopilot run --dry-run` to test full flow
- [ ] Verify 7-agent system works with new provider

---

## 5. Open Questions

1. **OpenCode setup:** How does OpenCode expose its local API? Is it a REST endpoint, OpenAI-compatible API, or something else?
2. **Model selection:** Which specific model should be the default? (GPT-4o, Claude 3.5 Sonnet, etc.)
3. **Cost tracking:** Should we log which backend was used for audit purposes?
4. **Fallback:** If OpenCode is down, should there be a Gemini fallback?

---

## 6. Success Criteria

- [ ] `stockai quality BBCA --ai` runs without Google API key
- [ ] All 7 AI agents produce valid output via OpenCode
- [ ] No duplicate payments (uses existing OpenCode subscription)
- [ ] Easy to switch providers in future (abstracted config)

---

## 7. Priority

**Medium-High** — Core functionality depends on AI provider. Without this, the multi-agent features don't work.

---

## Next Session Checklist

- [ ] Confirm OpenCode API endpoint format
- [ ] Get user's OpenAI API key (if using OpenAI direct)
- [ ] Decide between Option A (proxy) vs Option B (direct swap)
- [ ] Start Phase 1 implementation
