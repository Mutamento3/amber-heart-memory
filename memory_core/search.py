"""Three-route recall with the existing normalization, weights and failure isolation."""

from .models import embed_for
from .query import clean_query, keyword_query
from .recall_config import RecallSettings
from .space import RevisionConflict


def _make_route_hit(
    memory_id: int,
    raw_score: float,
    route_name: str,
    reason: str,
    source: str,
    field: str | None = None,
    hop_count: int | None = None,
    seed_entity_ids: list[int] | None = None,
    seed_topic_ids: list[int] | None = None,
    metadata: dict | None = None,
) -> dict:
    return {
        'memory_id': memory_id,
        'raw_score': float(raw_score),
        'norm_score': 0.0,
        'route_name': route_name,
        'reason': reason,
        'source': source,
        'field': field,
        'hop_count': hop_count,
        'seed_entity_ids': seed_entity_ids or [],
        'seed_topic_ids': seed_topic_ids or [],
        'metadata': metadata or {},
    }

def normalize_hits(results: dict) -> dict:
    if not results:
        return {}

    max_score = max(hit['raw_score'] for hit in results.values()) or 1.0
    normalized: dict = {}
    for memory_id, hit in results.items():
        copied = dict(hit)
        copied['norm_score'] = round(hit['raw_score'] / max_score, 6)
        normalized[memory_id] = copied  # type: ignore[assignment]
    return normalized

def merge_and_rank(
    kw_results: dict,
    vec_results: dict,
    graph_results: dict,
    top_k: int = 5,
) -> list[dict]:
    """三路合并、归一化、加权和交叉加分。"""
    kw_norm = normalize_hits(kw_results)
    vec_norm = normalize_hits(vec_results)
    graph_norm = normalize_hits(graph_results)

    all_ids = set(kw_norm) | set(vec_norm) | set(graph_norm)
    ranked: list[dict] = []

    for memory_id in all_ids:
        kw_hit = kw_norm.get(memory_id)
        vec_hit = vec_norm.get(memory_id)
        graph_hit = graph_norm.get(memory_id)

        kw_score = kw_hit['norm_score'] if kw_hit else 0.0
        vec_score = vec_hit['norm_score'] if vec_hit else 0.0
        graph_score = graph_hit['norm_score'] if graph_hit else 0.0

        route_hits = [
            route_name
            for route_name, hit in (
                ('keyword', kw_hit),
                ('vector', vec_hit),
                ('graph', graph_hit),
            )
            if hit
        ]

        routes_hit_count = len(route_hits)
        cross_bonus = {
            0: 0.0,
            1: 0.0,
            2: 0.06,
            3: 0.10,
        }[routes_hit_count]

        final_score = (
            0.25 * kw_score
            + 0.35 * vec_score
            + 0.30 * graph_score
            + cross_bonus
        )

        reasons = []
        if kw_hit:
            reasons.append(kw_hit['reason'])
        if vec_hit:
            reasons.append(vec_hit['reason'])
        if graph_hit:
            reasons.append(graph_hit['reason'])

        ranked.append({
            'memory_id': memory_id,
            'final_score': round(final_score, 6),
            'route_hits': route_hits,
            'routes_hit_count': routes_hit_count,
            'keyword_score_norm': round(kw_score, 6),
            'vector_score_norm': round(vec_score, 6),
            'graph_score_norm': round(graph_score, 6),
            'cross_route_bonus': round(cross_bonus, 6),
            'reasons': reasons,
            'route_evidence': {name: hit for name, hit in (('keyword',kw_hit),('vector',vec_hit),('graph',graph_hit)) if hit},
        })

    ranked.sort(key=lambda item: item['final_score'], reverse=True)
    return ranked[:top_k]


class ThreeRouteSearch:
    def __init__(self, store, settings=None, *, item_kind="atomic", include_archived=False, candidate_limit=30):
        self.store = store
        self.settings = RecallSettings() if settings is None else settings
        store._search_options(candidate_limit, item_kind, include_archived)
        self.item_kind = item_kind
        self.candidate_limit = candidate_limit
        self.include_archived = include_archived
        self.route_options = dict(limit=candidate_limit, item_kind=item_kind, include_archived=include_archived)

    def _keyword_hits(self, query):
        text, terms = keyword_query(query, self.policy["ignored_terms"])
        rows = self.store.search_keywords(text, **self.route_options)
        hits = {r['memory_id']: _make_route_hit(
            r['memory_id'], r['raw_score'], 'keyword', r['keyword_reason'], r['keyword_source'],
            field=r['keyword_field'],
            seed_entity_ids=[r['matched_entity_id']] if r['matched_entity_id'] else [],
            seed_topic_ids=[r['matched_topic_id']] if r['matched_topic_id'] else [],
            metadata={'keyword_query': text, 'keyword_terms': terms,
                      'matched_entity_id': r['matched_entity_id'], 'matched_topic_id': r['matched_topic_id']},
        ) for r in rows}
        return hits

    def _seeds(self, query, vector):
        text, terms = keyword_query(query, self.policy["ignored_terms"])
        rows = self.store.search_keywords(text, **self.route_options)
        blocked = set(self.policy['blocked_seed_entity_ids'])
        entities, topics = [], []
        debug = {'keyword_query': text, 'keyword_terms': terms, 'keyword_entity_ids': [],
                 'keyword_topic_ids': [], 'vector_entity_ids': [],
                 'entity_name_only_enabled': self.settings.graph_entity_name_only_enabled,
                 'keyword_entity_ids_before_filter': list(dict.fromkeys(
                     r['matched_entity_id'] for r in rows if r['matched_entity_id'] and r['matched_entity_id'] not in blocked))}
        evidence = {}
        if self.settings.graph_entity_name_only_enabled:
            try:
                evidence = self.store.name_evidence(rows, terms)
            except Exception as exc:
                debug['keyword_entity_filter_error'] = type(exc).__name__
            debug.update(keyword_entity_evidence=[], keyword_entity_rejected_ids=[])
        for row in rows:
            eid, tid = row['matched_entity_id'], row['matched_topic_id']
            hits = evidence.get((row['memory_id'], eid), [])
            if self.settings.graph_entity_name_only_enabled and eid:
                if hits:
                    debug['keyword_entity_evidence'].append({'entity_id': eid, 'memory_id': row['memory_id'], 'hits': hits})
                else:
                    debug['keyword_entity_rejected_ids'].append(eid)
                    eid = None
            if eid and eid not in entities and eid not in blocked:
                entities.append(eid)
                debug['keyword_entity_ids'].append(eid)
            if tid and tid not in topics:
                topics.append(tid)
                debug['keyword_topic_ids'].append(tid)
        for row in self.store.search_entities(vector, limit=10):
            if row['similarity'] >= 0.30 and row['id'] not in entities and row['id'] not in blocked:
                entities.append(row['id'])
                debug['vector_entity_ids'].append(row['id'])
        if self.settings.graph_entity_name_only_enabled:
            debug['keyword_entity_rejected_ids'] = sorted(set(debug['keyword_entity_rejected_ids']) - set(debug['keyword_entity_ids']))
        debug['selected_entity_ids'] = entities[:10]
        return entities[:10], topics[:10], debug

    def search(self, query, *, embedder, cleaner, diagnostics=None, check_time=None):
        if not isinstance(query, str):
            raise ValueError('查询必须为字符串')
        # Each call owns its policy snapshot. Avoid shared mutable request state.
        request = ThreeRouteSearch(self.store, self.settings, item_kind=self.item_kind,
                                   include_archived=self.include_archived, candidate_limit=self.candidate_limit)
        request.policy = self.store.policy()
        return request._search(query, embedder, cleaner, diagnostics, check_time)

    def _search(self, query, embedder, cleaner, diagnostics, check_time):
        stats = diagnostics if diagnostics is not None else {}
        stats.update(status='empty', routes={}, errors={}, policy_revision=self.policy['revision'])
        if not query.strip():
            stats.update(status='skipped', candidate_count=0)
            return []

        def attempt(name, operation, default):
            if check_time is not None:
                check_time()
            try:
                value = operation()
                stats['routes'][name] = 'ok' if value else 'empty'
                return value
            except RevisionConflict:
                raise
            except Exception as exc:
                stats['routes'][name] = 'error'
                stats['errors'][name] = type(exc).__name__
                return default

        if check_time is not None:
            check_time()
        cleaned = clean_query(query, self.policy, cleaner, stats)
        kw = attempt('keyword', lambda: self._keyword_hits(cleaned), {})
        def embedding():
            result = embed_for(embedder, query, purpose="query")
            self.store._vector(result)
            return result
        vector = attempt('embedding', embedding, None)
        vec, graph, seed_debug = {}, {}, {}
        if vector is not None:
            def vector_hits():
                return {r['id']: _make_route_hit(r['id'], r['similarity'], 'vector',
                    f"向量相似度{r['similarity']:.3f}", 'memory', metadata={'similarity': r['similarity']})
                    for r in self.store.search_vectors(vector, **self.route_options)}
            vec = attempt('vector', vector_hits, {})
            if self.settings.graph_enabled:
                seeds = attempt('graph_seeds', lambda: self._seeds(cleaned, vector), None)
                if seeds is not None:
                    entity_ids, topic_ids, seed_debug = seeds
                    stats['graph_seeds'] = seed_debug
                    if seed_debug.get('keyword_entity_filter_error'):
                        stats['errors']['keyword_entity_filter'] = seed_debug['keyword_entity_filter_error']
                    def graph_hits():
                        if not entity_ids and not topic_ids:
                            return {}
                        rows = self.store.search_graph(entity_ids, topic_ids, revision=self.policy['revision'], **self.route_options)
                        return {r['memory_id']: _make_route_hit(r['memory_id'], r['raw_score'], 'graph',
                            r['graph_reason'], r['graph_source'], hop_count=r['hop_count'],
                            seed_entity_ids=[r['seed_entity_id']] if r['seed_entity_id'] else [],
                            seed_topic_ids=[r['seed_topic_id']] if r['seed_topic_id'] else [],
                            metadata={'relation_type': r['relation_type'], 'neighbor_entity_id': r['neighbor_entity_id']}) for r in rows}
                    graph = attempt('graph', graph_hits, {})
                else:
                    stats['routes']['graph'] = 'skipped'
            else:
                stats['routes']['graph'] = 'disabled'
        else:
            stats['routes'].update(vector='skipped', graph='skipped')
        stats['candidate_counts'] = {'keyword': len(kw), 'vector': len(vec), 'graph': len(graph)}
        ranked = merge_and_rank(kw, vec, graph, top_k=self.candidate_limit)
        if ranked:
            cards = attempt('hydrate', lambda: self.store.cards([r['memory_id'] for r in ranked]), {})
            ranked = [{**r, **cards[r['memory_id']], 'seed_debug': seed_debug} for r in ranked
                      if r['memory_id'] in cards and cards[r['memory_id']]['content']
                      and (self.item_kind == 'all' or cards[r['memory_id']]['item_kind'] == self.item_kind)]
        if check_time is not None:
            check_time()
        self.store.check_revision(self.policy['revision'])
        stats['candidate_count'] = len(ranked)
        stats['status'] = ('degraded' if stats['errors'] else 'ok') if ranked else ('failed' if stats['errors'] else 'empty')
        return ranked
