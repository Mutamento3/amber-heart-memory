"""Existing conditional keyword cleaning; embeddings always keep the raw query."""

import json
from pathlib import Path

from .keyword_index import EDGE_CHARS, STOPWORDS, clean_address_terms, normalize_token, tokenize


def keyword_prompt(query, policy):
    data = {'participants': [{k:p[k] for k in ('name','aliases')} for p in policy['participants'].values()],
            'ignored_terms': policy['ignored_terms'], 'query': query}
    return (Path(__file__).with_name('prompts')/'keywords.md').read_text().replace(
        '{{INPUT}}',json.dumps(data,ensure_ascii=False))


def strip_addresses(query, ignored_terms):
    return clean_address_terms(query, ignored_terms).strip(EDGE_CHARS + " ")


def keyword_query(query, ignored_terms):
    candidates = tokenize(query, ignored_terms)
    terms = list(dict.fromkeys(candidates))
    positions = {term: candidates.index(term) for term in terms}
    terms.sort(key=lambda term: (-len(term), positions[term]))
    terms = terms[:6]
    if not terms:
        fallback = normalize_token(strip_addresses(query, ignored_terms))
        if not fallback or fallback in STOPWORDS or fallback in {normalize_token(t) for t in ignored_terms}:
            return "", []
        return fallback, [fallback]
    return " OR ".join(terms), terms


def clean_query(query, policy, cleaner, diagnostics):
    ignored = policy["ignored_terms"]
    deterministic = strip_addresses(query, ignored)
    contains_address = clean_address_terms(query, ignored) != " ".join(query.split())
    if len(query) <= 15 and not contains_address:
        diagnostics["cleaning"] = "skipped"
        return query
    try:
        result = cleaner.clean_query(query, prompt=keyword_prompt(query,policy))
        if not isinstance(result, str):
            raise ValueError("invalid keyword response")
        result = strip_addresses(result, ignored)
        if len(result) >= 2:
            diagnostics["cleaning"] = "ok"
            return result
        diagnostics["cleaning"] = "short_output_fallback"
    except Exception as exc:
        diagnostics["cleaning"] = "failed_fallback"
        diagnostics.setdefault("errors", {})["cleaning"] = type(exc).__name__
    return deterministic
