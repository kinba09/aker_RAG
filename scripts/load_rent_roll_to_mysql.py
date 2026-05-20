#!/usr/bin/env python3
"""
Dependency-light loader for rent roll files (.xls that are actually xlsx zip format).
Requires: mysql-connector-python

Usage:
  python3 scripts/load_rent_roll_to_mysql.py \\
    --data-dir RentRoll_LeaseCharges_NamesRedacted \\
    --host 127.0.0.1 --port 3306 --user root --password secret --database property_chatbot
"""
import argparse
import datetime as dt
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import mysql.connector

NS = {'a': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
EXCEL_EPOCH = dt.date(1899, 12, 30)


def excel_date_to_date(v):
    if not v:
        return None
    try:
        return EXCEL_EPOCH + dt.timedelta(days=int(float(v)))
    except Exception:
        return None


def cell_value(c, sst):
    t = c.attrib.get('t')
    v = c.find('a:v', NS)
    isv = c.find('a:is', NS)
    if t == 's' and v is not None:
        return sst[int(v.text)]
    if t == 'inlineStr' and isv is not None:
        return ''.join(x.text or '' for x in isv.iter('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t'))
    return '' if v is None else v.text


def parse_sheet(path):
    with zipfile.ZipFile(path) as z:
        sst = []
        if 'xl/sharedStrings.xml' in z.namelist():
            root = ET.fromstring(z.read('xl/sharedStrings.xml'))
            for si in root.findall('a:si', NS):
                sst.append(''.join(t.text or '' for t in si.iter('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t')))
        sh = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
        rows = sh.find('a:sheetData', NS).findall('a:row', NS)
        parsed = []
        for r in rows:
            row_map = {}
            for c in r.findall('a:c', NS):
                row_map[c.attrib['r'][0]] = cell_value(c, sst)
            parsed.append(row_map)
        return parsed


def f2(v):
    try:
        return round(float(v), 2)
    except Exception:
        return None


def load_file(cur, fpath):
    code = re.search(r'_([A-Za-z0-9]+)\.xls$', fpath.name).group(1).upper()
    rows = parse_sheet(fpath)
    prop_name = rows[1].get('A', '').strip()
    as_of_text = rows[2].get('A', '')
    month_year_text = rows[3].get('A', '')
    as_of = dt.datetime.strptime(as_of_text.split('=')[-1].strip(), '%m/%d/%Y').date()
    month_year = dt.datetime.strptime(month_year_text.split('=')[-1].strip(), '%m/%Y').strftime('%Y-%m')

    cur.execute("INSERT IGNORE INTO properties (property_code, property_name) VALUES (%s, %s)", (code, prop_name))
    cur.execute(
        """INSERT INTO rent_roll_snapshots (property_code, as_of_date, month_year, source_file)
           VALUES (%s, %s, %s, %s)
           ON DUPLICATE KEY UPDATE snapshot_id=LAST_INSERT_ID(snapshot_id)""",
        (code, as_of, month_year, fpath.name),
    )
    snapshot_id = cur.lastrowid

    i = 7
    while i < len(rows):
        r = rows[i]
        unit = (r.get('A') or '').strip()
        charge_code = (r.get('G') or '').strip()
        if not unit:
            i += 1
            continue

        cur.execute(
            """INSERT INTO rent_roll_units (snapshot_id, property_code, unit, unit_type, unit_sq_ft, resident_id,
            resident_name, market_rent, resident_deposit, other_deposit, move_in_date, lease_expiration_date, move_out_date, balance)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                snapshot_id, code, unit, r.get('B'), int(float(r['C'])) if r.get('C') else None, r.get('D'), r.get('E'),
                f2(r.get('F')), f2(r.get('I')), f2(r.get('J')),
                excel_date_to_date(r.get('K')), excel_date_to_date(r.get('L')), excel_date_to_date(r.get('M')), f2(r.get('N')),
            ),
        )
        unit_row_id = cur.lastrowid

        while i < len(rows):
            rr = rows[i]
            cc = (rr.get('G') or '').strip()
            if not cc:
                i += 1
                if i < len(rows) and (rows[i].get('A') or '').strip():
                    break
                continue
            cur.execute(
                "INSERT INTO rent_roll_unit_charges (unit_row_id, snapshot_id, property_code, charge_code, amount, is_total) VALUES (%s,%s,%s,%s,%s,%s)",
                (unit_row_id, snapshot_id, code, cc, f2(rr.get('H')) or 0.0, cc.upper() == 'TOTAL'),
            )
            i += 1
            if i < len(rows) and (rows[i].get('A') or '').strip():
                break


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data-dir', required=True)
    ap.add_argument('--host', required=True)
    ap.add_argument('--port', type=int, default=3306)
    ap.add_argument('--user', required=True)
    ap.add_argument('--password', required=True)
    ap.add_argument('--database', required=True)
    args = ap.parse_args()

    conn = mysql.connector.connect(host=args.host, port=args.port, user=args.user, password=args.password, database=args.database)
    cur = conn.cursor()
    files = sorted(Path(args.data_dir).glob('*.xls'))
    for f in files:
        load_file(cur, f)
    conn.commit()
    cur.close()
    conn.close()
    print(f'Loaded {len(files)} files.')


if __name__ == '__main__':
    main()
