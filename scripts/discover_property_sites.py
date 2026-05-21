#!/usr/bin/env python3
"""Seed/maintain property website mapping.

For now this is a controlled CSV/manual bootstrap workflow.
Later we can add automatic discovery/search scoring.
"""
import argparse
import csv
from pathlib import Path
import mysql.connector


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', required=True, help='CSV with columns: property_code,property_name,website_url,verified,confidence_score,discovery_notes')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=3306)
    ap.add_argument('--user', default='root')
    ap.add_argument('--password', required=True)
    ap.add_argument('--database', default='property_chatbot')
    args = ap.parse_args()

    rows = list(csv.DictReader(Path(args.csv).open()))
    conn = mysql.connector.connect(host=args.host, port=args.port, user=args.user, password=args.password, database=args.database)
    cur = conn.cursor()
    for r in rows:
        cur.execute(
            """
            INSERT INTO property_web_sources (property_code, property_name, website_url, verified, confidence_score, discovery_notes)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
              property_name=VALUES(property_name),
              website_url=VALUES(website_url),
              verified=VALUES(verified),
              confidence_score=VALUES(confidence_score),
              discovery_notes=VALUES(discovery_notes)
            """,
            (
                r['property_code'].upper(),
                r.get('property_name') or '',
                r.get('website_url') or None,
                str(r.get('verified', 'false')).lower() in ('1', 'true', 'yes'),
                float(r['confidence_score']) if r.get('confidence_score') else None,
                r.get('discovery_notes') or None,
            ),
        )
    conn.commit()
    print(f'Upserted {len(rows)} property website mappings.')


if __name__ == '__main__':
    main()
