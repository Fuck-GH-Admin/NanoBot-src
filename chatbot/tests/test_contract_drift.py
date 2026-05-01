# tests/test_contract_drift.py
"""
跨端数据契约漂移测试

验证 Python (Pydantic) 与 Node.js (Zod) 两端对 API 数据结构的校验一致性。
运行前提：Node.js 服务已在 127.0.0.1:3010 启动。
"""

import pytest
import httpx

from plugins.chatbot.schemas import (
    ChatRequestPayload,
    MemorySnapshot,
    UserProfile,
    Trait,
    Entity,
    Relation,
)

NODE_TEST_URL = "http://127.0.0.1:3010/api/chat"


def build_valid_payload() -> dict:
    """工厂函数：生成一个完全合法的、覆盖所有字段的请求体。"""
    payload = ChatRequestPayload(
        chatHistory=[
            {"role": "user", "content": "你好", "name": "TestUser", "user_id": "12345", "timestamp": "2025-01-01T00:00:00"},
            {"role": "assistant", "content": "你好！有什么可以帮你的吗？", "timestamp": "2025-01-01T00:00:01"},
        ],
        memorySnapshot=MemorySnapshot(
            summary="测试群组摘要",
            profiles=[
                UserProfile(
                    user_id="12345",
                    traits=[
                        Trait(content="喜欢编程", confidence=0.9),
                        Trait(content="活跃用户", confidence=0.7),
                    ],
                )
            ],
            entities=[
                Entity(
                    entity_id="user_12345",
                    name="TestUser",
                    type="person",
                    attributes={"role": "member"},
                )
            ],
            relations=[
                Relation(
                    relation_id="rel_1",
                    subject_entity="user_12345",
                    predicate="member_of",
                    object_entity="group_001",
                    confidence=0.95,
                )
            ],
        ),
        tools=[],
        context={"group_id": 100, "active_uids": ["12345"]},
    )
    return payload.model_dump()


@pytest.mark.asyncio
async def test_valid_payload_accepted():
    """标准 payload 应被接受，状态码绝不能是 400 (Schema校验失败)"""
    payload = build_valid_payload()
    async with httpx.AsyncClient() as client:
        resp = await client.post(NODE_TEST_URL, json=payload, timeout=10)
        assert resp.status_code != 400, (
            f"合法数据被 Schema 拒绝，契约已漂移: {resp.text}"
        )


@pytest.mark.asyncio
async def test_missing_chatHistory_rejected():
    """缺少必填字段 chatHistory，状态码必须是 400"""
    payload = build_valid_payload()
    del payload["chatHistory"]
    async with httpx.AsyncClient() as client:
        resp = await client.post(NODE_TEST_URL, json=payload, timeout=10)
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_invalid_confidence_rejected():
    """confidence 超出 [0, 1] 范围，状态码必须是 400"""
    payload = build_valid_payload()
    payload["memorySnapshot"]["profiles"][0]["traits"][0]["confidence"] = 1.5
    async with httpx.AsyncClient() as client:
        resp = await client.post(NODE_TEST_URL, json=payload, timeout=10)
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_missing_role_in_history_rejected():
    """role 字段类型不匹配（数字 vs 字符串），状态码必须是 400"""
    payload = build_valid_payload()
    payload["chatHistory"][0]["role"] = 123
    async with httpx.AsyncClient() as client:
        resp = await client.post(NODE_TEST_URL, json=payload, timeout=10)
        assert resp.status_code == 400
