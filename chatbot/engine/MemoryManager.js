/**
 * MemoryManager - 长对话历史自动总结模块
 *
 * 用于在对话长度超过阈值时，将旧消息压缩为一段记忆摘要，
 * 从而控制传递给大模型的上下文长度，避免超出 token 限制。
 *
 * 完全独立于 SillyTavern，只依赖 axios 调用 DeepSeek API。
 *
 * @module MemoryManager
 * @author silentaichat
 * @version 1.0.0
 */

const axios = require('axios');

class MemoryManager {
    /**
     * 创建 MemoryManager 实例
     * @param {number} maxHistoryLength - 触发总结的对话消息条数上限（例如 20）
     * @param {string} apiKey - DeepSeek API 密钥
     */
    constructor(maxHistoryLength, apiKey) {
        if (typeof maxHistoryLength !== 'number' || maxHistoryLength <= 0) {
            throw new Error('maxHistoryLength 必须是一个大于 0 的数字');
        }

        /** @type {number} 触发总结的消息条数阈值 */
        this.maxHistoryLength = maxHistoryLength;
        /** @type {string} DeepSeek API Key */
        this.apiKey = apiKey || process.env.DEEPSEEK_API_KEY || '';
        /** @type {string} API 基础 URL */
        this.baseURL = process.env.DEEPSEEK_BASE_URL || 'https://api.deepseek.com/chat/completions';
        /** @type {boolean} 标记是否有可用的 API Key */
        this.hasValidKey = !!this.apiKey;
    }

    /**
     * 检查对话历史长度并在必要时执行总结
     *
     * @param {Array<Object>} chatHistory - 当前完整的对话历史数组，
     *   每个元素通常包含 { mes: string, name: string, is_system?: boolean } 等字段
     * @param {string} [existingSummary=''] - 之前已经存在的记忆摘要（空字符串表示无）
     * @returns {Promise<{newHistory: Array<Object>, newSummary: string}>}
     *   返回对象包含裁剪后的近期历史和新生成的摘要
     */
    async checkAndSummarize(chatHistory, existingSummary = '') {
        // 参数校验
        if (!Array.isArray(chatHistory)) {
            throw new Error('chatHistory 必须是一个数组');
        }

        // 如果没有有效的 API Key，跳过总结
        if (!this.hasValidKey) {
            console.warn('[MemoryManager] 未配置 API Key，跳过记忆总结。');
            return {
                newHistory: chatHistory,
                newSummary: existingSummary
            };
        }

        // 如果未超过阈值，直接返回原始历史和现有摘要
        if (chatHistory.length <= this.maxHistoryLength) {
            return {
                newHistory: chatHistory,
                newSummary: existingSummary
            };
        }

        console.log(`[MemoryManager] 历史长度 ${chatHistory.length} 超过阈值 ${this.maxHistoryLength}，开始总结...`);

        // 1. 划分旧消息（前60%）和近期消息（后40%）
        const splitIndex = Math.floor(chatHistory.length * 0.6);
        // 确保至少保留 1 条近期消息，避免 newHistory 为空
        const safeSplitIndex = Math.max(1, Math.min(splitIndex, chatHistory.length - 1));

        /** @type {Array<Object>} 需要压缩的旧消息 */
        const oldMessages = chatHistory.slice(0, safeSplitIndex);
        /** @type {Array<Object>} 保留的近期历史 */
        const newHistory = chatHistory.slice(safeSplitIndex);

        if (oldMessages.length === 0) {
            // 理论上不会走到这里，但做安全处理
            return { newHistory, newSummary: existingSummary };
        }

        // 2. 构建总结请求的 Prompt
        const summaryPrompt = this._buildSummaryPrompt(oldMessages, existingSummary);

        // 3. 调用 DeepSeek API 获取摘要
        try {
            const newSummary = await this._callDeepSeekAPI(summaryPrompt);
            console.log(`[MemoryManager] 总结完成，新摘要长度: ${newSummary.length}`);

            return {
                newHistory,
                newSummary
            };
        } catch (error) {
            console.error('[MemoryManager] 总结 API 调用失败，回退至原历史和现有摘要', error.message);
            // 失败时返回完整的原历史（不裁剪）和现有摘要，避免丢失上下文
            return {
                newHistory: chatHistory,
                newSummary: existingSummary
            };
        }
    }

    /**
     * 根据旧消息和已有摘要构建发送给大模型的总结 prompt
     * @param {Array<Object>} oldMessages - 需要总结的旧消息数组
     * @param {string} existingSummary - 旧的摘要（可能为空）
     * @returns {Array<Object>} 符合 OpenAI Chat Completion 格式的 messages 数组
     * @private
     */
    _buildSummaryPrompt(oldMessages, existingSummary) {
        // 格式化旧消息为易读文本，跳过系统消息（is_system）避免干扰
        const conversationText = oldMessages
            .filter(msg => !msg.is_system && msg.mes)
            .map(msg => {
                const speaker = msg.name || 'Unknown';
                const text = msg.mes.trim();
                return `${speaker}: ${text}`;
            })
            .join('\n');

        // 系统提示词：指导模型总结
        const systemContent = 'Ignore previous instructions. Summarize the most important facts and events in the story so far. If a summary already exists, expand it. Only output the summary.';

        // 用户消息：如果有旧摘要，显式提供给模型
        let userContent = '';
        if (existingSummary && existingSummary.trim().length > 0) {
            userContent = `Existing summary:\n${existingSummary}\n\n`;
        }
        userContent += `Recent conversation to summarize:\n${conversationText}`;

        return [
            { role: 'system', content: systemContent },
            { role: 'user', content: userContent }
        ];
    }

    /**
     * 调用 DeepSeek API 发送总结请求
     * @param {Array<Object>} messages - 符合 OpenAI 格式的消息数组
     * @returns {Promise<string>} 模型返回的摘要文本
     * @throws {Error} 请求失败或响应异常
     * @private
     */
    async _callDeepSeekAPI(messages) {
        const response = await axios.post(
            this.baseURL,
            {
                model: process.env.DEEPSEEK_MODEL || 'deepseek-chat',
                messages: messages,
                stream: false,
                temperature: parseFloat(process.env.SUMMARY_TEMPERATURE) || 0.3,
                max_tokens: parseInt(process.env.SUMMARY_MAX_TOKENS) || 2000
            },
            {
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': `Bearer ${this.apiKey}`
                }
            }
        );

        // 检查响应结构
        if (response.data &&
            response.data.choices &&
            response.data.choices.length > 0 &&
            response.data.choices[0].message) {
            const summary = response.data.choices[0].message.content?.trim();
            if (!summary) {
                throw new Error('API 返回的摘要内容为空');
            }
            return summary;
        }

        throw new Error(`DeepSeek API 返回意外格式: ${JSON.stringify(response.data)}`);
    }
}

module.exports = MemoryManager;