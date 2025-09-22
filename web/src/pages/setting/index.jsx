import { Tabs } from 'antd'
import { Security } from './components/Security'
import { Webhook } from './components/Webhook'
import { Bangumi } from './components/Bangumi'
import { TMDB } from './components/TMDB'
import { Douban } from './components/Douban'
import { TVDB } from './components/TVDB'
import { Proxy } from './components/Proxy'
import { Accounts } from './components/Accounts'
import { Invites } from './components/Invites'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useAtomValue } from 'jotai'
import { userinfoAtom } from '../../../store/index.js'

export const Setting = () => {
  const [searchParams] = useSearchParams()
  const key = searchParams.get('key') || 'security'
  const navigate = useNavigate()
  const userinfo = useAtomValue(userinfoAtom)

  const perms = userinfo?.permissions || {}
  const isSuperAdmin = ((userinfo?.username || '').toLowerCase() === 'admin')
  const canEditTmdb = isSuperAdmin || !!perms.editTmdb
  const canEditProxy = isSuperAdmin || !!perms.editProxy
  const canEditWebhook = isSuperAdmin || !!perms.editWebhook
  const canEditBangumi = isSuperAdmin || !!perms.editBangumi
  const canEditDouban = isSuperAdmin || !!perms.editDouban
  const canEditTvdb = isSuperAdmin || !!perms.editTvdb
  // 账户管理（新增用户权限）：仅主管理员或具有 createUsers 权限的用户可进入
  const canCreateUsers = isSuperAdmin || !!perms.createUsers

  const initialKey = (!canCreateUsers && key === 'accounts') ? 'security' : key
  return (
    <Tabs
      defaultActiveKey={initialKey}
      items={[
        {
          label: '账户安全',
          key: 'security',
          children: <Security />,
        },
        // 账户管理：仅拥有“新增用户权限”的用户可进入；否则置灰
      {
          label: '账户管理',
          key: 'accounts',
          children: <Accounts />,
          disabled: !canCreateUsers,
        },
        // 邀请注册管理，位于账户管理与反向代理之间
        {
          label: '邀请注册',
          key: 'invites',
          children: <Invites />,
          disabled: !canCreateUsers,
        },
        canEditProxy && {
          label: '反向代理设置',
          key: 'proxy',
          children: <Proxy />,
        },
        {
          label: 'Webhook',
          key: 'webhook',
          children: <Webhook />,
          disabled: !canEditWebhook,
        },
        {
          label: 'Bangumi配置',
          key: 'bangumi',
          children: <Bangumi />,
          disabled: !canEditBangumi,
        },
        {
          label: 'TMDB配置',
          key: 'tmdb',
          children: <TMDB />,
          disabled: !canEditTmdb,
        },
        {
          label: '豆瓣配置',
          key: 'douban',
          children: <Douban />,
          disabled: !canEditDouban,
        },
        {
          label: 'TVDB配置',
          key: 'tvdb',
          children: <TVDB />,
          disabled: !canEditTvdb,
        },
      ].filter(Boolean)}
      onChange={key => {
        navigate(`/setting?key=${key}`, {
          replace: true,
        })
      }}
    />
  )
}
