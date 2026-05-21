CREATE TABLE IF NOT EXISTS property_web_sources (
  source_id BIGINT AUTO_INCREMENT PRIMARY KEY,
  property_code VARCHAR(20) NOT NULL,
  property_name VARCHAR(255) NOT NULL,
  website_url VARCHAR(1024),
  verified BOOLEAN DEFAULT FALSE,
  confidence_score DECIMAL(5,2),
  discovery_notes TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_property_source (property_code),
  CONSTRAINT fk_web_source_property FOREIGN KEY (property_code) REFERENCES properties(property_code)
);
