-- Enforce one snapshot per property per month.
-- Keeps the newest snapshot_id for each (property_code, month_year),
-- removes older duplicates and their dependent rows, then replaces the unique index.

CREATE TEMPORARY TABLE tmp_snapshot_keep AS
SELECT property_code, month_year, MAX(snapshot_id) AS keep_snapshot_id
FROM rent_roll_snapshots
GROUP BY property_code, month_year;

CREATE TEMPORARY TABLE tmp_snapshot_drop AS
SELECT s.snapshot_id
FROM rent_roll_snapshots s
JOIN tmp_snapshot_keep k
  ON k.property_code = s.property_code
 AND k.month_year = s.month_year
WHERE s.snapshot_id <> k.keep_snapshot_id;

DELETE c
FROM rent_roll_unit_charges c
JOIN tmp_snapshot_drop d ON d.snapshot_id = c.snapshot_id;

DELETE u
FROM rent_roll_units u
JOIN tmp_snapshot_drop d ON d.snapshot_id = u.snapshot_id;

DELETE s
FROM rent_roll_snapshots s
JOIN tmp_snapshot_drop d ON d.snapshot_id = s.snapshot_id;

ALTER TABLE rent_roll_snapshots DROP INDEX uq_property_month_file;
ALTER TABLE rent_roll_snapshots ADD UNIQUE KEY uq_property_month (property_code, month_year);

