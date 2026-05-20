#!/usr/bin/env python3
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

NS = {'a': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def cell_value(c, sst):
    t = c.attrib.get('t')
    v = c.find('a:v', NS)
    isv = c.find('a:is', NS)
    if t == 's' and v is not None:
        return sst[int(v.text)]
    if t == 'inlineStr' and isv is not None:
        return ''.join(x.text or '' for x in isv.iter('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t'))
    return '' if v is None else v.text


def parse_headers(xlsx_path: Path):
    with zipfile.ZipFile(xlsx_path) as z:
        sst = []
        if 'xl/sharedStrings.xml' in z.namelist():
            root = ET.fromstring(z.read('xl/sharedStrings.xml'))
            for si in root.findall('a:si', NS):
                sst.append(''.join(t.text or '' for t in si.iter('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t')))
        sh = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
        rows = sh.find('a:sheetData', NS).findall('a:row', NS)

        row5 = {c.attrib['r'][0]: cell_value(c, sst) for c in rows[4].findall('a:c', NS)}
        row6 = {c.attrib['r'][0]: cell_value(c, sst) for c in rows[5].findall('a:c', NS)}
        headers = {}
        for col in sorted(set(row5) | set(row6)):
            h1 = row5.get(col, '').strip()
            h2 = row6.get(col, '').strip()
            headers[col] = (h1 + ' ' + h2).strip().replace('  ', ' ')
        return headers


def main():
    root = Path('RentRoll_LeaseCharges_NamesRedacted')
    files = sorted(root.glob('*.xls'))
    code_counts = {}
    for f in files:
        m = re.search(r'_([A-Za-z0-9]+)\.xls$', f.name)
        if m:
            code = m.group(1).upper()
            code_counts[code] = code_counts.get(code, 0) + 1
    sample = root / 'Jan_RENT_ROLL_WITH_LEASE_CHARGES_115r.xls'
    headers = parse_headers(sample)

    print('total_files:', len(files))
    print('property_codes:', len(code_counts))
    print('codes:', sorted(code_counts.keys()))
    print('sample_headers_115R:')
    for col in 'ABCDEFGHIJKLMN':
        print(f'  {col}: {headers.get(col, "")}')


if __name__ == '__main__':
    main()
