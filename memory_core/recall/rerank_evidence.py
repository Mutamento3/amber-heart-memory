"""Bounded matched-child evidence for reranking only, never injection."""

def add_rerank_evidence(candidates, source_candidates, diagnostics, settings):
    """Attach bounded, actually matched source text for reranking only; no scoring changes."""
    enabled = settings.evidence_enabled
    limits = {name: getattr(settings, 'evidence_' + name) for name in (
        'max_children','max_title_chars','max_summary_chars','max_total_chars')}
    stats = {"enabled": enabled, **limits, "summary_count": 0, "total_chars": 0,
             "per_summary": {}}
    diagnostics["rerank_evidence"] = stats
    if not enabled or not all(limits.values()):
        return candidates

    sources = {row["memory_id"]: row for row in source_candidates}
    matched = diagnostics.get("gardener", {}).get("matched_children", {})
    output = []
    for candidate in candidates:
        mid = candidate["memory_id"]
        children = [sources[cid] for cid in matched.get(str(mid), []) if cid in sources]
        children.sort(key=lambda row: row.get("final_score") or 0, reverse=True)
        lines, child_ids = [], []
        if candidate.get("item_kind") == "topic_summary":
            for child in children[:limits["max_children"]]:
                title = (child.get("title") or "").strip()[:limits["max_title_chars"]]
                excerpt = " ".join((child.get("summary") or child.get("content") or "").split())
                excerpt = excerpt[:limits["max_summary_chars"]]
                prefix = f"来源 M-{child['memory_id']}｜{title}："
                remaining = limits["max_total_chars"] - len("\n".join(lines)) - bool(lines)
                if remaining < len(prefix) + 10:
                    break
                lines.append((prefix + excerpt)[:remaining])
                child_ids.append(child["memory_id"])
        text = "\n".join(lines)
        if text:
            candidate = {**candidate, "_rerank_evidence": text}
            stats["per_summary"][str(mid)] = {
                "child_count": len(child_ids), "chars": len(text), "child_ids": child_ids,
            }
            stats["summary_count"] += 1
            stats["total_chars"] += len(text)
        output.append(candidate)
    return output
