from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KB_ROOT = ROOT / "knowledge_base"
DEFAULT_MANIFEST_PATH = DEFAULT_KB_ROOT / "customer_truth_sources_v2.json"
MAX_SOURCE_CHARACTERS = 250_000
MAX_PACK_CHARACTERS = 500_000
MAX_RUNTIME_CATALOG_CHARACTERS = 65_000


@dataclass(frozen=True)
class CustomerTruthSource:
    source_id: str
    relative_path: str
    authority: str
    text: str
    sha256: str

    def as_prompt_reference(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "path": self.relative_path,
            "authority": self.authority,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class CustomerTruthFact:
    fact_id: str
    topic: str
    text: str
    conditions: str
    source_ids: tuple[str, ...]

    def as_prompt_fact(self) -> dict[str, object]:
        return {
            "fact_id": self.fact_id,
            "topic": self.topic,
            "text": self.text,
            "conditions": self.conditions,
            "source_ids": list(self.source_ids),
        }


@dataclass(frozen=True)
class CustomerTruthPack:
    version: int
    sources: tuple[CustomerTruthSource, ...]
    facts: tuple[CustomerTruthFact, ...]
    sha256: str
    source_pack_sha256: str
    total_characters: int
    runtime_characters: int

    @property
    def source_ids(self) -> frozenset[str]:
        return frozenset(source.source_id for source in self.sources)

    @property
    def source_paths(self) -> tuple[str, ...]:
        return tuple(source.relative_path for source in self.sources)

    def as_prompt_payload(self) -> dict[str, object]:
        return {
            "version": self.version,
            "sha256": self.sha256,
            "source_pack_sha256": self.source_pack_sha256,
            "source_count": len(self.sources),
            "fact_count": len(self.facts),
            "runtime_characters": self.runtime_characters,
            "source_catalog": [
                source.source_id for source in self.sources
            ],
            "facts": [fact.as_prompt_fact() for fact in self.facts],
        }


def load_customer_truth_pack(
    *,
    manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
    kb_root: str | Path = DEFAULT_KB_ROOT,
) -> CustomerTruthPack:
    manifest_file = Path(manifest_path).resolve()
    root = Path(kb_root).resolve()
    raw = _load_manifest(manifest_file)
    version = _positive_int(raw.get("version"), field="version")
    v1_sources = _clean_unique_paths(raw.get("v1_sources"), field="v1_sources")
    supplements = _clean_unique_paths(
        raw.get("supplement_sources"),
        field="supplement_sources",
    )
    forbidden = set(
        _clean_unique_paths(
            raw.get("never_include_in_runtime_prompt"),
            field="never_include_in_runtime_prompt",
            allow_empty=True,
        )
    )
    overlap = forbidden.intersection([*v1_sources, *supplements])
    if overlap:
        raise ValueError(
            "Truth-pack manifest includes forbidden runtime sources: "
            + ", ".join(sorted(overlap))
        )

    documents: list[CustomerTruthSource] = []
    seen_ids: set[str] = set()
    for authority, paths in (
        ("v1_curated", v1_sources),
        ("v2_curated_supplement", supplements),
    ):
        for relative_path in paths:
            path = _resolve_inside(root, relative_path)
            if not path.exists() or not path.is_file():
                raise ValueError(f"Truth-pack source is missing: {relative_path}")
            text = path.read_text(encoding="utf-8").strip()
            if not text:
                raise ValueError(f"Truth-pack source is empty: {relative_path}")
            if len(text) > MAX_SOURCE_CHARACTERS:
                raise ValueError(
                    f"Truth-pack source is too large: {relative_path} ({len(text)} chars)"
                )
            source_id = _source_id(authority, relative_path)
            if source_id in seen_ids:
                raise ValueError(f"Duplicate truth-pack source id: {source_id}")
            seen_ids.add(source_id)
            documents.append(
                CustomerTruthSource(
                    source_id=source_id,
                    relative_path=relative_path,
                    authority=authority,
                    text=text,
                    sha256=_sha256_text(text),
                )
            )

    total_characters = sum(len(source.text) for source in documents)
    if total_characters > MAX_PACK_CHARACTERS:
        raise ValueError(
            f"Truth pack is too large: {total_characters} > {MAX_PACK_CHARACTERS}"
        )
    source_canonical = {
        "version": version,
        "sources": [
            {
                "source_id": source.source_id,
                "relative_path": source.relative_path,
                "authority": source.authority,
                "sha256": source.sha256,
                "text": source.text,
            }
            for source in documents
        ],
    }
    source_pack_hash = _sha256_text(
        json.dumps(
            source_canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    runtime_catalog_value = str(raw.get("runtime_catalog") or "").strip()
    if runtime_catalog_value:
        runtime_catalog_file = _resolve_inside(root, runtime_catalog_value)
        facts = _load_runtime_catalog(
            runtime_catalog_file,
            valid_source_ids=frozenset(source.source_id for source in documents),
        )
    else:
        # Small custom manifests used by tests and local tools retain the old
        # semantics. The production manifest always pins a curated compact
        # catalog, so full source prose is never sent in the live v2 prompt.
        facts = tuple(
            CustomerTruthFact(
                fact_id=f"LEGACY_{index:03d}",
                topic="legacy_source",
                text=source.text,
                conditions="",
                source_ids=(source.source_id,),
            )
            for index, source in enumerate(documents, start=1)
        )
    runtime_payload = [fact.as_prompt_fact() for fact in facts]
    runtime_characters = len(
        json.dumps(
            runtime_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    if runtime_characters > MAX_RUNTIME_CATALOG_CHARACTERS:
        raise ValueError(
            "Runtime truth catalog is too large: "
            f"{runtime_characters} > {MAX_RUNTIME_CATALOG_CHARACTERS}"
        )
    pack_hash = _sha256_text(
        json.dumps(
            {
                "version": version,
                "source_pack_sha256": source_pack_hash,
                "source_catalog": [
                    source.as_prompt_reference() for source in documents
                ],
                "facts": runtime_payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return CustomerTruthPack(
        version=version,
        sources=tuple(documents),
        facts=facts,
        sha256=pack_hash,
        source_pack_sha256=source_pack_hash,
        total_characters=total_characters,
        runtime_characters=runtime_characters,
    )


def validate_source_references(
    pack: CustomerTruthPack,
    source_ids: Iterable[object],
) -> tuple[list[str], list[str]]:
    valid_ids = pack.source_ids
    valid: list[str] = []
    invalid: list[str] = []
    for raw in source_ids:
        source_id = str(raw or "").strip()
        if not source_id:
            continue
        target = valid if source_id in valid_ids else invalid
        if source_id not in target:
            target.append(source_id)
    return valid, invalid


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Truth-pack manifest is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Truth-pack manifest is invalid JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Truth-pack manifest must be a JSON object")
    return raw


def _load_runtime_catalog(
    path: Path,
    *,
    valid_source_ids: frozenset[str],
) -> tuple[CustomerTruthFact, ...]:
    raw = _load_manifest(path)
    raw_facts = raw.get("facts")
    if not isinstance(raw_facts, list) or not raw_facts:
        raise ValueError("Runtime truth catalog facts must be a non-empty list")
    result: list[CustomerTruthFact] = []
    seen_fact_ids: set[str] = set()
    cited_source_ids: set[str] = set()
    for index, item in enumerate(raw_facts, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Runtime truth fact #{index} must be an object")
        fact_id = str(item.get("fact_id") or "").strip()
        topic = str(item.get("topic") or "").strip()
        text = str(item.get("text") or "").strip()
        conditions = str(item.get("conditions") or "").strip()
        source_ids = tuple(
            _clean_unique_strings(item.get("source_ids"), field=f"{fact_id}.source_ids")
        )
        if not fact_id or fact_id in seen_fact_ids:
            raise ValueError(f"Invalid or duplicate runtime fact id: {fact_id!r}")
        if not topic or not text:
            raise ValueError(f"Runtime truth fact is incomplete: {fact_id}")
        unknown = sorted(set(source_ids).difference(valid_source_ids))
        if unknown:
            raise ValueError(
                f"Runtime truth fact {fact_id} has unknown source ids: "
                + ", ".join(unknown)
            )
        seen_fact_ids.add(fact_id)
        cited_source_ids.update(source_ids)
        result.append(
            CustomerTruthFact(
                fact_id=fact_id,
                topic=topic,
                text=text,
                conditions=conditions,
                source_ids=source_ids,
            )
        )
    missing = sorted(valid_source_ids.difference(cited_source_ids))
    if missing:
        raise ValueError(
            "Runtime truth catalog does not preserve provenance for sources: "
            + ", ".join(missing)
        )
    return tuple(result)


def _clean_unique_paths(
    value: object,
    *,
    field: str,
    allow_empty: bool = False,
) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    result: list[str] = []
    for item in value:
        relative_path = str(item or "").strip().replace("\\", "/")
        if not relative_path:
            raise ValueError(f"{field} contains an empty path")
        if relative_path.startswith("/") or ".." in Path(relative_path).parts:
            raise ValueError(f"{field} contains an unsafe path: {relative_path}")
        if relative_path not in result:
            result.append(relative_path)
    if not result and not allow_empty:
        raise ValueError(f"{field} must not be empty")
    return result


def _clean_unique_strings(value: object, *, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty list")
    result: list[str] = []
    for item in value:
        clean = str(item or "").strip()
        if not clean:
            raise ValueError(f"{field} contains an empty value")
        if clean not in result:
            result.append(clean)
    return result


def _resolve_inside(root: Path, relative_path: str) -> Path:
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Truth-pack path escapes knowledge root: {relative_path}") from exc
    return candidate


def _source_id(authority: str, relative_path: str) -> str:
    prefix = "v1" if authority == "v1_curated" else "v2"
    return f"{prefix}:{relative_path}"


def _positive_int(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if result <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return result


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
