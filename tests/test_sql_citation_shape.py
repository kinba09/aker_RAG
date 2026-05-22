
def test_sql_citation_shape():
    citation = {'source_type': 'sql', 'property_code': '115R', 'period_applied': '2025-05', 'query_source': 'template', 'sql_kind': 'kpi_summary', 'row_count': 1}
    assert citation['source_type'] == 'sql'
    assert citation['property_code'] == '115R'
    assert citation['query_source'] in {'template', 'llm_generated_validated', 'none'}
    assert 'sql_kind' in citation
