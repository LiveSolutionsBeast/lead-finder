# Search Provider Limits and Rotation Strategy

Lead Finder uses a multi-provider search router (`lf_search_providers.py`) with paid
APIs as the primary tier and self-hosted providers as fallback.

## Provider priority

1. **Brave Search** (`brave_search_api_key`) — 1,000 queries/month cap
2. **Tavily** (`tavily_api_key`) — 1,000 queries/month cap
3. **Exa** (`exa_api_key`) — 1,000 queries/month cap
4. **Serper** (`serper_api_key`) — 2,500 one-time credits (no monthly reset)
5. **Firecrawl** (`firecrawl_api_key`) — 500 queries/month cap
6. **Degoog** (`degoog_url`) — self-hosted, unlimited, currently captcha-prone
7. **4get** (`fourget_url`) — self-hosted, unlimited, currently DuckDuckGo-captcha'd
8. **SearXNG** (`searxng_url`) — self-hosted, unlimited, currently engine-suspended
9. **SerpAPI** (`serpapi_api_key`) — 250 queries/month cap, last resort

## Rotation behavior

- The router tries providers in the order above.
- Monthly providers are skipped once their calendar-month usage reaches the cap.
- **One-time providers** (Serper) are skipped once their lifetime usage reaches the cap.
- Usage is tracked in `provider_usage.json` by calendar month for monthly providers,
  and under a special `lifetime` key for one-time providers.
- Set `search_only_free: true` in `lf_config.json` to disable paid tiers.
- Pass `prefer="<provider>"` to force a specific provider.

## API keys

Keys are loaded from environment variables (preferred) or `lf_config.json`:

- `BRAVE_SEARCH_API_KEY`
- `TAVILY_API_KEY`
- `EXA_API_KEY`
- `SERPER_API_KEY`
- `FIRECRAWL_API_KEY`

Current keys are stored in `.env` (gitignored).

## When to rotate

Monitor `provider_usage.json`. Once a paid provider exceeds ~80% of its cap,
consider switching the default priority or pausing non-critical batches until
the next billing cycle (or until a new one-time key is obtained).
