"""Content identity is different from recipe identity; preserve provenance."""
import hashlib
import json


def content_key(doc):
    metadata = doc.metadata
    # Identical chunks from separate retrievers should collapse, but an entity
    # summary must never suppress a different chunk belonging to the same recipe.
    digest = hashlib.sha256(doc.page_content.strip().encode('utf-8')).hexdigest()
    owner = metadata.get('parent_id') or metadata.get('recipe_id') or metadata.get('node_id') or ''
    return str(owner), digest


def evidence_context(documents):
    parts = []
    for doc in documents:
        content = doc.page_content.strip()
        if not content:
            continue
        meta = doc.metadata
        source = {key: meta[key] for key in (
            'recipe_name', 'recipe_id', 'node_id', 'parent_id', 'chunk_id',
            'node_type', 'source', 'source_type', 'retrieval_level', 'search_method') if key in meta}
        parts.append('[资料来源 ' + json.dumps(source, ensure_ascii=False) + ']\n' + content)
    return '\n\n'.join(parts)
