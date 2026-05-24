from app.sql_planner import parse_period, parse_unit_hint, sql_intent


def test_parse_period_latest():
    assert parse_period('give me kpi summary') == ('latest', None)


def test_parse_period_month():
    kind, val = parse_period('give me may occupancy')
    assert kind == 'month'
    assert val == '05'


def test_parse_period_all_months():
    assert parse_period('all months kpi') == ('all_months', None)


def test_sql_kind_highest_balance():
    assert sql_intent("show highest balances for may 2025", None) == "highest_balances"


def test_sql_kind_unit_detail():
    unit = parse_unit_hint("show details for unit A103")
    assert unit == "A103"
    assert sql_intent("show details for unit A103", unit) == "unit_detail"


def test_sql_kind_lease_charges():
    assert sql_intent("show lease charges for may 2025", None) == "lease_charges"


def test_sql_intent_lease_variant_expire():
    assert sql_intent("leases expire next month", None) == "expiring_next_month"


def test_sql_intent_lease_variant_expiring():
    assert sql_intent("leases expiring next month", None) == "expiring_next_month"


def test_sql_intent_lease_variant_ending():
    assert sql_intent("leases ending next month", None) == "expiring_next_month"


def test_sql_intent_lease_variant_upcoming():
    assert sql_intent("upcoming lease expirations", None) == "expiring_next_month"


def test_sql_intent_renewals_due_soon():
    assert sql_intent("renewals due soon", None) == "expiring_next_month"


def test_sql_intent_moving_out_soon():
    assert sql_intent("who is moving out soon", None) == "expiring_next_month"


def test_sql_intent_owing_more_than():
    assert sql_intent("residents owing more than 500", None) == "highest_balances"
