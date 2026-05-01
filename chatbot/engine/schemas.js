/**
 * 跨端统一数据契约（Node 端 Zod 校验）
 * 与 Python schemas.py 完全对应，双端强制校验。
 */

const { z } = require('zod');

const TraitSchema = z.object({
    uid: z.string().nullable().optional(),
    content: z.string(),
    confidence: z.number().min(0).max(1),
    updated_at: z.string().default("")
});

const UserProfileSchema = z.object({
    user_id: z.string(),
    traits: z.array(TraitSchema).default([])
});

const EntitySchema = z.object({
    entity_id: z.string(),
    name: z.string(),
    type: z.string(),
    attributes: z.record(z.any()).default({})
});

const RelationSchema = z.object({
    relation_id: z.string(),
    subject_entity: z.string(),
    predicate: z.string(),
    object_entity: z.string(),
    confidence: z.number().min(0).max(1)
});

const MemorySnapshotSchema = z.object({
    summary: z.string().default(""),
    profiles: z.array(UserProfileSchema).default([]),
    entities: z.array(EntitySchema).default([]),
    relations: z.array(RelationSchema).default([])
});

// ─── 消息 Schema（跨端契约核心） ───
const MessageSchema = z.object({
    role: z.enum(['user', 'assistant', 'system', 'tool']),
    content: z.string().optional(),
    name: z.string().optional(),
    user_id: z.string().optional(),
    timestamp: z.string().optional(),
    tool_call_id: z.string().optional(),
    tool_calls: z.array(z.any()).optional(),
});

const ChatRequestSchema = z.object({
    chatHistory: z.array(MessageSchema.passthrough()),
    memorySnapshot: MemorySnapshotSchema,
    tools: z.array(z.any()).optional().default([]),
    context: z.record(z.any()).optional().default({})
});

module.exports = {
    ChatRequestSchema,
    MessageSchema,
    MemorySnapshotSchema,
    TraitSchema,
    UserProfileSchema,
    EntitySchema,
    RelationSchema
};
