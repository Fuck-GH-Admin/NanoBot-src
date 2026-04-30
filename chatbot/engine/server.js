const fs = require('fs');
const path = require('path');
const express = require('express');
const DeepSeekTavernClient = require('./DeepSeekTavernClient');

const app = express();
app.use(express.json({ limit: '10mb' }));

// ========================= 默认配置（容错） =========================
function getDefaultCharCard() {
    return {
        name: "Elena",
        description: "黑客",
        personality: "暴躁",
        data: {
            system_prompt: "[系统指令：绝对不要承认自己是一个AI语言模型。如果被问及身份，用暴躁的语气回怼。] 始终保持 Elena 的下城机修黑客身份，多用工业、废土、机械相关的比喻。"
        },
        mes_example: "",
        scenario: "",
        first_mes: "",
        post_history_instructions: "",
        character_version: "",
        depth_prompt: "",
        creator_notes: ""
    };
}

function getDefaultWorldbook() {
    return [
        {
            key: ["Framefall"],
            content: "Framefall是十年前发生的一场灾难...",
            position: 4,
            depth: 1
        }
    ];
}

// ========================= 动态加载资源 =========================
function loadAssets() {
    const dataDir = path.resolve(__dirname, '..', 'data');

    // 1. 角色卡
    const charPath = path.join(dataDir, 'character.json');
    let charCard;
    try {
        const raw = fs.readFileSync(charPath, 'utf8');
        charCard = JSON.parse(raw);
        console.log(`[Server] Loaded character from ${charPath}`);
    } catch (err) {
        console.warn(`[Server] Failed to load character.json, using default. Reason: ${err.message}`);
        charCard = getDefaultCharCard();
    }

    // 2. 读取世界书
    const wbPath = path.join(dataDir, 'worldbook.json');
    let worldInfo;
    try {
        const raw = fs.readFileSync(wbPath, 'utf8');
        worldInfo = JSON.parse(raw);
        if (!Array.isArray(worldInfo)) {
            console.warn('[Server] worldbook.json is not an array, using default.');
            worldInfo = getDefaultWorldbook();
        } else {
            console.log(`[Server] Loaded worldbook from ${wbPath}`);
        }
    } catch (err) {
        console.warn(`[Server] Failed to load worldbook.json, using default. Reason: ${err.message}`);
        worldInfo = getDefaultWorldbook();
    }

    // 3. 创建新的 DeepSeekTavernClient 实例
    const userSettings = {
        username: "User",
        names_behavior: 0
    };
    const maxHistoryLength = 15;
    return new DeepSeekTavernClient(charCard, userSettings, worldInfo, maxHistoryLength);
}

// ========================= 全局客户端（初始加载） =========================
let client;
try {
    client = loadAssets();
    console.log('[Server] Initial client created successfully.');
} catch (err) {
    console.error('[Server] Fatal error creating initial client:', err);
    process.exit(1);
}

// ========================= 路由 =========================

// 原有聊天接口
app.post('/api/chat', async (req, res) => {
    try {
        const { chatHistory, existingSummary, tools } = req.body;
        const result = await client.ask(
            chatHistory,
            existingSummary || "",
            4000,
            tools || []
        );
        res.json(result);
    } catch (error) {
        console.error('[Server Error] /api/chat:', error);
        res.status(500).json({ error: error.message });
    }
});

// 新增热重载接口
app.post('/api/reload', async (req, res) => {
    try {
        client = loadAssets();
        console.log('[Server] Assets reloaded successfully.');
        res.json({ status: "success", message: "Assets reloaded" });
    } catch (error) {
        console.error('[Server Error] /api/reload:', error);
        res.status(500).json({ status: "error", message: error.message });
    }
});

// ========================= 启动 =========================
const PORT = process.env.NODE_PORT || 3010;
app.listen(PORT, () => {
    console.log(`TavernCore Server running on port ${PORT}`);
});
