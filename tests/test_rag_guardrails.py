from qdrant_client.models import Filter, FieldCondition, MatchValue


def test_property_filter_shape():
    f = Filter(must=[FieldCondition(key='property_code', match=MatchValue(value='115R'))])
    assert f.must[0].key == 'property_code'
    assert f.must[0].match.value == '115R'
