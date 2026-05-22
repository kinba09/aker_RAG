from app.sql_guardrails import validate_generated_sql


def test_sql_validator_rejects_fake_table():
    r = validate_generated_sql(
        "SELECT l.lease_id FROM leases l WHERE l.property_code = :property_code LIMIT 10"
    )
    assert not r.ok


def test_sql_validator_requires_property_code_param():
    r = validate_generated_sql("SELECT u.unit FROM rent_roll_units u LIMIT 10")
    assert not r.ok
    assert ":property_code" in r.reason


def test_sql_validator_rejects_select_star():
    r = validate_generated_sql(
        "SELECT * FROM rent_roll_units u WHERE u.property_code = :property_code LIMIT 10"
    )
    assert not r.ok


def test_sql_validator_rejects_non_select():
    r = validate_generated_sql(
        "DELETE FROM rent_roll_units WHERE property_code = :property_code LIMIT 1"
    )
    assert not r.ok


def test_sql_validator_requires_limit():
    r = validate_generated_sql(
        "SELECT u.unit FROM rent_roll_units u WHERE u.property_code = :property_code"
    )
    assert not r.ok


def test_sql_validator_accepts_bound_property_code():
    r = validate_generated_sql(
        """
        SELECT u.unit, u.balance
        FROM rent_roll_units u
        JOIN rent_roll_snapshots s ON s.snapshot_id = u.snapshot_id
        WHERE u.property_code = :property_code AND s.month_year = :month_year
        ORDER BY u.balance DESC
        LIMIT 25
        """
    )
    assert r.ok
    assert r.normalized_sql is not None
