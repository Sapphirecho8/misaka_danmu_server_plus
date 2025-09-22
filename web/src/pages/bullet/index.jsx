import { Tabs } from 'antd'
import { TokenManage } from './components/TokenManage'
import { OutputManage } from './components/OutputManage'
import { useAtomValue } from 'jotai'
import { userinfoAtom } from '../../../store/index.js'
import { useNavigate, useSearchParams } from 'react-router-dom'

export const Bullet = () => {
  const [searchParams] = useSearchParams()
  const key = searchParams.get('key') || 'token'
  const navigate = useNavigate()
  const userinfo = useAtomValue(userinfoAtom)
  const perms = userinfo?.permissions || {}

  const isSuper = (userinfo?.username || '').toLowerCase() === 'admin'
  const canEditOutput = isSuper || !!perms.editDanmakuOutput

  return (
    <Tabs
      defaultActiveKey={key}
      items={[
        {
          label: 'Token管理',
          key: 'token',
          children: <TokenManage />,
        },
        // 输出控制：需 editDanmakuOutput 或 admin
        canEditOutput && {
          label: '弹幕输出控制',
          key: 'output',
          children: <OutputManage />,
        },
      ].filter(Boolean)}
      onChange={key => {
        navigate(`/bullet?key=${key}`, {
          replace: true,
        })
      }}
    />
  )
}
