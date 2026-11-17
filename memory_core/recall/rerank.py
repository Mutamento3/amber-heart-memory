"""Two-stage ordering/selection with the existing skip and fallback behavior."""

from ..models.http_transport import ModelCallError
from .rerank_input import format_document, ranking_query, selection_prompt


def order_candidates(query, candidates, model, *, full_list):
    stats = dict(total=len(candidates), selected=0, skipped=False, ordering_only=full_list)
    if not candidates or (not full_list and len(candidates)<=10):
        stats.update(selected=len(candidates), skipped=True)
        return candidates, stats
    limit = len(candidates) if full_list else 10
    try:
        # Injected rerankers without a format-specific hook keep the existing query.
        prepare_query = getattr(model, "prepare_rerank_query", ranking_query)
        output = model.rerank(prepare_query(query), [r['memory_id'] for r in candidates],
                             documents=[format_document(r) for r in candidates])
        if not isinstance(output, (list,tuple)):
            raise ValueError('invalid ordering output')
        positions = []
        for value in output:
            if type(value) is int and 0<=value<len(candidates) and value not in positions:
                positions.append(value)
                if len(positions)==limit:
                    break
        if not positions:
            stats.update(selected=len(candidates), skipped=True, error='EmptyDocumentRerank')
            return candidates, stats
        if full_list and len(positions)!=len(candidates):
            stats.update(selected=len(candidates), skipped=True, error='IncompleteDocumentRerank')
            return candidates, stats
        ordered = [candidates[i] for i in positions]
        stats.update(selected=len(ordered), ordered_ids=[r['memory_id'] for r in ordered])
        return ordered, stats
    except Exception as exc:
        code = exc.code if isinstance(exc, ModelCallError) else type(exc).__name__
        stats.update(selected=len(candidates), skipped=True, error=code)
        return candidates, stats


def select_candidates(query, candidates, model):
    stats = dict(total=len(candidates), selected=0, skipped=False)
    if len(candidates)<=5:
        stats.update(selected=len(candidates), skipped=True)
        return candidates, stats
    try:
        response = model.select(query, [r['memory_id'] for r in candidates], prompt=selection_prompt(query,candidates))
        if not isinstance(response,str):
            raise ValueError('invalid selection output')
        indices = []
        for part in response.replace('，',',').split(','):
            part = part.strip()
            if part.isdigit():
                index = int(part)-1
                if 0<=index<len(candidates) and index not in indices:
                    indices.append(index)
            if len(indices)==5:
                break
        stats['valid_selected'] = len(indices)
        if not response.strip() or not indices:
            stats['error'] = 'EmptyRerankOutput' if not response.strip() else 'InvalidRerankOutput'
        # Preserve legacy short-output fill. Full-list callers discard this
        # partial selection and fall back to the leading five instead.
        selected = [candidates[i] for i in indices]
        for i,row in enumerate(candidates):
            if len(selected)>=5:
                break
            if i not in indices:
                selected.append(row)
        stats['selected'] = len(selected)
        return selected, stats
    except Exception as exc:
        stats.update(selected=min(5,len(candidates)), error=type(exc).__name__)
        return candidates[:5], stats
