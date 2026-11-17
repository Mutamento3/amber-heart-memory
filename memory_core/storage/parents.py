"""Read existing summary decisions. No gardener execution or candidate refill."""


def replace_parents(store, candidates, diagnostics, *, check_time=None):
    counts = dict(input_count=len(candidates), output_count=len(candidates), replaced_count=0,
                  archived_dropped_count=0, merged_group_count=0, missing_parent_count=0, matched_children={})
    diagnostics['gardener'] = counts
    if not candidates:
        return []
    if check_time is not None:
        check_time()
    try:
        states = store.cards([r['memory_id'] for r in candidates])
    except Exception as exc:
        diagnostics.setdefault('errors', {})['gardener_states'] = type(exc).__name__
        return candidates
    parents = {}
    parent_ids = sorted({r['parent_reflection_id'] for r in states.values() if r['parent_reflection_id'] is not None})
    if parent_ids:
        if check_time is not None:
            check_time()
        try:
            parents = {mid: row for mid, row in store.cards(parent_ids).items()
                       if row['item_kind'] == 'topic_summary' and row['content'].strip()}
        except Exception as exc:
            diagnostics.setdefault('errors', {})['gardener_parents'] = type(exc).__name__
    output = {}
    for candidate in candidates:
        child_id = candidate['memory_id']
        state = states.get(child_id, {})
        parent_id = state.get('parent_reflection_id')
        if parent_id is not None:
            parent = parents.get(parent_id)
            if parent is not None and parent.get('archive_reason') is not None:
                counts['archived_dropped_count'] += 1
                continue
            if parent is None:
                counts['missing_parent_count'] += 1
                row = dict(candidate)
            else:
                row = {**candidate, **parent, 'memory_id': parent_id}
                counts['replaced_count'] += 1
                counts['matched_children'].setdefault(str(parent_id), []).append(child_id)
        elif state.get('archive_reason') is not None:
            counts['archived_dropped_count'] += 1
            continue
        else:
            row = dict(candidate)
        mid = row['memory_id']
        previous = output.get(mid)
        if previous is None or (row.get('final_score') or 0) > (previous.get('final_score') or 0):
            output[mid] = row
    rows = sorted(output.values(), key=lambda r: r.get('final_score') or 0, reverse=True)
    counts['merged_group_count'] = sum(len(ids) > 1 for ids in counts['matched_children'].values())
    counts['output_count'] = len(rows)
    return rows
