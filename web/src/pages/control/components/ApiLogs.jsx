import { Card, Table, Tag } from 'antd'
import { useEffect, useState } from 'react'
import { getControlApiKeyLog } from '../../../apis'
import dayjs from 'dayjs'

export const ApiLogs = () => {
  const [loading, setLoading] = useState(true)
  const [logs, setLogs] = useState([])

  useEffect(() => {
    setLoading(true)
    getControlApiKeyLog()
      .then(res => {
        setLogs(res.data ?? [])
      })
      .finally(() => {
        setLoading(false)
      })
  }, [])

  const columns = [
    {
      title: '访问时间',
      dataIndex: 'accessTime',
      key: 'accessTime',
      width: 200,
      render: (_, record) => {
        return (
          <div>{dayjs(record.accessTime).format('YYYY-MM-DD HH:mm:ss')}</div>
        )
      },
    },
    {
      title: 'IP地址',
      dataIndex: 'ipAddress',
      key: 'ipAddress',
      width: 150,
    },
    {
      title: '端点',
      dataIndex: 'endpoint',
      key: 'endpoint',
      width: 200,
    },
    {
      title: '状态码',
      width: 200,
      dataIndex: 'statusCode',
      key: 'statusCode',
      render: (_, record) => {
        return (
          <Tag color={record.statusCode >= 400 ? 'red' : 'green'}>
            {record.statusCode}
          </Tag>
        )
      },
    },
    {
      title: '消息',
      dataIndex: 'message',
      key: 'message',
      width: 400,
    },
  ]

  return (
    <div className="my-6">
      <Card title="API访问日志" loading={loading}>
        <div className="mb-4">这里显示最近100条通过外部API的访问记录。</div>
        <Table
          pagination={false}
          size="small"
          dataSource={logs}
          columns={columns}
          rowKey={'accessTime'}
          scroll={{
            x: '100%',
            y: 400,
          }}
        />
      </Card>
    </div>
  )
}
