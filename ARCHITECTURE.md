# SupplyPilot architecture notes

## Agent boundaries

| Agent | Input | Responsibility | Output dependency |
|---|---|---|---|
| Demand Forecasting | Sales history + forecast tool output | Demand velocity, trend, confidence, SKU watchlist | Feeds inventory risk and procurement synthesis |
| Inventory Risk | Inventory + forecast output | Days of cover, reorder exposure, urgency | Feeds procurement synthesis |
| Supplier Intelligence | Supplier records + weighted score tool | Cost/reliability/quality/capacity trade-offs | Feeds procurement synthesis |
| Supply Risk | Supplier rankings + inventory position | Single-source and capacity resilience | Feeds procurement synthesis |
| Procurement Planning | All four findings + numeric order options | Prioritize actions and explain trade-offs | Feeds independent auditor |
| Decision Auditor | Planner output + all specialist findings + hard checks | Challenge assumptions and identify review requirements | Determines human-review status |

## Execution rules

- The four specialist Gemini requests are independent and execute concurrently.
- The procurement planner runs only after all four specialists complete.
- The decision auditor runs only after the planner completes.
- Each model request is counted; transient provider errors are retried up to two times after the initial attempt.
- If an agent fails, the run fails visibly. There is no mocked or rules-only substitute presented as a successful multi-agent run.
- Deterministic analytics and hard constraints remain separate from LLM interpretation. The model cannot change the numeric procurement options.
- The final status means only `ready_for_human_review` or `review_required`; neither is purchase authorization.