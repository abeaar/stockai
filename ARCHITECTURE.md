# StockAI — Architecture Tour

A one-page map of the codebase for fast orientation. Last verified: 2026-06-28.

## TL;DR

- **Single-user CLI**, Typer + Rich, runs as `uv run stockai <command>`.
- **Local-first**: SQLite (via SQLAlchemy) for persistence, yfinance for market data.
- **AI-optional**: works without an API key (deterministic signals), gets smarter with Google Gemini / OpenAI / Anthropic.
- **No background services**: each command is a one-shot `python -m stockai.cli.main ...`.

## Layer map

```
                  ┌──────────────────────────────┐
                  │  cli/main.py  (Typer app)    │  <- entry point, 27+ commands
                  └──────────────┬───────────────┘
                                 │
        ┌────────────┬───────────┼───────────┬──────────────┐
        ▼            ▼           ▼           ▼              ▼
   ┌────────┐  ┌────────┐  ┌─────────┐  ┌─────────┐  ┌─────────────┐
   │scoring │  │ agent  │  │ briefing│  │ autopilot│  │  intraday   │  <- business logic
   └───┬────┘  │+agents │  └────┬────┘  └────┬────┘  └──────┬──────┘
       │       └────┬───┘       │           │              │
       │            │           │           │              │
       └────────────┴───────────┴───────────┴──────────────┘
                                 │
                  ┌──────────────▼───────────────┐
                  │  data/  (SQLAlchemy + cache)  │  <- persistence
                  └──────────────┬───────────────┘
                                 │
                  ┌──────────────▼───────────────┐
                  │  data/sources/ (yahoo, idx)  │  <- external IO
                  └──────────────────────────────┘
```

## Top-level entry

`src/stockai/__init__.py` — exports `__version__`, `get_settings`.
`src/stockai/__main__.py` — `python -m stockai` shim.
`src/stockai/cli/main.py` — the Typer app. 5,830 LOC. Registers all commands.

## Module-by-module

| Module | LOC | What it does |
|---|---|---|
| `cli/main.py` | 5,830 | The CLI. Every user-facing command lives here. |
| `intraday/` | ~1,300 | Intraday plan generator + paper-trade log. **Added 2026-06-28.** |
| `autopilot/` | ~1,900 | Automated daily trading system with validator + executor. |
| `tools/stock_tools.py` | 1,241 | LangChain tool wrappers (used by agents). |
| `core/sentiment/news.py` | 942 | News ingestion + sentiment scoring. |
| `core/predictor/accuracy.py` | 928 | Prediction accuracy tracking. |
| `agents/orchestrator.py` | 740 | Multi-agent LangChain orchestrator. |
| `tutorial/lessons.py` | 716 | The "learn" command (lessons + quizzes). |
| `web/routes.py` | 704 | FastAPI dashboard (the `web` command). |
| `agent/` (singular) | ~1,500 | The new "Kai-Code" agent runtime (memory, phases, state). |
| `briefing/daily.py` | 492 | Morning/evening/weekly briefings. |
| `tutorial/paper_trading.py` | 472 | The `paper` command (virtual portfolio). |
| `core/portfolio/analytics.py` | 457 | Portfolio metrics. |
| `core/predictor/features.py` | 453 | Feature engineering for ML predictor. |

## Data layer

| File | Purpose |
|---|---|
| `data/database.py` | SQLAlchemy engine, WAL mode, `session_scope` context manager. |
| `data/models.py` | `Base` + 13 ORM models: `Stock`, `StockPrice`, `PortfolioItem`, `Prediction`, `WatchlistItem`, … **and our 2 added: `IntradayPlanRow`, `IntradayOutcomeRow`.** |
| `data/cache.py` | TTL cache wrapper around the data sources. |
| `data/listings.py` | Stock universe master list (curated, not scraped). |
| `data/sectors.py` | Sector mapping for IDX names. |
| `data/sources/yahoo.py` | `YahooFinanceSource` — `.get_price_history()`, `.get_stock_info()`, `.get_current_price()`, etc. **This is the only network source the `intraday` module needs.** |
| `data/sources/idx.py` | `IDXIndexSource` + `get_idx30()`, `get_lq45()`, `get_jii70()` (in-memory). |

### Database file

`stockai/config.py` defines `db_path = "data/stockai.db"` (relative) and
`project_root = src/stockai/.. → src → src/..` so the resolved path is
**`E:\project\data\stockai.db`** (one level up from the project root).
This is a pre-existing quirk; the intraday module inherits it.

Tables in that DB (verified 2026-06-28):

```
agent_memories, autopilot_runs, autopilot_trades, autopilot_validations,
cache_entries, intraday_outcomes, intraday_plans, news_articles,
portfolio_items, portfolio_transactions, predictions, stock_prices,
stocks, watchlist_items
```

## Configuration

`config.py` (Pydantic Settings) reads from `.env` with prefix `STOCKAI_`:

```
GOOGLE_API_KEY   - Gemini (LangChain default)
OPENAI_API_KEY   - OpenAI / OpenCode proxy
ANTHROPIC_API_KEY - Claude
FIRECRAWL_API_KEY - web research
TAVILY_API_KEY    - alternative search
STOCKAI_DB_PATH   - override DB location
STOCKAI_LOG_LEVEL - DEBUG/INFO/WARNING/ERROR
```

## CLI commands (verified 2026-06-28)

```
list, init, config, tools, info, analyze, quality, eval-smart-money,
predict, volume, suggest, train, history, web, morning, evening, weekly,
portfolio, sentiment, agents, predictions, watchlist, auto, learn, paper,
score, risk, autopilot
intraday  (sub-app: screen, plan, report, evaluate, log, stats)
```

## AI layer

There are **two parallel** agent runtimes:

- `agents/` (plural) — the original 7-agent LangChain system (Analyst,
  Researcher, Risk Manager, etc.). Used by the `agents` command.
- `agent/` (singular) — the newer "Kai-Code" runtime with memory + phases.
  Imported as a git dep `kai-code @ git+https://github.com/mta-tech/kai-code.git`
  in `pyproject.toml`. Currently used by `kai_tools/`.

PRD at `PRD-opencode-integration.md` is the open question of whether to
abstract the LLM factory so OpenCode can replace Gemini.

## How `intraday` plugs in (the new code)

```
        cli/main.py (intraday sub-app)
            │
            ├── intraday/scoring.py   ──► data/sources/yahoo.py
            ├── intraday/planner.py   ──► (pure compute from IntradayProfile)
            ├── intraday/reporter.py  ──► (pure markdown render)
            ├── intraday/models.py    ──► data/models.py (Base)
            ├── intraday/storage.py   ──► data/database.py
            └── scripts/seed_backtest.py  (dev only)
```

- Scoring mirrors `reports/idx_intraday_202605.md` (the May report).
- `IntradayProfile` is the in-memory snapshot; `IntradayPlanRow` is its DB row.
- `evaluate_pending_plans()` uses yfinance `get_price_history()` for the next
  session and the standard daily-OHLC backtest approximation to classify
  outcomes.

## Gotchas

1. **CLAUDE.md mentions Mac paths** (`/Users/fitrakacamarga/...`) that don't
   work on this Windows host. The actual Python is whatever `uv run` resolves
   to (the venv at `E:\project\stockai\.venv`). Update CLAUDE.md if you want
   it to be accurate.
2. **DB path quirk** — see "Database file" above.
3. **yfinance is daily-only** for `.JK` tickers. Intraday backtesting on real
   tick data needs a paid feed.
4. **Typer 0.9** does NOT accept `datetime.date` as a CLI option type — use
   `str` and parse manually. The `intraday evaluate` and `intraday log`
   commands follow this pattern.
5. **uv prints a warning** that `VIRTUAL_ENV` (Hermes' venv) doesn't match
   the project's `.venv`. It still works because `uv run` uses the project
   venv; the warning is benign.

## Running it

```bash
# install / sync
uv sync

# daily morning workflow
uv run stockai morning
uv run stockai quality BBCA           # single-name deep dive
uv run stockai autopilot --dry-run    # see what the bot would do

# our new intraday workflow
uv run stockai intraday report        # generate today's plan
uv run stockai intraday evaluate      # fill in yesterday's outcomes
uv run stockai intraday stats         # see if the system is working
```
