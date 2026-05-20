import os
import re
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from app.db import get_engine


def _to_date(val):
    if pd.isna(val):
        return None
    if hasattr(val, 'date'):
        return val.date()
    try:
        return pd.to_datetime(val).date()
    except Exception:
        return None


def _to_float(val):
    if pd.isna(val):
        return None
    try:
        return float(val)
    except Exception:
        return None


def load_all_files(data_dir: str | None = None):
    data_dir = data_dir or os.getenv('DATA_DIR', '/data')
    root = Path(data_dir)
    files = sorted(root.glob('*.xls'))
    engine = get_engine()

    loaded = 0
    with engine.begin() as conn:
        for fpath in files:
            code_match = re.search(r'_([A-Za-z0-9]+)\.xls$', fpath.name)
            if not code_match:
                continue
            property_code = code_match.group(1).upper()

            df = pd.read_excel(fpath, header=None, engine='openpyxl')
            property_name = str(df.iloc[1, 0]).strip()
            as_of = pd.to_datetime(str(df.iloc[2, 0]).split('=')[-1].strip(), format='%m/%d/%Y').date()
            month_year = pd.to_datetime(str(df.iloc[3, 0]).split('=')[-1].strip(), format='%m/%Y').strftime('%Y-%m')

            conn.execute(text("INSERT IGNORE INTO properties (property_code, property_name) VALUES (:c,:n)"), {"c": property_code, "n": property_name})
            conn.execute(text("""
                INSERT INTO rent_roll_snapshots (property_code, as_of_date, month_year, source_file)
                VALUES (:c,:a,:m,:f)
                ON DUPLICATE KEY UPDATE snapshot_id=LAST_INSERT_ID(snapshot_id)
            """), {"c": property_code, "a": as_of, "m": month_year, "f": fpath.name})
            snapshot_id = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()

            data = df.iloc[7:].copy()
            data.columns = list('ABCDEFGHIJKLMN')
            i = 0
            while i < len(data):
                row = data.iloc[i]
                unit = row['A']
                if pd.isna(unit) or str(unit).strip() == '':
                    i += 1
                    continue
                if str(unit).strip().lower().startswith('current/notice/vacant'):
                    i += 1
                    continue
                if _to_float(row['F']) is None and _to_float(row['H']) is None:
                    i += 1
                    continue

                conn.execute(text("""
                    INSERT INTO rent_roll_units (
                      snapshot_id, property_code, unit, unit_type, unit_sq_ft, resident_id, resident_name,
                      market_rent, resident_deposit, other_deposit, move_in_date, lease_expiration_date, move_out_date, balance
                    ) VALUES (
                      :snapshot_id, :property_code, :unit, :unit_type, :unit_sq_ft, :resident_id, :resident_name,
                      :market_rent, :resident_deposit, :other_deposit, :move_in_date, :lease_expiration_date, :move_out_date, :balance
                    )
                """), {
                    "snapshot_id": snapshot_id,
                    "property_code": property_code,
                    "unit": str(row['A']).strip(),
                    "unit_type": None if pd.isna(row['B']) else str(row['B']).strip(),
                    "unit_sq_ft": None if pd.isna(row['C']) else int(float(row['C'])),
                    "resident_id": None if pd.isna(row['D']) else str(row['D']).strip(),
                    "resident_name": None if pd.isna(row['E']) else str(row['E']).strip(),
                    "market_rent": _to_float(row['F']),
                    "resident_deposit": _to_float(row['I']),
                    "other_deposit": _to_float(row['J']),
                    "move_in_date": _to_date(row['K']),
                    "lease_expiration_date": _to_date(row['L']),
                    "move_out_date": _to_date(row['M']),
                    "balance": _to_float(row['N']),
                })
                unit_row_id = conn.execute(text("SELECT LAST_INSERT_ID()")).scalar()

                while i < len(data):
                    r = data.iloc[i]
                    charge_code = '' if pd.isna(r['G']) else str(r['G']).strip()
                    amount = _to_float(r['H'])
                    if charge_code:
                        conn.execute(text("""
                            INSERT INTO rent_roll_unit_charges (unit_row_id, snapshot_id, property_code, charge_code, amount, is_total)
                            VALUES (:u,:s,:p,:c,:a,:t)
                        """), {
                            "u": unit_row_id,
                            "s": snapshot_id,
                            "p": property_code,
                            "c": charge_code,
                            "a": amount or 0.0,
                            "t": charge_code.upper() == 'TOTAL',
                        })

                    i += 1
                    if i >= len(data):
                        break
                    next_unit = data.iloc[i]['A']
                    if not pd.isna(next_unit) and str(next_unit).strip() != '':
                        break

            loaded += 1

    return {"loaded_files": loaded}
