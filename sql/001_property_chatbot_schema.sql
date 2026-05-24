CREATE TABLE IF NOT EXISTS properties (
  property_code VARCHAR(20) PRIMARY KEY,
  property_name VARCHAR(255) NOT NULL,
  source_system VARCHAR(64) DEFAULT 'rent_roll',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS rent_roll_snapshots (
  snapshot_id BIGINT AUTO_INCREMENT PRIMARY KEY,
  property_code VARCHAR(20) NOT NULL,
  as_of_date DATE NOT NULL,
  month_year CHAR(7) NOT NULL,
  source_file VARCHAR(255) NOT NULL,
  ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_property_month (property_code, month_year),
  CONSTRAINT fk_snapshot_property FOREIGN KEY (property_code) REFERENCES properties(property_code)
);

CREATE TABLE IF NOT EXISTS rent_roll_units (
  unit_row_id BIGINT AUTO_INCREMENT PRIMARY KEY,
  snapshot_id BIGINT NOT NULL,
  property_code VARCHAR(20) NOT NULL,
  unit VARCHAR(30) NOT NULL,
  unit_type VARCHAR(64),
  unit_sq_ft INT,
  resident_id VARCHAR(64),
  resident_name VARCHAR(255),
  market_rent DECIMAL(12,2),
  resident_deposit DECIMAL(12,2),
  other_deposit DECIMAL(12,2),
  move_in_date DATE,
  lease_expiration_date DATE,
  move_out_date DATE,
  balance DECIMAL(12,2),
  CONSTRAINT fk_unit_snapshot FOREIGN KEY (snapshot_id) REFERENCES rent_roll_snapshots(snapshot_id),
  INDEX idx_unit_property (property_code, unit),
  INDEX idx_unit_resident (property_code, resident_id)
);

CREATE TABLE IF NOT EXISTS rent_roll_unit_charges (
  charge_row_id BIGINT AUTO_INCREMENT PRIMARY KEY,
  unit_row_id BIGINT NOT NULL,
  snapshot_id BIGINT NOT NULL,
  property_code VARCHAR(20) NOT NULL,
  charge_code VARCHAR(64) NOT NULL,
  amount DECIMAL(12,2) NOT NULL,
  is_total BOOLEAN DEFAULT FALSE,
  CONSTRAINT fk_charge_unit FOREIGN KEY (unit_row_id) REFERENCES rent_roll_units(unit_row_id),
  CONSTRAINT fk_charge_snapshot FOREIGN KEY (snapshot_id) REFERENCES rent_roll_snapshots(snapshot_id),
  INDEX idx_charge_property (property_code, charge_code)
);
