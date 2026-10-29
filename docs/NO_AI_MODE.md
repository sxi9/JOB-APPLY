# No-AI mode

The pipeline runs end to end without any LLM. AI only fills gaps when a provider is configured and the user
allows it. Nothing waits on a key, a quota or a network.

## When it kicks in

- `JOBAPPLY_LLM_BACKEND=live` but no provider has a key (`get_llm` raises `LLMError`).
- The user set the preference `ai_mode=off` (`get_llm` refuses even when keys exist). `auto` is the default:
  use the LLM when there is one, heuristics otherwise.
- The failover chain is exhausted mid-run (rate limit, credits, network): the rest of that run uses the
  heuristics instead of stopping. A non-retryable error (bad request, refusal) still marks that one job
  `scoring failed` and leaves it `new`.

Each run logs once that it is in no-AI mode (`no-AI mode: ...`, logger `jobapply.pipeline.score`).

## What replaces the LLM (`jobapply.ai.heuristics`)

| Task | Fallback |
|---|---|
| Score | `score_job_heuristic`: skill/keyword overlap between title+description and `profile.skills` + `filters.roles`/`keywords_include`, seniority inside `filters.seniority`, location/remote match, `keywords_exclude` in the title, recency bonus. 0-100, deterministic. Reason prefixed `[heuristic]` on the dashboard. |
| Cover letter | `cover_letter_template(user, job, tone)`: three short paragraphs from profile facts only. |
| Resume | The uploaded master PDF unchanged, else the master JSON Resume rendered once to `variants/master.pdf`. |
| Form answers | `heuristic_answer`: answer bank first, then profile rules for authorization, sponsorship, relocation, notice period/start date, years with a skill, total years, salary, links, contact. Each rule is anchored to its subject ("expected CTC", "phone number", "available to start"), so "expected graduation date" or "mobile development" fall through to the LLM. Work-authorization yes/no answers need an explicit `needs_sponsorship`, and a question naming a country is only answered when that country appears in `profile.location` / `work_authorization`. Unknown questions return `(None, 0)` so the LLM gets them when there is one; otherwise the field stays empty and the run pauses for review. A heuristic answer is stored in the answer bank (`source=heuristic`, unapproved) so it can be reviewed and corrected on the Answers page. |
| Mail | `rule_category` is sufficient alone (verification, interview, assessment, rejection, spam, acknowledgement) and writes a template reply for interview/assessment. With an LLM available, interview/assessment mail still gets the rule's category but the LLM extracts the company and job title and tailors the reply; rejection/spam/verification never reach the LLM. Mail the rules could not place goes to the LLM when there is one. |

`FakeLLM` calls the same functions, so tests and no-AI runs agree.

## Confidence

Heuristic answers carry 70-95 confidence; the form runner still flags anything below `min_answer_confidence`
(50) for review, as with AI answers. A `[heuristic]` score is a keyword match, not a judgement: set the
score threshold accordingly or keep auto-submit off while running without AI.
