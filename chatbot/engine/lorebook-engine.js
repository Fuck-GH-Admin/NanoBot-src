
/**

 * lorebook-engine.js

 *

 * 纯函数的世界书（Lorebook）核心扫描与触发引擎。

 * 不依赖任何 DOM、全局变量或 UI 渲染。

 * 输入：条目数组、扫描文本、配置参数。

 * 输出：触发结果（布尔值）或分类打包后的内容对象。

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

 * 支持 AND、OR、NOT、Selective 逻辑。

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

// 提取正向关键词和反向(排除)关键词
    const mainKeys = entry.key.filter(k => !k.startsWith('-'));
    const notKeys = entry.key.filter(k => k.startsWith('-')).map(k => k.slice(1));

    // 1. 检查排除词 (NOT逻辑)：如果命中任何一个排除词，直接否决！
    for (const key of notKeys) {
        if (matchKey(scanText, key, matchOptions)) {
            return false; 
        }
    }

    // 2. 检查主关键词 (OR逻辑)：必须命中至少一个正向词
    let primaryMatch = mainKeys.length === 0; // 如果没有正向词，默认通过
    for (const key of mainKeys) {
        if (matchKey(scanText, key, matchOptions)) {
            primaryMatch = true;
            break;
        }
    }

    if (!primaryMatch) {
        return false;
    }



    if (!primaryMatch) {

        return false;

    }



    // 如果没有次要关键词，直接返回 true

    if (!Array.isArray(entry.keysecondary) || entry.keysecondary.length === 0) {

        return true;

    }



    // 处理次要关键词逻辑

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

 * @returns {object[]} - 被触发的条目数组

 */

function recursiveScan(entries, initialScanText, config = {}) {

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



    return Array.from(activatedEntries.values());

}



// ============================================================

// C. 预算与位置分类器

// ============================================================



/**

 * 根据条目的 position 设置，将激活的条目分类打包。

 *

 * @param {object[]} activatedEntries - 被触发的条目数组

 * @param {object} [options={}]

 * @param {number} [options.defaultDepth=4] - 默认深度

 * @returns {object} - 分类打包后的结果

 */

function classifyByPosition(activatedEntries, options = {}) {

    const { defaultDepth = DEFAULT_DEPTH } = options;



    const result = {

        wiBefore: '',

        wiAfter: '',

        wiDepth: [],

        wiANTop: [],

        wiANBottom: [],

        wiEMTop: [],

        wiEMBottom: [],

        wiOutlet: {},

    };



    // 按 order 降序排序（高优先级在前）

    const sorted = [...activatedEntries].sort((a, b) => (b.order ?? 100) - (a.order ?? 100));



    for (const entry of sorted) {

        const content = entry.content || '';

        if (!content) continue;



        const position = entry.position ?? world_info_position.before;

        const depth = entry.depth ?? defaultDepth;



        switch (position) {

            case world_info_position.before:

                result.wiBefore = content + '\n' + result.wiBefore;

                break;

            case world_info_position.after:

                result.wiAfter = content + '\n' + result.wiAfter;

                break;

            case world_info_position.atDepth: {

                const existing = result.wiDepth.find(d => d.depth === depth && d.role === (entry.role ?? 0));

                if (existing) {

                    existing.content = content + '\n' + existing.content;

                } else {

                    result.wiDepth.push({

                        depth: depth,

                        content: content,

                        role: entry.role ?? 0,

                    });

                }

                break;

            }

            case world_info_position.ANTop:

                result.wiANTop.unshift(content);

                break;

            case world_info_position.ANBottom:

                result.wiANBottom.unshift(content);

                break;

            case world_info_position.EMTop:

                result.wiEMTop.unshift({ position: 0, content });

                break;

            case world_info_position.EMBottom:

                result.wiEMBottom.unshift({ position: 1, content });

                break;

            case world_info_position.outlet: {

                const outletName = entry.outletName || 'default';

                if (!result.wiOutlet[outletName]) {

                    result.wiOutlet[outletName] = [];

                }

                result.wiOutlet[outletName].unshift(content);

                break;

            }

            default:

                result.wiBefore = content + '\n' + result.wiBefore;

                break;

        }

    }



    // 将数组转换为字符串

    result.wiANTop = result.wiANTop.join('\n');

    result.wiANBottom = result.wiANBottom.join('\n');



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

 * @returns {object} - 分类打包后的结果

 */

function scanLorebook(entries, scanText, config = {}) {

    // 1. 执行递归扫描，获取所有激活的条目

    const activatedEntries = recursiveScan(entries, scanText, config);



    // 2. 按位置分类打包

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