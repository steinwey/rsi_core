"""One choice for the text a solver sees before it starts.

Both routed modes share BM25 (`routing.retrieve_bm25`) and token accounting
(`budget.render_context`, the counters, and `max_initial_skill_tokens`).
They pack different units:

- `experience`: `repository_skill.route_task` ranks experience blocks, adjudicates
  conflicts, then packs blocks under `recall_k` and `max_blocks`.
- `files`: `routing.route` ranks reference files and `budget.select_context`
  packs whole files under `top_k` and `allow_zero_matches`.
- `full`: routing is off and the solver receives the skill guide unchanged.

`top_k` and `allow_zero_matches` apply only to `files`. Experience mode reads
`recall_k` and `max_blocks` from `repository_skill`.
"""


def routing_mode(config):
    if config.get('repository_skill', {}).get('enabled', False):
        return 'experience'
    if config.get('skill_routing', {}).get('enabled', False):
        return 'files'
    return 'full'
