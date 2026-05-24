from pydantic import BaseModel, Field


class ChatIn(BaseModel):
    property_code: str = Field(..., description="Property code, e.g. 115R")
    question: str
    model_id: str | None = Field(default="gemini-3.1-flash-lite", description="Runtime model switch")


class ModelInfo(BaseModel):
    model_id: str
    provider: str
    supports_tools: bool
    enabled_by_default: bool = False


MODEL_REGISTRY = [
    ModelInfo(model_id="gemini-3.1-flash-lite", provider="google", supports_tools=True, enabled_by_default=True),
    ModelInfo(model_id="gemini-1.5-flash", provider="google", supports_tools=True),
    ModelInfo(model_id="gemini-1.5-flash-8b", provider="google", supports_tools=True),
    ModelInfo(model_id="grok-beta", provider="xai", supports_tools=True),
    ModelInfo(model_id="gpt-4.1-mini", provider="openai", supports_tools=True),
    ModelInfo(model_id="gpt-4.1", provider="openai", supports_tools=True),
]
