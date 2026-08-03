from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


STOPWORDS = {
    "и",
    "в",
    "во",
    "на",
    "по",
    "а",
    "но",
    "или",
    "для",
    "про",
    "можно",
    "ли",
    "что",
    "это",
    "как",
    "где",
    "есть",
    "вас",
    "вам",
    "кто",
    "the",
    "and",
    "or",
}

KB_MANIFEST_FILENAME = "kb_manifest.json"
KB_MANIFEST_EXTENSION_GLOB = "kb_manifest_*.json"
KB_ONTOLOGY_FILENAME = "kb_ontology.json"


@dataclass(frozen=True)
class KnowledgeChunk:
    source: str
    heading: str
    text: str
    score: int
    metadata: Dict[str, Any] | None = None


@dataclass(frozen=True)
class KnowledgeRouteMatch:
    pack_id: str
    source: str
    score: int
    source_hints: List[str]


def retrieve_knowledge_chunks(
    scope: Iterable[str],
    query: str,
    kb_root: str = "knowledge_base",
    limit: int = 3,
) -> List[KnowledgeChunk]:
    """Найти в базе знаний куски, relevant запросу.

    В исходном контуре список файлов брался из ``campaigns.knowledge_base_scope``
    — колонки, которой у нас нет. Здесь он приходит параметром: поиск по базе
    не зависит от того, где хранится её состав, а без обращения к БД модуль
    перестаёт тянуть за собой чужую схему.
    """
    root = Path(kb_root).resolve()
    manifest = load_kb_manifest(root)
    expanded_query = expand_query_with_ontology(query, root)
    query_tokens = tokenize_terms(expanded_query)
    query_terms = set(query_tokens)
    raw_chunks: List[tuple[str, str, str, Dict[str, Any], str]] = []
    for rel_path in scope:
        path = resolve_knowledge_path(root, str(rel_path))
        if path is None:
            continue
        if not path.exists() or not path.is_file():
            continue
        source = str(path.relative_to(root))
        metadata = source_metadata(manifest, source)
        for heading, text in split_markdown_chunks(path.read_text(encoding="utf-8")):
            raw_chunks.append(
                (
                    source,
                    heading,
                    text,
                    metadata,
                    searchable_chunk_text(heading, text, metadata),
                )
            )
    if not raw_chunks:
        return []

    corpus_tokens = [tokenize_terms(searchable_text) for *_rest, searchable_text in raw_chunks]
    avgdl = sum(len(tokens) for tokens in corpus_tokens) / max(len(corpus_tokens), 1)
    document_frequency = document_frequencies(corpus_tokens, query_terms)

    chunks: List[KnowledgeChunk] = []
    for index, (source, heading, text, metadata, searchable_text) in enumerate(raw_chunks):
        score = score_chunk_hybrid(
            query_tokens=query_tokens,
            searchable_text=searchable_text,
            metadata=metadata,
            document_tokens=corpus_tokens[index],
            document_frequency=document_frequency,
            total_documents=len(raw_chunks),
            average_document_length=avgdl,
        )
        if score <= 0:
            continue
        chunks.append(
            KnowledgeChunk(
                source=source,
                heading=heading,
                text=normalize_chunk_text(text),
                score=score,
                metadata=metadata,
            )
        )
    return sorted(chunks, key=lambda item: (-item.score, item.source, item.heading))[:limit]


def load_kb_manifest(kb_root: str | Path = "knowledge_base") -> Dict[str, Any]:
    root = Path(kb_root).resolve()
    path = root / KB_MANIFEST_FILENAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    manifest = raw if isinstance(raw, dict) else {}
    for extension_path in sorted(root.glob(KB_MANIFEST_EXTENSION_GLOB)):
        try:
            extension = json.loads(extension_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(extension, dict):
            manifest = merge_manifest_dicts(manifest, extension)
    return manifest


def merge_manifest_dicts(base: Dict[str, Any], extension: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, value in extension.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            merged = dict(result[key])
            merged.update(value)
            result[key] = merged
        elif key == "required_source_metadata_fields" and isinstance(value, list):
            existing = result.get(key) if isinstance(result.get(key), list) else []
            result[key] = [*existing, *[item for item in value if item not in existing]]
        elif key not in result:
            result[key] = value
    return result


def source_metadata(manifest: Dict[str, Any], source: str) -> Dict[str, Any]:
    sources = manifest.get("sources")
    if not isinstance(sources, dict):
        return {}
    raw = sources.get(source)
    if not isinstance(raw, dict):
        return {}
    metadata = dict(raw)
    metadata.setdefault("usage_type", infer_usage_type(source, metadata))
    return metadata


def infer_usage_type(source: str, metadata: Dict[str, Any]) -> str:
    explicit = metadata.get("usage_type")
    if isinstance(explicit, str) and explicit:
        return explicit
    topic_values = metadata.get("topic") if isinstance(metadata.get("topic"), list) else []
    topics = {str(item) for item in topic_values}
    audience = str(metadata.get("audience") or "")
    risk = str(metadata.get("risk") or "")
    if "source_coverage" in topics or "audit" in topics or source.startswith("source_"):
        return "source_audit"
    if "handoff" in topics:
        return "handoff_rule"
    if risk == "high" or "guardrails" in topics:
        return "safety_rule"
    if audience == "customer_llm":
        return "reply_fact"
    return "reasoning_context"


def answer_pack_matches_for_query(
    kb_root: str | Path,
    query: str,
    pack_ids: Optional[Iterable[str]] = None,
    limit: int = 4,
) -> List[KnowledgeRouteMatch]:
    return route_matches_for_query(
        kb_root=kb_root,
        query=query,
        collection_key="answer_packs",
        allowed_ids=pack_ids,
        limit=limit,
    )


def answer_card_matches_for_query(
    kb_root: str | Path,
    query: str,
    card_ids: Optional[Iterable[str]] = None,
    limit: int = 4,
) -> List[KnowledgeRouteMatch]:
    return route_matches_for_query(
        kb_root=kb_root,
        query=query,
        collection_key="answer_cards",
        allowed_ids=card_ids,
        limit=limit,
    )


def route_matches_for_query(
    kb_root: str | Path,
    query: str,
    collection_key: str,
    allowed_ids: Optional[Iterable[str]] = None,
    limit: int = 4,
) -> List[KnowledgeRouteMatch]:
    manifest = load_kb_manifest(kb_root)
    collection = manifest.get(collection_key)
    if not isinstance(collection, dict):
        return []
    allowed = set(allowed_ids) if allowed_ids is not None else None
    expanded_query = expand_query_with_ontology(query, kb_root)
    query_terms = tokenize(expanded_query)
    query_lowered = expanded_query.lower()
    matches: List[KnowledgeRouteMatch] = []
    for item_id, raw_item in collection.items():
        if allowed is not None and item_id not in allowed:
            continue
        if not isinstance(raw_item, dict):
            continue
        source = str(raw_item.get("source") or "").strip()
        if not source:
            continue
        terms = clean_string_list(raw_item.get("terms"))
        term_tokens = set()
        substring_hits = 0
        for term in terms:
            term_lowered = term.lower()
            term_tokens.update(tokenize(term_lowered))
            if term_lowered and term_lowered in query_lowered:
                substring_hits += 1
        overlap = len(query_terms & term_tokens)
        if overlap <= 0 and substring_hits <= 0:
            continue
        score = overlap * 10 + substring_hits * 15
        metadata = source_metadata(manifest, source)
        source_meta_terms = tokenize(metadata_search_text(metadata))
        score += len(query_terms & source_meta_terms) * 4
        if score <= 0:
            continue
        matches.append(
            KnowledgeRouteMatch(
                pack_id=str(item_id),
                source=source,
                score=score,
                source_hints=clean_string_list(raw_item.get("source_hints")),
            )
        )
    return sorted(matches, key=lambda item: (-item.score, item.pack_id))[:limit]


def answer_pack_sources_for_query(
    kb_root: str | Path,
    query: str,
    pack_ids: Optional[Iterable[str]] = None,
    limit: int = 4,
) -> List[str]:
    result: List[str] = []
    for match in answer_pack_matches_for_query(kb_root, query, pack_ids=pack_ids, limit=limit):
        if match.source not in result:
            result.append(match.source)
    return result


def answer_card_sources_for_query(
    kb_root: str | Path,
    query: str,
    card_ids: Optional[Iterable[str]] = None,
    limit: int = 4,
) -> List[str]:
    result: List[str] = []
    for match in answer_card_matches_for_query(kb_root, query, card_ids=card_ids, limit=limit):
        if match.source not in result:
            result.append(match.source)
    return result


def load_kb_ontology(kb_root: str | Path = "knowledge_base") -> Dict[str, Any]:
    root = Path(kb_root).resolve()
    path = root / KB_ONTOLOGY_FILENAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def expand_query_with_ontology(query: str, kb_root: str | Path = "knowledge_base") -> str:
    ontology = load_kb_ontology(kb_root)
    topics = ontology.get("topics")
    if not isinstance(topics, dict):
        return query
    query_lowered = query.lower()
    query_terms = tokenize(query)
    expansions: List[str] = []
    for topic_id, raw_topic in topics.items():
        if not isinstance(raw_topic, dict):
            continue
        canonical_terms = clean_string_list(raw_topic.get("canonical_terms"))
        aliases = clean_string_list(raw_topic.get("aliases"))
        matched = False
        for alias in aliases:
            alias_lowered = alias.lower()
            if alias_lowered and alias_lowered in query_lowered:
                matched = True
                break
        if not matched:
            canonical_token_set = set()
            for term in canonical_terms:
                canonical_token_set.update(tokenize(term))
            matched = bool(query_terms & canonical_token_set)
        if not matched:
            continue
        expansions.append(str(topic_id))
        expansions.extend(canonical_terms)
        expansions.extend(clean_string_list(raw_topic.get("expand_with")))
    if not expansions:
        return query
    return " ".join([query, *dedupe_strings(expansions)])


def resolve_knowledge_path(root: Path, rel_path: str) -> Path | None:
    relative = Path(rel_path)
    # Some launch artifacts store repo-relative entries such as
    # ``knowledge_base/faq.md`` while retrieval already receives
    # ``knowledge_base`` as its root.  Treat that single, matching prefix as
    # canonical instead of silently looking under knowledge_base/knowledge_base.
    if not relative.is_absolute() and relative.parts[:1] == (root.name,):
        relative = Path(*relative.parts[1:])
    path = (root / relative).resolve()
    if not is_inside_root(root, path):
        return None
    if path.exists() or path.suffix:
        return path
    md_path = path.with_suffix(".md").resolve()
    if not is_inside_root(root, md_path):
        return None
    return md_path


def is_inside_root(root: Path, path: Path) -> bool:
    return path == root or root in path.parents


def split_markdown_chunks(text: str) -> Iterable[tuple[str, str]]:
    current_heading = ""
    buffer: List[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            if buffer:
                yield current_heading, "\n".join(buffer).strip()
            current_heading = line.lstrip("#").strip()
            buffer = []
        else:
            buffer.append(line)
    if buffer:
        yield current_heading, "\n".join(buffer).strip()


def tokenize(text: str) -> set[str]:
    return set(tokenize_terms(text))


def tokenize_terms(text: str) -> List[str]:
    tokens = [
        token
        for token in re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9]{3,}", text.lower())
        if token not in STOPWORDS
    ]
    expanded: List[str] = []
    for token in tokens:
        expanded.append(token)
        if len(token) > 5:
            expanded.append(token[:5])
    return expanded


def score_chunk(query_terms: set[str], text: str) -> int:
    if not query_terms:
        return 0
    text_terms = tokenize(text)
    score = len(query_terms & text_terms) * 3
    lowered = text.lower()
    for term in query_terms:
        if term in lowered:
            score += 1
    return score


def document_frequencies(
    corpus_tokens: List[List[str]],
    query_terms: set[str],
) -> Dict[str, int]:
    result = {term: 0 for term in query_terms}
    for tokens in corpus_tokens:
        token_set = set(tokens)
        for term in query_terms:
            if term in token_set:
                result[term] += 1
    return result


def score_chunk_hybrid(
    query_tokens: List[str],
    searchable_text: str,
    metadata: Dict[str, Any],
    document_tokens: List[str],
    document_frequency: Dict[str, int],
    total_documents: int,
    average_document_length: float,
) -> int:
    if not query_tokens:
        return 0
    query_terms = set(query_tokens)
    token_counts = Counter(document_tokens)
    document_length = max(len(document_tokens), 1)
    average_document_length = max(average_document_length, 1.0)
    bm25 = 0.0
    k1 = 1.4
    b = 0.72
    for term in query_terms:
        tf = token_counts.get(term, 0)
        if not tf:
            continue
        df = max(document_frequency.get(term, 0), 0)
        idf = math.log(1 + (total_documents - df + 0.5) / (df + 0.5))
        denominator = tf + k1 * (1 - b + b * document_length / average_document_length)
        bm25 += idf * (tf * (k1 + 1) / denominator)

    lowered = searchable_text.lower()
    substring_bonus = sum(1 for term in query_terms if term in lowered)
    metadata_bonus = metadata_score(query_terms, metadata)
    if bm25 <= 0 and substring_bonus <= 0 and metadata_bonus <= 0:
        return 0
    priority = metadata_priority(metadata)
    priority_bonus = priority / 5 if priority else 0
    score = bm25 * 10 + substring_bonus + metadata_bonus + priority_bonus
    return max(1, int(round(score)))


def metadata_score(query_terms: set[str], metadata: Dict[str, Any]) -> int:
    if not metadata:
        return 0
    metadata_terms = tokenize(metadata_search_text(metadata))
    if not metadata_terms:
        return 0
    return len(query_terms & metadata_terms) * 5


def metadata_search_text(metadata: Dict[str, Any]) -> str:
    values: List[str] = []
    for key in ("topic", "intent", "audience", "risk", "usage_type", "source_url", "tags"):
        raw = metadata.get(key)
        if isinstance(raw, list):
            values.extend(str(item) for item in raw)
        elif raw is not None:
            values.append(str(raw))
    return " ".join(values)


def metadata_priority(metadata: Dict[str, Any]) -> int:
    try:
        return int(metadata.get("priority") or 0)
    except (TypeError, ValueError):
        return 0


def searchable_chunk_text(heading: str, text: str, metadata: Dict[str, Any]) -> str:
    return "\n".join([heading, text, metadata_search_text(metadata)]).strip()


def clean_string_list(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


def dedupe_strings(values: List[str]) -> List[str]:
    result: List[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def normalize_chunk_text(text: str, max_chars: int = 420) -> str:
    value = re.sub(r"\s+", " ", text).strip()
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1].rstrip() + "..."
