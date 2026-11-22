"""Validated decisions shared by live and scripted agents.
真实模型与脚本模型共用的决策校验契约。
"""

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.support_workflow import PrepareRefund


class TextDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str = Field(strict=True, min_length=1, max_length=20000)

    @field_validator("text")
    @classmethod
    def usable(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Text must not be blank or contain null characters")
        return value


class TicketQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ticket_id: int = Field(strict=True, gt=0, le=2**63 - 1)


class OrderQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    order_id: int = Field(strict=True, gt=0, le=2**63 - 1)


SCHEMAS = {
    "reply": TextDecision,
    "ask_clarification": TextDecision,
    "lookup_ticket": TicketQuery,
    "check_refund": OrderQuery,
    "prepare_refund": PrepareRefund,
}
TERMINAL = {"reply", "ask_clarification", "prepare_refund"}


@dataclass(frozen=True)
class Decision:
    name: str
    arguments: dict

    def validated(self):
        """Reject authority fields and unknown actions, including direct refund application.
        拒绝授权字段及未知动作，包括直接执行退款。
        """
        if self.name not in SCHEMAS:
            raise ValueError("Unknown agent action")
        return Decision(self.name, SCHEMAS[self.name].model_validate(self.arguments).model_dump())


def model_tools():
    """Use strict function schemas; application validation remains authoritative.
    使用严格函数结构，最终仍由应用校验决定是否执行。
    """
    descriptions = {
        "lookup_ticket": "Read an accessible ticket and linked order. Never changes business data.",
        "check_refund": "Check current refund eligibility for an accessible order. No refund is applied.",
        "prepare_refund": "Save an exact proposed customer reply and refund for human approval. This does NOT apply a refund. Use only when the user requests a proposed resolution, not a read-only check.",
        "ask_clarification": "Ask for missing or ambiguous information without changing business data.",
    }
    tools = []
    for name, description in descriptions.items():
        schema = SCHEMAS[name].model_json_schema()
        # Strict mode requires every property; nullable fields remain explicitly nullable.
        # 严格模式要求列出全部属性，可空字段仍显式允许 null。
        schema["required"] = list(schema["properties"])
        for value in schema["properties"].values():
            value.pop("default", None)
        tools.append(
            {
                "type": "function",
                "name": name,
                "description": description,
                "parameters": schema,
                "strict": True,
            }
        )
    return tools
