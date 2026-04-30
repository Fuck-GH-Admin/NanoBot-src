const axios = require('axios');
const { TavernCoreV2 } = require('./TavernCoreV2');
const MemoryManager = require('./MemoryManager');

const DEEPSEEK_API_KEY = process.env.DEEPSEEK_API_KEY || '';
const BASE_URL = process.env.DEEPSEEK_BASE_URL || 'https://api.deepseek.com';

class DeepSeekTavernClient {
    constructor(charCard, userSettings, worldInfoEntries = [], maxHistoryLength = 15) {
        this.engine = new TavernCoreV2(charCard, userSettings, worldInfoEntries);
        try {
            this.memory = new MemoryManager(maxHistoryLength, DEEPSEEK_API_KEY);
        } catch (e) {
            console.warn('[DeepSeekTavernClient] MemoryManager not initialized:', e.message);
            this.memory = null;
        }
    }

    /**
     * 发送聊天请求（自动处理历史压缩）
     * @param {Array} chatHistory 原始聊天历史
     * @param {string} [existingSummary=''] 已有的记忆摘要
     * @param {number} [maxTokens=4000] 最大 token 数
     * @param {Array} [tools=[]] OpenAI 格式的工具定义数组，显式传递避免全局竞态
     * @returns {Promise<Object>} { choices, newSummary, newHistory }
     */
    async ask(chatHistory, existingSummary = '', maxTokens = 4000, tools = []) {
        try {
            // 1. 先进行记忆压缩
            const { newHistory, newSummary } = await this.memory.checkAndSummarize(
                chatHistory,
                existingSummary
            );

            // 2. 如果产生了摘要，将其注入到历史中（通常作为系统消息）
            const historyToSend = [...newHistory];
            if (newSummary && newSummary.trim()) {
                historyToSend.unshift({
                    is_system: true,
                    mes: `[Summary of previous events: ${newSummary}]`,
                    name: 'System',
                });
            }

            // 3. 构建 OpenAI 消息
            const { messages } = await this.engine.buildOpenAIMessages(historyToSend, maxTokens);

            // 4. 构造请求体，显式包含工具定义
            const requestBody = {
                model: process.env.DEEPSEEK_MODEL || "deepseek-chat",
                messages: messages,
                stream: false,
                temperature: parseFloat(process.env.LLM_TEMPERATURE) || 0.7,
            };
            if (tools && tools.length > 0) {
                requestBody.tools = tools;
            }

            // 5. 请求 DeepSeek API
            const response = await axios.post(`${BASE_URL}/chat/completions`, requestBody, {
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': `Bearer ${DEEPSEEK_API_KEY}`
                }
            });

            // 6. 透传 DeepSeek 原始 choices（OpenAI 兼容格式，含 tool_calls）
            //    Python 端 agent_service / drawing_service / permission_service 均读取
            //    data["choices"][0]["message"]["content"] 和 tool_calls
            return {
                choices: response.data.choices,
                newSummary,
                newHistory,
            };
        } catch (error) {
            console.error('API 请求失败:', error.response?.data || error.message);
            throw error;
        }
    }
}

module.exports = DeepSeekTavernClient;
