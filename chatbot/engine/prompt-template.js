/**
 * 安全 XML 模板引擎
 * 所有动态变量自动转义，防止提示词注入。
 */

/**
 * 对字符串进行 XML 转义
 */
function escapeXml(str) {
    return String(str).replace(/[<>&]/g, c => ({
        '<': '&lt;',
        '>': '&gt;',
        '&': '&amp;'
    }[c]));
}

/**
 * 构建单个 XML 标签块，自动转义内容
 * @param {string} tag - 标签名
 * @param {string} content - 动态内容（自动转义）
 * @returns {string} 完整的 XML 块
 */
function buildXmlBlock(tag, content) {
    const escaped = escapeXml(String(content));
    return `<${tag}>\n${escaped}\n</${tag}>`;
}

/**
 * 安全的多行 XML 块，支持子元素数组
 * @param {string} tag - 父标签名
 * @param {Array<{tag: string, content: string}>} children - 子元素列表
 * @returns {string} 完整的 XML 块，无有效子元素时返回空串
 */
function buildXmlBlockWithChildren(tag, children = []) {
    const lines = children
        .filter(c => c && c.content && String(c.content).trim())
        .map(c => `  <${c.tag}>${escapeXml(String(c.content))}</${c.tag}>`);
    if (lines.length === 0) return '';
    return `<${tag}>\n${lines.join('\n')}\n</${tag}>`;
}

/**
 * 标签模板函数：安全拼接 XML
 * @example safeXml`<name>${userInput}</name>`
 */
function safeXml(strings, ...values) {
    return strings.reduce((result, str, i) => {
        const val = String(values[i] ?? '');
        return result + str + escapeXml(val);
    }, '');
}

module.exports = { buildXmlBlock, buildXmlBlockWithChildren, safeXml, escapeXml };
