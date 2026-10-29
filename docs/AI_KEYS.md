# Several API keys per LLM provider

Every provider in `jobapply.ai.providers.CATALOG` can have more than one key. Keys of one provider are rotated
round-robin and each key cools down on its own, so a free-tier quota or a rate limit on one key does not take the
provider out: the next key of the same provider answers, and only when every key of that provider is cooling does
the chain fall through to the next provider.

## Server keys (.env)

```
JOBAPPLY_GEMINI_API_KEY=AIza...one,AIza...two        # one key, or a comma-separated list
JOBAPPLY_GEMINI_API_KEYS="AIza...three               # alias, newline- or comma-separated; merged after _API_KEY
AIza...four"
JOBAPPLY_ANTHROPIC_API_KEY=sk-ant-...                 # Claude uses the ANTHROPIC_ prefix, same rules
```

`server_configs(name)` returns one `ProviderConfig` per key (same model and base URL), labelled `gemini#1`,
`gemini#2`, ... in the order given. Duplicates are dropped. `server_config(name)` still returns the first.

## User keys (Settings -> AI)

Each provider has a textarea "API keys (one per line)". They are stored encrypted in the vault as the `api_key`
credential with the secret `{"password": "<first>", "keys": ["<first>", "<second>", ...]}`. Older rows that only
have `password` keep working as a single key. A user's keys are labelled `gemini#u1`, `gemini#u2` and are tried
before the server's keys for the same provider.

## Chain and failover

For the order `gemini,openai` with two user keys and two server keys for Gemini the chain is
`gemini#u1, gemini#u2, gemini#1, gemini#2, openai#1`.

- Within a provider the starting key rotates round-robin per process (`providers._rr`), so successive requests
  spread over the healthy keys instead of always hitting the first. Rotation stays inside one source: the
  user's keys rotate among themselves and the server's among themselves (`_rr` is keyed `gemini:user` /
  `gemini:server`), so a user's own keys always answer before the server's keys are charged.
- The chain's combined error is retryable only when some key failed with a rate limit, credits, auth or network
  problem or was sitting in cooldown. When every key answered with a per-request problem (refusal, bad request,
  a reply that did not match the schema) the error is not retryable: the scorer records that one job and keeps
  using the chain for the next one instead of switching the rest of the run to heuristics.
- The Settings page masks the keys textarea with `-webkit-text-security` (Chrome, Edge, Safari) and offers a
  "show keys while typing" toggle. Firefox cannot mask a multi-line field: keys are visible while typing there.
  Saved keys are never rendered again; only the count is shown.
- A 429 / quota / credits error puts only that key into cooldown (`JOBAPPLY_LLM_COOLDOWN_MINUTES`, 6x for quota,
  credits and auth failures); the next key of the same provider is tried before the next provider.
- A bad key (401) parks only that key for the long cooldown.
- Cooldowns are keyed by provider + SHA1 of the key (`health_key`), so the same server key shares one state across
  users and one user's key never blocks another's.

## Usage records

`llm_usage.model` stores the key label in front of the model: `gemini#2:gemini-2.5-flash` (a user's key:
`gemini#u1:gemini-2.5-flash`). No schema change was needed; the Admin page folds 30-day usage onto the label.

## Admin page

The provider table shows one row per server key as `<provider> key #n (…last4)`, with the key's cooldown left,
successes / failures and 30-day usage. The full key is never rendered; keys shorter than 12 characters are shown
through `jobapply.vault.mask`.
