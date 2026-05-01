/**
 * DeepSeekTavernClient — 纯粹的 Prompt 编译与 LLM 调用中枢
 *
 * 职责：
 * 1. 将 Python 端传入的 chatHistory / summaryContext / userProfiles 编译为 OpenAI 消息
 * 2. 调用 DeepSeek API
 * 3. 透传原始 choices 返回给 Python 端
 *
 * 不持有任何运行时状态，不负责记忆压缩（已移交给 Python 端 memory_service）。
 *
 * 配置通过构造函数注入（由 server.js 在启动时从环境变量读取一次），禁止自行读取 process.env。
 */

const axios = require('axios');
const { TavernCoreV2 } = require('./TavernCoreV2');

class DeepSeekTavernClient {
    /**
     * @param {Object} charCard - 角色卡
     * @param {Object} userSettings - 用户设置
     * @param {Array}  worldInfoEntries - 世界书条目
     * @param {Object} llmConfig - LLM 配置（由 server.js 从环境变量读取后注入）
     * @param {string} llmConfig.apiKey - DeepSeek API Key
     * @param {string} llmConfig.baseUrl - API 基础 URL
     * @param {string} llmConfig.model - 模型名称
     * @param {number} llmConfig.temperature - 采样温度
     */
    constructor(charCard, userSettings, worldInfoEntries = [], llmConfig = {}) {
        this.engine = new TavernCoreV2(charCard, userSettings, worldInfoEntries);
        this.apiKey = llmConfig.apiKey;
        this.baseUrl = llmConfig.baseUrl;
        this.model = llmConfig.model;
        this.temperature = llmConfig.temperature;
    }

    /**
     * 发送聊天请求（无状态流水线）
     *
     * @param {Array}  chatHistory   - Python 端精确裁剪好的近期消息
     * @param {string} [existingSummary=''] - 群组宏观摘要（作为背景上下文注入）
     * @param {number} [maxTokens=4000]     - 最大响应 token 数
     * @param {Array}  [tools=[]]           - OpenAI 格式的工具定义
     * @param {Object} [userProfiles={}]    - 活跃群友画像 { user_id: "特征1; 特征2" }
     * @returns {Promise<Object>} { choices }
     */
    async ask(chatHistory, existingSummary = '', maxTokens = 4000, tools = [], userProfiles = {}, context = {}, relations = []) {
        // 1. 编译 Prompt：世界书扫描 + 系统提示 + 画像注入 + Token 预算截断
        const summaryContext = (existingSummary && existingSummary.trim()) ? existingSummary : '';
        const { messages } = await this.engine.buildOpenAIMessages(
            chatHistory, maxTokens, summaryContext, userProfiles, context, relations
        );

        // 2. 构造请求体
        const requestBody = {
            model: this.model,
            messages: messages,
            stream: false,
            temperature: this.temperature,
        };
        if (tools && tools.length > 0) {
            requestBody.tools = tools;
        }

        // 3. 调用 DeepSeek API
        const response = await axios.post(`${this.baseUrl}/chat/completions`, requestBody, {
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${this.apiKey}`
            },
            timeout: 120000,
        });

        // 4. 透传原始 choices（OpenAI 兼容格式，含 tool_calls）
        return {
            choices: response.data.choices,
        };
    }
}

module.exports = DeepSeekTavernClient;
