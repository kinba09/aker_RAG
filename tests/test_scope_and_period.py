from app.chat import _parse_period


def test_parse_period_latest():
    assert _parse_period('give me kpi summary') == ('latest', None)


def test_parse_period_month():
    kind, val = _parse_period('give me may occupancy')
    assert kind == 'month'
    assert val == '05'


def test_parse_period_all_months():
    assert _parse_period('all months kpi') == ('all_months', None)
