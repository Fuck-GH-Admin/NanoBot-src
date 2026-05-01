
/**
 * lorebook-engine.js
 *
 * 纯函数的世界书（Lorebook）核心扫描与触发引擎。
 * 不依赖任何 DOM、全局变量或 UI 渲染。
 *
 * 输入：条目数组、扫描文本、配置参数。
 * 输出：触发结果（布尔值）或分类打包后的内容对象。
 *
 * v2.0 重构：
 * - checkKeywords：规范化的主次关键词匹配链路，剔除冗余返回
 * - classifyByPosition：彻底禁用 unshift，改用 push + 确定性排序
 * - scanLorebook：扩展签名支持 context 参数，为群聊上下文预留接口
 */

// ============================================================
// 常量定义（与 world-info.js 保持一致）
// ============================================================

const world_info_logic = {
    AND_ANY: 0,
    NOT_ALL: 1,
    NOT_ANY: 2,
    AND_ALL: 3,
};

const world_info_position = {
    before: 0,
    after: 1,
    ANTop: 2,
    ANBottom: 3,
    atDepth: 4,
    EMTop: 5,
    EMBottom: 6,
    outlet: 7,
};

const DEFAULT_DEPTH = 4;
const MAX_SCAN_DEPTH = 1000;

// ============================================================
// 工具函数
// ============================================================

/**
 * 转义正则表达式中的特殊字符。
 * @param {string} string
 * @returns {string}
 */
function escapeRegex(string) {
    return string.replace(/[/\-\\^$*+?.()|[\]{}]/g, '\\$&');
}

/**
 * 从字符串中解析正则表达式（格式：/pattern/flags）。
 * @param {string} input
 * @returns {RegExp|null}
 */
function parseRegexFromString(input) {
    const match = input.match(/^\/([\w\W]+?)\/([gimsuy]*)$/);
    if (!match) return null;

    let [, pattern, flags] = match;

    // 检查是否有未转义的分隔符
    if (pattern.match(/(^|[^\\])\//)) return null;

    // 取消转义斜杠
    pattern = pattern.replace('\\/', '/');

    try {
        return new RegExp(pattern, flags);
    } catch {
        return null;
    }
}

/**
 * 对字符串进行大小写转换（根据 caseSensitive 设置）。
 * @param {string} str
 * @param {boolean} caseSensitive
 * @returns {string}
 */
function transformString(str, caseSensitive) {
    return caseSensitive ? str : str.toLowerCase();
}

/**
 * 简易内容哈希，用于确定性排序的兜底键。
 * 确保相同内容的条目无论被谁先触发，排序结果永远一致。
 * @param {string} str
 * @returns {number}
 */
function _contentHash(str) {
    if (!str) return 0;
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
        hash = ((hash << 5) - hash) + str.charCodeAt(i);
        hash |= 0;
    }
    return hash;
}

// ============================================================
// A. 关键词匹配引擎
// ============================================================

/**
 * 检查单个关键词是否在文本中匹配。
 * @param {string} haystack - 要搜索的文本
 * @param {string} needle - 要搜索的关键词
 * @param {object} options - 匹配选项
 * @param {boolean} [options.caseSensitive=false]
 * @param {boolean} [options.matchWholeWords=false]
 * @returns {boolean}
 */
function matchKey(haystack, needle, options = {}) {
    const { caseSensitive = false, matchWholeWords = false } = options;

    // 如果关键词是正则表达式，使用正则匹配
    const keyRegex = parseRegexFromString(needle);
    if (keyRegex) {
        return keyRegex.test(haystack);
    }

    // 普通文本匹配
    const transformedHaystack = transformString(haystack, caseSensitive);
    const transformedNeedle = transformString(needle, caseSensitive);

    if (matchWholeWords) {
        const keyWords = transformedNeedle.split(/\s+/);

        if (keyWords.length > 1) {
            // 多词短语：直接包含匹配
            return transformedHaystack.includes(transformedNeedle);
        } else {
            // 单个词：使用单词边界
            const regex = new RegExp(`(?:^|\\W)(${escapeRegex(transformedNeedle)})(?:$|\\W)`);
            return regex.test(transformedHaystack);
        }
    } else {
        return transformedHaystack.includes(transformedNeedle);
    }
}

/**
 * 检查条目是否匹配扫描文本。
 *
 * 匹配流程（规范化）：
 * 1. 提取正向关键词（key）和反向排除关键词（-key）
 * 2. 反向排除：命中任何一个排除词 → 直接返回 false
 * 3. 主关键词匹配（ANY 逻辑）：至少命中一个正向词 → primaryMatch = true
 * 4. 次要关键词校验（根据 selectiveLogic 决定 AND/OR/NOT）
 *
 * @param {object} entry - 条目对象
 * @param {string} scanText - 扫描文本
 * @param {object} [options={}] - 全局覆盖选项
 * @param {boolean} [options.caseSensitive]
 * @param {boolean} [options.matchWholeWords]
 * @returns {boolean}
 */
function checkKeywords(entry, scanText, options = {}) {
    const caseSensitive = options.caseSensitive ?? entry.caseSensitive ?? false;
    const matchWholeWords = options.matchWholeWords ?? entry.matchWholeWords ?? false;
    const matchOptions = { caseSensitive, matchWholeWords };

    // 如果没有主关键词，无法触发
    if (!Array.isArray(entry.key) || entry.key.length === 0) {
        return false;
    }

    // ─── 第一步：分离正向词与反向排除词 ───
    const mainKeys = entry.key.filter(k => !k.startsWith('-'));
    const notKeys = entry.key.filter(k => k.startsWith('-')).map(k => k.slice(1));

    // ─── 第二步：反向排除（NOT 逻辑）───
    // 命中任何一个排除词 → 直接否决
    for (const key of notKeys) {
        if (matchKey(scanText, key, matchOptions)) {
            return false;
        }
    }

    // ─── 第三步：主关键词匹配（ANY 逻辑）───
    // 必须命中至少一个正向词；若无正向词（仅有排除词），默认通过
    let primaryMatch = mainKeys.length === 0;
    for (const key of mainKeys) {
        if (matchKey(scanText, key, matchOptions)) {
            primaryMatch = true;
            break;
        }
    }

    if (!primaryMatch) {
        return false;
    }

    // ─── 第四步：次要关键词校验 ───
    // 如果没有次要关键词，主关键词已通过 → 直接返回 true
    if (!Array.isArray(entry.keysecondary) || entry.keysecondary.length === 0) {
        return true;
    }

    // 根据 selectiveLogic 决定次要关键词的组合方式
    const selectiveLogic = entry.selectiveLogic ?? world_info_logic.AND_ANY;

    let hasAnySecondary = false;
    let hasAllSecondary = true;

    for (const key of entry.keysecondary) {
        const match = matchKey(scanText, key, matchOptions);
        if (match) hasAnySecondary = true;
        if (!match) hasAllSecondary = false;
    }

    switch (selectiveLogic) {
        case world_info_logic.AND_ANY:
            // 主关键词匹配 + 任一次要关键词匹配
            return hasAnySecondary;
        case world_info_logic.NOT_ALL:
            // 主关键词匹配 + 不是所有次要关键词都匹配
            return !hasAllSecondary;
        case world_info_logic.NOT_ANY:
            // 主关键词匹配 + 没有任何次要关键词匹配
            return !hasAnySecondary;
        case world_info_logic.AND_ALL:
            // 主关键词匹配 + 所有次要关键词都匹配
            return hasAllSecondary;
        default:
            return true;
    }
}

// ============================================================
// B. 递归扫描引擎
// ============================================================

/**
 * 执行递归扫描，返回所有被触发的条目。
 *
 * @param {object[]} entries - 所有条目数组（已排序）
 * @param {string} initialScanText - 初始扫描文本
 * @param {object} [config={}] - 配置参数
 * @param {number} [config.maxDepth=2] - 最大扫描深度（递归层数）
 * @param {number} [config.budget=25] - 最大 token 预算百分比
 * @param {number} [config.maxContext=2048] - 最大上下文 token 数
 * @param {number} [config.budgetCap=0] - 预算上限（0 表示不限制）
 * @param {boolean} [config.recursive=true] - 是否启用递归
 * @param {object} [config.globalOptions={}] - 全局匹配选项
 * @param {object} [context={}] - 上下文信息
 * @param {object[]} [context.semantic_hits] - 语义命中条目（直接激活，跳过关键词）
 * @returns {object[]} - 被触发的条目数组
 */
function recursiveScan(entries, initialScanText, config = {}, context = {}) {
    const {
        maxDepth = 2,
        budget = 25,
        maxContext = 2048,
        budgetCap = 0,
        recursive = true,
        globalOptions = {},
    } = config;

    // 计算 token 预算
    let tokenBudget = Math.round(budget * maxContext / 100) || 1;
    if (budgetCap > 0 && tokenBudget > budgetCap) {
        tokenBudget = budgetCap;
    }

    // 已激活的条目（使用 Map 避免重复）
    const activatedEntries = new Map();
    // 当前扫描文本（初始为初始文本）
    let currentScanText = initialScanText;
    // 当前递归深度
    let depth = 0;
    // 是否因预算溢出而停止
    let budgetOverflowed = false;

    // ─── 语义命中预处理 ───
    // 从 context.semantic_hits 构建 uid → hit 的映射表
    const semanticHitMap = new Map();
    if (Array.isArray(context.semantic_hits)) {
        for (const hit of context.semantic_hits) {
            if (hit.uid != null) {
                semanticHitMap.set(String(hit.uid), hit);
            }
        }
    }

    while (depth <= maxDepth) {
        // 在当前扫描文本中查找新激活的条目
        const newlyActivated = [];

        for (const entry of entries) {
            const entryKey = `${entry.world ?? ''}.${entry.uid}`;

            // 跳过已激活的条目
            if (activatedEntries.has(entryKey)) continue;
            // 跳过禁用的条目
            if (entry.disable === true) continue;
            // 跳过常亮条目（它们应该在初始扫描中被激活）
            if (entry.constant && depth > 0) continue;

            // ─── 上下文感知元数据过滤 ───
            // group_ids 白名单：条目仅对指定群组生效
            if (entry.filters?.group_ids && context.group_id) {
                if (!entry.filters.group_ids.includes(context.group_id)) continue;
            }
            // user_ids 白名单：条目仅在指定用户活跃时生效
            if (entry.filters?.user_ids && context.active_uids) {
                if (!entry.filters.user_ids.some(uid => context.active_uids.includes(uid))) continue;
            }

            // ─── 语义命中检查（优先于关键词）───
            const entryUid = String(entry.uid ?? '');
            if (semanticHitMap.has(entryUid)) {
                newlyActivated.push(entry);
                continue;
            }

            // 检查关键词匹配
            if (checkKeywords(entry, currentScanText, globalOptions)) {
                newlyActivated.push(entry);
            }
        }

        // 如果没有新激活的条目，结束扫描
        if (newlyActivated.length === 0) break;

        // 处理新激活的条目
        let newContent = '';
        for (const entry of newlyActivated) {
            const entryKey = `${entry.world ?? ''}.${entry.uid}`;

            // 检查预算
            if (budgetOverflowed && !entry.ignoreBudget) continue;

            // 估算 token 数（简单按字符数估算）
            const contentTokens = Math.ceil((entry.content || '').length / 3.35);
            if (!entry.ignoreBudget && (newContent.length / 3.35 + contentTokens) >= tokenBudget) {
                budgetOverflowed = true;
                continue;
            }

            activatedEntries.set(entryKey, entry);
            newContent += (entry.content || '') + '\n';
        }

        // 如果启用了递归，将新内容加入扫描文本
        if (recursive && newContent) {
            currentScanText = currentScanText + '\n' + newContent;
        }

        depth++;
    }

    // ─── 注入语义命中但未在 entries 中找到的孤立条目 ───
    for (const [uid, hit] of semanticHitMap) {
        const hitKey = `semantic.${uid}`;
        if (!activatedEntries.has(hitKey)) {
            // 检查是否已在主 entries 中被激活（通过 uid 匹配）
            const alreadyActivated = Array.from(activatedEntries.values()).some(
                e => String(e.uid ?? '') === uid
            );
            if (!alreadyActivated && hit.content) {
                activatedEntries.set(hitKey, {
                    key: hit.key || [],
                    content: hit.content,
                    position: hit.position ?? 0,
                    depth: hit.depth ?? 4,
                    uid: uid,
                    _semantic: true,
                });
            }
        }
    }

    return Array.from(activatedEntries.values());
}

// ============================================================
// C. 预算与位置分类器（v2.0 确定性排序版）
// ============================================================

/**
 * 根据条目的 position 设置，将激活的条目分类打包。
 *
 * v2.0 重构：
 * - 彻底禁用 unshift()，所有分类使用 push()
 * - 分类完成后执行确定性排序：order(asc) → depth(asc) → contentHash(asc)
 * - 排序后 join 拼接，确保无论触发顺序如何，输出永远唯一确定
 *
 * @param {object[]} activatedEntries - 被触发的条目数组
 * @param {object} [options={}]
 * @param {number} [options.defaultDepth=4] - 默认深度
 * @returns {object} - 分类打包后的结果
 */
function classifyByPosition(activatedEntries, options = {}) {
    const { defaultDepth = DEFAULT_DEPTH } = options;

    const result = {
        wiBefore: [],
        wiAfter: [],
        wiDepth: [],
        wiANTop: [],
        wiANBottom: [],
        wiEMTop: [],
        wiEMBottom: [],
        wiOutlet: {},
    };

    // ─── 第一步：按 position 分类，全部使用 push ───
    for (const entry of activatedEntries) {
        const content = entry.content || '';
        if (!content) continue;

        const position = entry.position ?? world_info_position.before;
        const depth = entry.depth ?? defaultDepth;

        // 为排序附加元数据（不修改原始 entry）
        const sortMeta = {
            order: entry.order ?? 100,
            depth: depth,
            contentHash: _contentHash(content),
        };

        switch (position) {
            case world_info_position.before:
                result.wiBefore.push({ ...sortMeta, content });
                break;

            case world_info_position.after:
                result.wiAfter.push({ ...sortMeta, content });
                break;

            case world_info_position.atDepth:
                result.wiDepth.push({
                    ...sortMeta,
                    depth: depth,
                    content: content,
                    role: entry.role ?? 0,
                });
                break;

            case world_info_position.ANTop:
                result.wiANTop.push({ ...sortMeta, content });
                break;

            case world_info_position.ANBottom:
                result.wiANBottom.push({ ...sortMeta, content });
                break;

            case world_info_position.EMTop:
                result.wiEMTop.push({ ...sortMeta, position: 0, content });
                break;

            case world_info_position.EMBottom:
                result.wiEMBottom.push({ ...sortMeta, position: 1, content });
                break;

            case world_info_position.outlet: {
                const outletName = entry.outletName || 'default';
                if (!result.wiOutlet[outletName]) {
                    result.wiOutlet[outletName] = [];
                }
                result.wiOutlet[outletName].push({ ...sortMeta, content });
                break;
            }

            default:
                result.wiBefore.push({ ...sortMeta, content });
                break;
        }
    }

    // ─── 第二步：确定性排序 ───
    // 排序规则（优先级递减）：
    //   1. order   — 升序（数值小的在前，靠近提示词顶部）
    //   2. depth   — 升序（数值小的在前）
    //   3. contentHash — 升序（兜底，确保相同 order+depth 的条目顺序唯一确定）
    const _deterministicSort = (a, b) => {
        const orderDiff = (a.order ?? 100) - (b.order ?? 100);
        if (orderDiff !== 0) return orderDiff;

        const depthDiff = (a.depth ?? DEFAULT_DEPTH) - (b.depth ?? DEFAULT_DEPTH);
        if (depthDiff !== 0) return depthDiff;

        return (a.contentHash ?? 0) - (b.contentHash ?? 0);
    };

    result.wiBefore.sort(_deterministicSort);
    result.wiAfter.sort(_deterministicSort);
    result.wiDepth.sort(_deterministicSort);
    result.wiANTop.sort(_deterministicSort);
    result.wiANBottom.sort(_deterministicSort);
    result.wiEMTop.sort(_deterministicSort);
    result.wiEMBottom.sort(_deterministicSort);
    for (const outletName of Object.keys(result.wiOutlet)) {
        result.wiOutlet[outletName].sort(_deterministicSort);
    }

    // ─── 第三步：join 拼接为最终字符串 ───
    result.wiBefore = result.wiBefore.map(e => e.content).join('\n');
    result.wiAfter = result.wiAfter.map(e => e.content).join('\n');
    result.wiANTop = result.wiANTop.map(e => e.content).join('\n');
    result.wiANBottom = result.wiANBottom.map(e => e.content).join('\n');
    // wiDepth 保留结构化数组（含 depth/content/role），由 TavernCoreV2 消费

    return result;
}

// ============================================================
// 主入口函数
// ============================================================

/**
 * 执行完整的世界书扫描流程。
 *
 * @param {object[]} entries - 所有条目数组
 * @param {string} scanText - 初始扫描文本
 * @param {object} [config={}] - 配置参数
 * @param {number} [config.maxDepth=2] - 最大递归深度
 * @param {number} [config.budget=25] - 预算百分比
 * @param {number} [config.maxContext=2048] - 最大上下文 token 数
 * @param {number} [config.budgetCap=0] - 预算上限
 * @param {boolean} [config.recursive=true] - 是否启用递归
 * @param {object} [config.globalOptions={}] - 全局匹配选项
 * @param {number} [config.defaultDepth=4] - 默认深度
 * @param {object} [context={}] - 上下文信息
 * @param {number} [context.group_id] - 群组 ID
 * @param {string[]} [context.active_uids] - 活跃用户 ID 列表
 * @param {object[]} [context.semantic_hits] - 语义命中条目（直接激活，跳过关键词匹配）
 * @returns {object} - 分类打包后的结果
 */
function scanLorebook(entries, scanText, config = {}, context = {}) {
    // 1. 执行递归扫描，获取所有激活的条目
    const activatedEntries = recursiveScan(entries, scanText, config, context);

    // 2. 按位置分类打包（内部已执行确定性排序）
    const result = classifyByPosition(activatedEntries, config);

    return result;
}

// ============================================================
// 导出
// ============================================================

module.exports = {
    world_info_logic,
    world_info_position,
    DEFAULT_DEPTH,
    MAX_SCAN_DEPTH,
    escapeRegex,
    parseRegexFromString,
    transformString,
    matchKey,
    checkKeywords,
    recursiveScan,
    classifyByPosition,
    scanLorebook,
};
