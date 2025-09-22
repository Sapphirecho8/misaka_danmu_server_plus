import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  deleteTask,
  getTaskList,
  pauseTask,
  resumeTask,
  stopTask,
} from '@/apis'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Button,
  Card,
  Checkbox,
  Empty,
  Input,
  List,
  message,
  Modal,
  Progress,
  Space,
  Tag,
  Tooltip,
} from 'antd'
import {
  CheckOutlined,
  DeleteOutlined,
  MinusOutlined,
  PauseOutlined,
  StepBackwardOutlined,
  StopOutlined,
} from '@ant-design/icons'
import classNames from 'classnames'
import { useModal } from '../../../ModalContext'
import { useMessage } from '../../../MessageContext'
// 移除useScroll导入，改用分页模式

export const ImportTask = () => {
  const [loading, setLoading] = useState(true)
  const [taskList, setTaskList] = useState([])
  const [selectList, setSelectList] = useState([])
  const timer = useRef()

  const [pagination, setPagination] = useState({
    current: 1,
    pageSize: 100,
    total: 0,
  })

  const navigate = useNavigate()
  const modalApi = useModal()
  const messageApi = useMessage()

  const [canPause, isPause] = useMemo(() => {
    return [
      (selectList.every(item => item.status === '运行中') &&
        !!selectList.length) ||
        (selectList.every(item => item.status === '已暂停') &&
          !!selectList.length),
      selectList.every(item => item.status === '已暂停'),
    ]
  }, [selectList])

  const canDelete = useMemo(() => {
    return (
      selectList.every(
        item =>
          item.status === '已完成' ||
          item.status === '失败' ||
          item.status === '排队中'
      ) && !!selectList.length
    )
  }, [selectList])

  const [searchParams] = useSearchParams()

  const [search, status] = useMemo(() => {
    return [
      searchParams.get('search') ?? '',
      searchParams.get('status') ?? 'in_progress',
    ]
  }, [searchParams])

  useEffect(() => {
    setPagination(n => ({
      ...n,
      pageSize: 100,
      current: 1,
    }))
  }, [search, status])



  /**
   * 轮询刷新当前页面任务列表
   */
  const pollTasks = useCallback(async () => {
    try {
      const res = await getTaskList({
        search,
        status,
        page: pagination.current,
        pageSize: pagination.pageSize,
      })

      const newData = res.data?.list || []
      setTaskList(newData)


    } catch (error) {
      console.error('轮询获取数据失败:', error)
    }
  }, [search, status, pagination.current, pagination.pageSize])

  /**
   * 刷新任务列表
   */
  const refreshTasks = useCallback(async () => {
    try {
      setLoading(true)

      const res = await getTaskList({
        search,
        status,
        page: pagination.current,
        pageSize: pagination.pageSize,
      })

      const newData = res.data?.list || []
      setTaskList(newData)


      setLoading(false)
      setPagination(prev => ({
        ...prev,
        total: res.data?.total || 0,
      }))
    } catch (error) {
      console.error(error)
      setLoading(false)
    }
  }, [search, status, pagination.current, pagination.pageSize])

  /**
   * 处理暂停/恢复任务操作
   */
  const handlePause = async () => {
    if (isPause) {
      try {
        await Promise.all(
          selectList.map(it => resumeTask({ taskId: it.taskId }))
        )
        refreshTasks()
        setSelectList([])
      } catch (error) {
        message.error(`操作失败: ${error.message}`)
      }
    } else {
      try {
        await Promise.all(
          selectList.map(it => pauseTask({ taskId: it.taskId }))
        )
        refreshTasks()
        setSelectList([])
      } catch (error) {
        message.error(`操作失败: ${error.message}`)
      }
    }
  }

  /**
   * 处理中止任务操作
   */
  const handleStop = () => {
    modalApi.confirm({
      title: '中止任务',
      zIndex: 1002,
      content: (
        <div>
          您确定要中止任务任务吗？
          <br />
          此操作会尝试停止任务，如果无法停止，则会将其强制标记为“失败”状态。
          <div className="max-h-[310px] overflow-y-auto mt-3">
            {selectList.map((it, i) => (
              <div>
                {i + 1}、{it.title}
              </div>
            ))}
          </div>
        </div>
      ),
      okText: '确认',
      cancelText: '取消',
      onOk: async () => {
        try {
          await Promise.all(
            selectList.map(it => stopTask({ taskId: it.taskId }))
          )
          refreshTasks()
          setSelectList([])
          messageApi.success('中止成功')
        } catch (error) {
          messageApi.error(`中止任务失败: ${error.message}`)
        }
      },
    })
  }

  /**
   * 处理删除任务操作
   */
  const handleDelete = () => {
    modalApi.confirm({
      title: '删除任务',
      zIndex: 1002,
      content: (
        <div>
          您确定要从历史记录中删除任务吗？
          <div className="max-h-[310px] overflow-y-auto mt-3">
            {selectList.map((it, i) => (
              <div>
                {i + 1}、{it.title}
              </div>
            ))}
          </div>
        </div>
      ),
      okText: '确认',
      cancelText: '取消',
      onOk: async () => {
        try {
          await Promise.all(
            selectList.map(it => deleteTask({ taskId: it.taskId }))
          )
          refreshTasks()
          setSelectList([])
          messageApi.success('删除成功')
        } catch (error) {
          messageApi.error(`删除任务失败: ${error.message}`)
        }
      },
    })
  }

  useEffect(() => {
    const isLoadMore = pagination.current > 1
    refreshTasks(isLoadMore)
    if (!isLoadMore) {
      setSelectList([])
    }
  }, [search, status, pagination.current, pagination.pageSize])

  useEffect(() => {
    // 清除之前的定时器
    clearInterval(timer.current)

    // 启动轮询定时器，每3秒刷新当前页面任务列表
    timer.current = setInterval(() => {
      pollTasks()
    }, 3000)

    return () => {
      clearInterval(timer.current)
    }
  }, [pollTasks])

  return (
    <div className="my-6">
      <Card
        loading={loading}
        title="任务管理器"
        extra={
          <Space>
            <Tooltip title="全选/取消全选">
              <Button
                type="default"
                shape="circle"
                icon={
                  selectList.length === taskList.length &&
                  !!selectList.length ? (
                    <CheckOutlined />
                  ) : (
                    <MinusOutlined />
                  )
                }
                onClick={() => {
                  if (
                    selectList.length === taskList.length &&
                    !!selectList.length
                  ) {
                    setSelectList([])
                  } else {
                    setSelectList(taskList)
                  }
                }}
              />
            </Tooltip>
            <Tooltip title="启用/暂停任务">
              <Button
                disabled={!canPause}
                type="default"
                shape="circle"
                icon={isPause ? <PauseOutlined /> : <StepBackwardOutlined />}
                onClick={handlePause}
              />
            </Tooltip>
            <Tooltip title="删除任务">
              <Button
                disabled={!canDelete}
                type="default"
                shape="circle"
                icon={<DeleteOutlined />}
                onClick={handleDelete}
              />
            </Tooltip>
            <Tooltip title="中止任务">
              <Button
                disabled={!canPause}
                type="default"
                shape="circle"
                icon={<StopOutlined />}
                onClick={handleStop}
              />
            </Tooltip>

            <Input.Search
              placeholder="按任务标题搜索"
              allowClear
              enterButton
              onSearch={value => {
                navigate(`/task?search=${value}&status=${status}`, {
                  replace: true,
                })
              }}
            />
          </Space>
        }
      >
        <div className="flex items-center justify-center gap-4 py-3 text-base font-semibold">
          <div
            className={classNames('cursor-pointer px-3 py-1 rounded-full', {
              'bg-primary text-white': status === 'all',
            })}
            onClick={() => {
              navigate(`/task?search=${search}&status=all`, {
                replace: true,
              })
            }}
          >
            全部
          </div>
          <div
            className={classNames('cursor-pointer px-3 py-1 rounded-full', {
              'bg-primary text-white': status === 'completed',
            })}
            onClick={() => {
              navigate(`/task?search=${search}&status=completed`, {
                replace: true,
              })
            }}
          >
            已完成
          </div>
          <div
            className={classNames('cursor-pointer px-3 py-1 rounded-full', {
              'bg-primary text-white': status === 'in_progress',
            })}
            onClick={() => {
              navigate(`/task?search=${search}&status=in_progress`, {
                replace: true,
              })
            }}
          >
            进行中
          </div>
        </div>
        <div>
          {!!taskList?.length ? (
            <List
              itemLayout="vertical"
              size="small"
              dataSource={taskList}
              pagination={{
                ...pagination,
                showLessItems: true,
                align: 'center',
                onChange: (page, pageSize) => {
                  setPagination(n => {
                    return {
                      ...n,
                      current: page,
                      pageSize,
                    }
                  })
                },
                onShowSizeChange: (_, size) => {
                  setPagination(n => {
                    return {
                      ...n,
                      pageSize: size,
                    }
                  })
                },
                hideOnSinglePage: true,
              }}
              renderItem={(item, index) => {
                const isActive = selectList.some(
                  it => it.taskId === item.taskId
                )

                return (
                  <List.Item
                    key={index}
                    extra={
                      <>
                        <Tag
                          className="!mb-3"
                          color={item.status.includes('失败') ? 'red' : 'green'}
                        >
                          {item.status}
                        </Tag>
                      </>
                    }
                    onClick={() => {
                      setSelectList(list => {
                        return list.map(it => it.taskId).includes(item.taskId)
                          ? list.filter(i => i.taskId !== item.taskId)
                          : [...list, item]
                      })
                    }}
                  >
                    <div
                      className={classNames('relative', {
                        'pl-9': isActive,
                      })}
                    >
                      {isActive && (
                        <Checkbox
                          checked={isActive}
                          className="absolute top-1/2 left-0 transform -translate-y-1/2"
                        />
                      )}

                      <div className="text-base mb-1">{item.title}</div>
                      <div className="mb-2">{item.description}</div>
                      <Progress
                        percent={item.progress}
                        status={item.status.includes('失败') && 'exception'}
                      />
                    </div>
                  </List.Item>
                )
              }}
            />
          ) : (
            <Empty description="没有符合条件的任务" />
          )}
        </div>
      </Card>
    </div>
  )
}
