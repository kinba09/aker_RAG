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
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import mysql.connector
import requests
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_ollama import OllamaEmbeddings
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


def extract_links(html: str, base_url: str):
    links = re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.I)
    out = []
    for href in links:
        href = href.strip()
        if not href or href.startswith('#') or href.startswith('mailto:') or href.startswith('tel:'):
            continue
        full = urljoin(base_url, href)
        out.append(full)
    return out


def should_skip_url(u: str):
    lower = u.lower()
    blocked_ext = (
        '.pdf', '.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg',
        '.mp4', '.mov', '.avi', '.zip', '.css', '.js', '.woff',
        '.woff2', '.ttf', '.ico', '.xml', '.json'
    )
    return lower.endswith(blocked_ext)


def looks_like_noise(text: str):
    if len(text) < 120:
        return True
    letters = sum(ch.isalpha() for ch in text)
    ratio = letters / max(1, len(text))
    return ratio < 0.45


def build_embedder(args):
    provider = os.getenv("EMBEDDING_PROVIDER", "google").lower()
    if provider == "ollama":
        return OllamaEmbeddings(
            model=os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text-v2-moe"),
            base_url=os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434"),
        )
    return GoogleGenerativeAIEmbeddings(
        model=args.embedding_model,
        google_api_key=os.getenv("GOOGLE_API_KEY"),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db-host', default='127.0.0.1')
    ap.add_argument('--db-port', type=int, default=3306)
    ap.add_argument('--db-user', default='root')
    ap.add_argument('--db-password', required=True)
    ap.add_argument('--db-name', default='property_chatbot')
    ap.add_argument('--qdrant-url', default='http://127.0.0.1:6333')
    ap.add_argument('--collection', default='property_website_chunks')
    ap.add_argument('--max-depth', type=int, default=1, help='Crawl depth from seed URL (recommended 1-2)')
    ap.add_argument('--max-pages', type=int, default=25, help='Max pages to crawl per property')
    ap.add_argument('--reindex', action='store_true', help='Delete and recreate collection before upsert')
    ap.add_argument('--embedding-model', default=os.getenv("GOOGLE_EMBEDDING_MODEL", "models/embedding-001"))
    args = ap.parse_args()

    conn = mysql.connector.connect(host=args.db_host, port=args.db_port, user=args.db_user, password=args.db_password, database=args.db_name)
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT property_code, property_name, website_url FROM property_web_sources WHERE verified=1 AND website_url IS NOT NULL")
    sources = cur.fetchall()

    q = QdrantClient(url=args.qdrant_url)
    embedder = build_embedder(args)
    probe_vec = embedder.embed_query("property website chunk")
    dim = len(probe_vec)

    if args.reindex:
        try:
            q.delete_collection(args.collection)
        except Exception:
            pass

    try:
        q.get_collection(args.collection)
    except Exception:
        q.create_collection(args.collection, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))

    upserts = 0
    for s in sources:
        seed_url = s['website_url']
        seed_host = urlparse(seed_url).netloc.lower()
        queue = deque([(seed_url, 0)])
        visited = set()
        page_texts = []

        while queue and len(visited) < args.max_pages:
            url, depth = queue.popleft()
            if url in visited:
                continue
            if should_skip_url(url):
                continue
            if urlparse(url).netloc.lower() != seed_host:
                continue
            visited.add(url)

            try:
                resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 PropertyCrawler/1.0"})
                resp.raise_for_status()
                ctype = resp.headers.get("Content-Type", "").lower()
                if "text/html" not in ctype:
                    continue
                html = resp.text
            except Exception:
                continue

            text = simple_clean(html)
            if text and not looks_like_noise(text):
                page_texts.append((url, text))

            if depth < args.max_depth:
                for nxt in extract_links(html, url):
                    if nxt not in visited and urlparse(nxt).netloc.lower() == seed_host and not should_skip_url(nxt):
                        queue.append((nxt, depth + 1))

        points = []
        chunk_counter = 0
        for page_url, text in page_texts:
            chunks = chunk_text(text)
            for ch in chunks:
                if looks_like_noise(ch):
                    continue
                chunk_id = f"{s['property_code']}-{chunk_counter}"
                chunk_counter += 1
                pid = int(hashlib.md5(f"{s['property_code']}:{page_url}:{chunk_id}".encode()).hexdigest()[:12], 16)
                vec = None
                for attempt in range(3):
                    try:
                        vec = embedder.embed_query(ch)
                        break
                    except Exception:
                        time.sleep(2 + attempt * 2)
                if vec is None:
                    continue

                points.append(PointStruct(
                    id=pid,
                    vector=vec,
                    payload={
                        'property_code': s['property_code'],
                        'property_name': s['property_name'],
                        'source_url': page_url,
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
