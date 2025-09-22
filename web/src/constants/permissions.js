// 权限清单（去除可见模块 view.*）
// - type: 'action' | 'flag'（flag 默认允许；action 默认拒绝）
// - danger: 高危操作，UI 高亮
// - group: 用于界面分组

export const PERMISSIONS = [
  // 账户与安全
  { key: 'changePasswordSelf', label: '允许修改自己密码', group: '账户与安全', type: 'flag' },
  { key: 'createUsers', label: '允许新增用户', group: '账户与安全', type: 'action', danger: true },
  { key: 'editUsers', label: '允许编辑用户权限', group: '账户与安全', type: 'action', danger: true },

  // 监控与配额
  { key: 'viewGlobalRate', label: '查看全局配额使用', group: '监控与配额', type: 'action' },
  { key: 'viewHomeStatus', label: '查看首页日志/状态', group: '监控与配额', type: 'action' },

  // 设置配置（原“设置与集成”，统一标记为高危）
  { key: 'editTmdb', label: '编辑 TMDB 配置', group: '设置配置', type: 'action', danger: true },
  { key: 'editTvdb', label: '编辑 TVDB 配置', group: '设置配置', type: 'action', danger: true },
  { key: 'editDouban', label: '编辑 豆瓣 配置', group: '设置配置', type: 'action', danger: true },
  { key: 'editBangumi', label: '编辑 Bangumi 配置', group: '设置配置', type: 'action', danger: true },
  { key: 'editProxy', label: '编辑 反向代理 设置', group: '设置配置', type: 'action', danger: true },
  { key: 'editTrustedProxies', label: '编辑 受信任反代', group: '设置配置', type: 'action', danger: true },
  { key: 'editWebhook', label: '编辑 Webhook 设置', group: '设置配置', type: 'action', danger: true },
  { key: 'editCustomDomain', label: '编辑 自定义域名', group: '设置配置', type: 'action', danger: true },
  { key: 'editUaFilter', label: '编辑 全局 UA 过滤', group: '设置配置', type: 'action', danger: true },

  // 弹幕/库
  { key: 'editLibrary', label: '编辑 弹幕库', group: '任务/弹幕/搜索/外部控制', type: 'action', danger: true },
  { key: 'editDanmakuOutput', label: '编辑 弹幕token', group: '任务/弹幕/搜索/外部控制', type: 'action', danger: true },

  // 任务
  { key: 'editScheduledTasks', label: '编辑 定时任务', group: '任务/弹幕/搜索/外部控制', type: 'action', danger: true },
  { key: 'editWebhookTasks', label: '编辑 Webhook 任务', group: '任务/弹幕/搜索/外部控制', type: 'action', danger: true },

  // 搜索源/外部控制
  { key: 'editScrapers', label: '编辑 搜索源', group: '任务/弹幕/搜索/外部控制', type: 'action', danger: true },
  { key: 'editControl', label: '编辑 外部控制', group: '任务/弹幕/搜索/外部控制', type: 'action', danger: true },
]

export const GROUP_ORDER = [
  '账户与安全', '监控与配额', '设置配置', '任务/弹幕/搜索/外部控制'
]

export const KEY_TO_META = PERMISSIONS.reduce((m, p) => { m[p.key] = p; return m }, {})

export function isDefaultAllow(key) {
  const p = KEY_TO_META[key]
  if (!p) return false
  return p.type === 'flag' // 仅 flag 默认允许
}

export function defaultBoolOf(key) {
  return isDefaultAllow(key) ? true : false
}

export function groupBy(arr, fn) {
  return arr.reduce((acc, item) => {
    const k = fn(item)
    acc[k] = acc[k] || []
    acc[k].push(item)
    return acc
  }, {})
}
