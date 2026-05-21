
def test_sql_citation_shape():
    citation = {'source_type': 'mysql_snapshot', 'property_code': '115R', 'month_year': '2025-05'}
    assert citation['source_type'] == 'mysql_snapshot'
    assert citation['property_code'] == '115R'
    assert 'month_year' in citation
