import { useState, useEffect, useCallback, useMemo } from 'react'
import { List, Button, Tag, Space, Card, Checkbox, Empty, Tooltip, Input } from 'antd'
import { DeleteOutlined, CheckOutlined, MinusOutlined, PlayCircleOutlined } from '@ant-design/icons'
import dayjs from 'dayjs'
import { getWebhookTasks, deleteWebhookTasks, runWebhookTasksNow } from '../../../apis'
import { useMessage } from '../../../MessageContext'
import { useModal } from '../../../ModalContext'

const getStatusTagType = status => {
  if (status === 'pending') return 'processing'
  if (status === 'submitted') return 'success'
  if (status === 'failed') return 'error'
  return 'default'
}

const translateStatus = status => {
  const statusMap = {
    pending: '待处理',
    submitted: '已提交',
    processing: '处理中',
    failed: '失败',
  }
  return statusMap[status] || status
}

export const WebhookTasks = () => {
  const [loading, setLoading] = useState(true)
  const [taskList, setTaskList] = useState([])
  const [selectedTasks, setSelectedTasks] = useState([])
  const [pagination, setPagination] = useState({
    current: 1,
    pageSize: 20,
    total: 0,
  })
  const [searchTerm, setSearchTerm] = useState('')
  const messageApi = useMessage()
  const modalApi = useModal()

  const fetchTasks = useCallback(async () => {
    setLoading(true)
    try {
      const { data } = await getWebhookTasks({
        search: searchTerm,
        page: pagination.current,
        pageSize: pagination.pageSize,
      })
      setTaskList(data.list || [])
      setPagination(prev => ({ ...prev, total: data.total || 0 }))
    } catch (error) {
      messageApi.error('获取 Webhook 任务列表失败')
    } finally {
      setLoading(false)
    }
  }, [messageApi, pagination.current, pagination.pageSize, searchTerm])

  useEffect(() => {
    fetchTasks()
  }, [fetchTasks])

  const handleSelectionChange = (task, checked) => {
    setSelectedTasks(prev =>
      checked ? [...prev, task] : prev.filter(t => t.id !== task.id)
    )
  }

  const handleSelectAll = () => {
    if (selectedTasks.length === taskList.length) {
      setSelectedTasks([])
    } else {
      setSelectedTasks(taskList)
    }
  }

  const handleBulkDelete = () => {
    modalApi.confirm({
      title: '批量删除任务',
      content: `确定要删除选中的 ${selectedTasks.length} 个任务吗？`,
      onOk: async () => {
        try {
          const ids = selectedTasks.map(task => task.id)
          await deleteWebhookTasks({ ids })
          messageApi.success('批量删除成功')
          setSelectedTasks([])
          fetchTasks()
        } catch (error) {
          messageApi.error('批量删除失败')
        }
      },
    })
  }

  const handleRunNow = () => {
    modalApi.confirm({
      title: '立即执行任务',
      content: `确定要立即执行选中的 ${selectedTasks.length} 个待处理任务吗？`,
      onOk: async () => {
        try {
          const ids = selectedTasks.map(task => task.id)
          await runWebhookTasksNow({ ids })
          messageApi.success('任务已提交执行')
          setSelectedTasks([])
          // 刷新列表以更新状态
          fetchTasks()
        } catch (error) {
          messageApi.error('提交执行失败')
        }
      },
    })
  }

  const selectedTaskIds = useMemo(() => new Set(selectedTasks.map(t => t.id)), [
    selectedTasks,
  ])

  return (
    <div className="my-6">
      <Card
        loading={loading}
        title="Webhook 任务列表"
        extra={
          <Space>
            <Tooltip title="全选/取消全选">
              <Button
                type="default"
                shape="circle"
                icon={
                  selectedTasks.length === taskList.length &&
                  !!selectedTasks.length ? (
                    <CheckOutlined />
                  ) : (
                    <MinusOutlined />
                  )
                }
                onClick={handleSelectAll}
              />
            </Tooltip>
            <Tooltip title="立即执行选中任务">
              <Button
                type="primary"
                shape="circle"
                icon={<PlayCircleOutlined />}
                disabled={selectedTasks.length === 0}
                onClick={handleRunNow}
              />
            </Tooltip>
            <Tooltip title="批量删除">
              <Button
                danger
                type="primary"
                shape="circle"
                icon={<DeleteOutlined />}
                disabled={selectedTasks.length === 0}
                onClick={handleBulkDelete}
              />
            </Tooltip>
            <Input.Search
              placeholder="搜索任务标题"
              onSearch={value => {
                setSearchTerm(value)
                setPagination(prev => ({ ...prev, current: 1 }))
              }}
              style={{ width: 200 }}
            />
          </Space>
        }
      >
        <div>
          {taskList.length > 0 ? (
            <List
              itemLayout="vertical"
              size="small"
              dataSource={taskList}
              pagination={{
                ...pagination,
                align: 'center',
                showSizeChanger: true,
                pageSizeOptions: ['20', '50', '100'],
                onChange: (page, pageSize) => {
                  setPagination(prev => ({ ...prev, current: page, pageSize }))
                },
              }}
              renderItem={item => {
                const isSelected = selectedTaskIds.has(item.id)
                return (
                  <List.Item
                    key={item.id}
                    onClick={() => handleSelectionChange(item, !isSelected)}
                    className="!cursor-pointer hover:!bg-gray-100"
                    extra={
                      <Tag color={getStatusTagType(item.status)}>
                        {translateStatus(item.status)}
                      </Tag>
                    }
                  >
                    <div className="relative pl-8">
                      <Checkbox
                        checked={isSelected}
                        className="absolute top-1/2 left-0 transform -translate-y-1/2"
                      />
                      <div className="text-base mb-1">{item.taskTitle}</div>
                      <div className="text-gray-500 text-sm">
                        <span>来源: {item.webhookSource}</span>
                        <span className="mx-2">|</span>
                        <span>
                          接收于: {dayjs(item.receptionTime).format('YYYY-MM-DD HH:mm:ss')}
                        </span>
                        <span className="mx-2">|</span>
                        <span>
                          计划于: {dayjs(item.executeTime).format('YYYY-MM-DD HH:mm:ss')}
                        </span>
                      </div>
                    </div>
                  </List.Item>
                )
              }}
            />
          ) : (
            <Empty description="没有待处理的 Webhook 任务" />
          )}
        </div>
      </Card>
    </div>
  )
}