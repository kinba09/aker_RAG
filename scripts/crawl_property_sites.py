#!/usr/bin/env python3
"""Crawl verified property websites and upsert chunks to Qdrant.

Minimal first version:
- Reads verified sources from MySQL
- Fetches homepage HTML
- Strips text
- Chunks text
- Upserts metadata-scoped chunks into Qdrant collection
"""
import argparse
import hashlib
import re
from datetime import datetime, timezone

import mysql.connector
import requests
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams


def simple_clean(html: str) -> str:
    html = re.sub(r'<script[\s\S]*?</script>', ' ', html, flags=re.I)
    html = re.sub(r'<style[\s\S]*?</style>', ' ', html, flags=re.I)
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def chunk_text(text: str, chunk_size: int = 900, overlap: int = 150):
    out = []
    i = 0
    while i < len(text):
        out.append(text[i:i + chunk_size])
        i += max(1, chunk_size - overlap)
    return out


def fake_embed(text: str, dim: int = 32):
    # Temporary deterministic vector until embedding model wiring is added.
    h = hashlib.sha256(text.encode('utf-8')).digest()
    vals = [b / 255.0 for b in h[:dim]]
    if len(vals) < dim:
        vals.extend([0.0] * (dim - len(vals)))
    return vals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db-host', default='127.0.0.1')
    ap.add_argument('--db-port', type=int, default=3306)
    ap.add_argument('--db-user', default='root')
    ap.add_argument('--db-password', required=True)
    ap.add_argument('--db-name', default='property_chatbot')
    ap.add_argument('--qdrant-url', default='http://127.0.0.1:6333')
    ap.add_argument('--collection', default='property_website_chunks')
    args = ap.parse_args()

    conn = mysql.connector.connect(host=args.db_host, port=args.db_port, user=args.db_user, password=args.db_password, database=args.db_name)
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT property_code, property_name, website_url FROM property_web_sources WHERE verified=1 AND website_url IS NOT NULL")
    sources = cur.fetchall()

    q = QdrantClient(url=args.qdrant_url)
    try:
        q.get_collection(args.collection)
    except Exception:
        q.create_collection(args.collection, vectors_config=VectorParams(size=32, distance=Distance.COSINE))

    upserts = 0
    for s in sources:
        url = s['website_url']
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            text = simple_clean(resp.text)
        except Exception:
            continue

        chunks = chunk_text(text)
        points = []
        for idx, ch in enumerate(chunks):
            chunk_id = f"{s['property_code']}-{idx}"
            pid = int(hashlib.md5(chunk_id.encode()).hexdigest()[:12], 16)
            points.append(PointStruct(
                id=pid,
                vector=fake_embed(ch),
                payload={
                    'property_code': s['property_code'],
                    'property_name': s['property_name'],
                    'source_url': url,
                    'chunk_id': chunk_id,
                    'text': ch,
                    'crawled_at': datetime.now(timezone.utc).isoformat(),
                }
            ))
        if points:
            q.upsert(collection_name=args.collection, points=points)
            upserts += len(points)

    print(f'Upserted {upserts} chunks into {args.collection}.')


if __name__ == '__main__':
    main()
