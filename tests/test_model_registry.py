from app.models import MODEL_REGISTRY


def test_model_registry_matches_supported_prefixes():
    supported_prefixes = ("gemini", "grok", "gpt")
    for model in MODEL_REGISTRY:
        assert model.model_id.startswith(supported_prefixes)

