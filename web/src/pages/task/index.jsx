import { ImportTask } from './components/ImportTask'
import { ScheduleTask } from './components/ScheduleTask'
import { RateLimitPanel } from './components/RateLimitPanel'
import { WebhookTasks } from './components/WebhookTasks'
import { Tabs } from 'antd'
import { useNavigate, useSearchParams } from 'react-router-dom'

import { useAtomValue } from 'jotai'
import { userinfoAtom } from '../../../store/index.js'

export const Task = () => {
  const [searchParams] = useSearchParams()
  const key = searchParams.get('key') || 'task'

  const navigate = useNavigate()
  const userinfo = useAtomValue(userinfoAtom)
  const perms = userinfo?.permissions || {}

  return (
    <Tabs
      defaultActiveKey={key}
      items={[
        {
          label: '进行中的任务',
          key: 'task',
          children: <ImportTask />,
        },
        {
          label: 'Webhook 任务',
          key: 'webhook',
          children: <WebhookTasks />,
        },
        {
          label: '定时任务',
          key: 'schedule',
          children: <ScheduleTask />,
        },
        {
          label: '流控面板',
          key: 'ratelimit',
          children: <RateLimitPanel />,
        },
      ].filter(it => {
        if (it.key === 'task') return perms['view.tasks.running'] !== false
        if (it.key === 'ratelimit') return perms['view.tasks.ratepanel'] !== false
        if (it.key === 'webhook') return (userinfo?.username?.toLowerCase?.() === 'admin') || !!perms['editWebhookTasks']
        if (it.key === 'schedule') return (userinfo?.username?.toLowerCase?.() === 'admin') || !!perms['editScheduledTasks']
        return true
      })}
      onChange={key => {
        navigate(`/task?key=${key}`, {
          replace: true,
        })
      }}
    />
  )
}
