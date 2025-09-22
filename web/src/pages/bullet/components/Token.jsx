import {
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  message,
  Modal,
  Select,
  Space,
  Progress,
  Table,
  Tag,
  Tooltip,
} from 'antd'
import { useEffect, useState } from 'react'
import { useAtomValue } from 'jotai'
import { userinfoAtom } from '../../../../store/index.js'
import {
  addToken,
  deleteToken,
  editToken,
  getCustomDomain,
  getTokenList,
  getTokenLog,
  resetTokenCounter,
  toggleTokenStatus,
  lockToken,
  unlockToken,
} from '../../../apis'
import dayjs from 'dayjs'
import { MyIcon } from '@/components/MyIcon.jsx'
import copy from 'copy-to-clipboard'
import { EyeInvisibleOutlined, EyeTwoTone, LockOutlined, UnlockOutlined } from '@ant-design/icons'
import { useModal } from '../../../ModalContext'
import { useMessage } from '../../../MessageContext'

export const Token = () => {
  const [loading, setLoading] = useState(false)
  const [tokenList, setTokenList] = useState([])
  const [isModalOpen, setIsModalOpen] = useState(false)
  const [isEditing, setIsEditing] = useState(false)
  const [editingRecord, setEditingRecord] = useState(null)
  const [confirmLoading, setConfirmLoading] = useState(false)
  const [form] = Form.useForm()
  const [tokenLogs, setTokenLogs] = useState([])
  const [logsOpen, setLogsOpen] = useState(false)
  const [domain, setDomain] = useState('')
  const modalApi = useModal()
  const messageApi = useMessage()
  const userinfo = useAtomValue(userinfoAtom)
  const perms = userinfo?.permissions || {}
  const isSuper = (userinfo?.username || '').toLowerCase() === 'admin'
  const canEditAllTokens = isSuper || !!perms.editDanmakuOutput

  const getTokens = async () => {
    try {
      setLoading(true)
      const [tokenRes, domainRes] = await Promise.all([
        getTokenList(),
        getCustomDomain(),
      ])
      setTokenList(tokenRes.data)
      setDomain(domainRes.data?.value ?? '')
    } catch (error) {
      console.error(error)
    } finally {
      setLoading(false)
    }
  }

  const handleTokenLogs = async record => {
    if (record.isLocked && !canEditAllTokens) {
      messageApi.error('该Token已锁死，禁止查看访问日志')
      return
    }
    try {
      const res = await getTokenLog({
        tokenId: record.id,
      })
      setTokenLogs(res.data)
      setLogsOpen(true)
    } catch (error) {
      messageApi.error('获取日志失败')
    }
  }

  const handleToggleStatus = async record => {
    if (record.isLocked) {
      messageApi.error('该Token已锁死，无法切换状态，请先解除锁定')
      return
    }
    try {
      await toggleTokenStatus({
        tokenId: record.id,
      })
      getTokens()
    } catch (error) {
      messageApi.error('操作失败')
    }
  }

  const handleDelete = record => {
    if (record.isLocked) {
      messageApi.error('该Token已锁死，无法删除，请先解除锁定')
      return
    }
    modalApi.confirm({
      title: '删除',
      zIndex: 1002,
      content: <div>您确定要删除{record.name}吗？</div>,
      okText: '确认',
      cancelText: '取消',
      onOk: async () => {
        try {
          await deleteToken({
            tokenId: record.id,
          })
          getTokens()
          messageApi.success('删除成功')
        } catch (error) {
          console.error(error)
          messageApi.error('删除失败')
        }
      },
    })
  }

  const handleOpenModal = (editing = false, record = null) => {
    if (editing && record?.isLocked) {
      messageApi.error('该Token已锁死，无法编辑')
      return
    }
    setIsEditing(editing)
    setEditingRecord(record)
    if (editing && record) {
      form.setFieldsValue({
        name: record.name,
        dailyCallLimit: record.dailyCallLimit,
        validityPeriod: 'custom', // 默认不改变有效期
        scope: record.ownerUserId ? 'private' : 'global',
      })
    } else {
      form.resetFields()
      form.setFieldsValue({
        validityPeriod: 'permanent',
        dailyCallLimit: 500,
        genMode: 'random',
        scope: 'private',
      })
    }
    setIsModalOpen(true)
  }

  const handleSave = async () => {
    try {
      const values = await form.validateFields()
      setConfirmLoading(true)
      if (isEditing && editingRecord) {
        const payload = { ...values, id: editingRecord.id }
        if (!canEditAllTokens) {
          delete payload.scope
        }
        await editToken(payload)
        messageApi.success('编辑成功')
      } else {
        const payload = { ...values }
        // 提交时：仅当选择自定义模式才携带 token 字段
        if (values.genMode !== 'custom') {
          delete payload.token
        }
        delete payload.genMode
        // 无权限用户只能创建私有token
        if (!canEditAllTokens) {
          payload.scope = 'private'
        }
        await addToken(payload)
        messageApi.success('添加成功')
      }
      setIsModalOpen(false)
      getTokens()
    } catch (error) {
      messageApi.error(error?.detail || '操作失败')
    } finally {
      setConfirmLoading(false)
    }
  }

  const handleResetCounter = async () => {
    if (!editingRecord) return
    if (editingRecord.isLocked) {
      messageApi.error('该Token已锁死，无法重置调用次数')
      return
    }
    try {
      await resetTokenCounter({ id: editingRecord.id })
      messageApi.success('调用次数已重置为0')
      setIsModalOpen(false)
      getTokens()
    } catch (error) {
      messageApi.error('重置失败')
    }
  }

  useEffect(() => {
    getTokens()
  }, [])

  // 监听全局事件以在解锁成功后刷新列表（避免在 Modal.confirm 的 onOk 中直接闭包引用本地方法）
  useEffect(() => {
    const onUnlocked = () => { getTokens() }
    window.addEventListener('danmaku:tokenUnlocked', onUnlocked)
    return () => window.removeEventListener('danmaku:tokenUnlocked', onUnlocked)
  }, [])

  const columns = [
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
      width: 100,
    },
    {
      title: '类型',
      key: 'scope',
      width: 70,
      render: (_, record) => (record.ownerUserId ? '私有' : '全局'),
    },
    {
      title: '创建者',
      key: 'owner',
      width: 120,
      render: (_, record) => {
        const creatorName = record.createdByUsername
        const creatorId = record.createdByUserId
        if (creatorName) return creatorName
        if (creatorId) return `用户#${creatorId}`
        // 回退：若无创建者信息，尝试显示拥有者
        if (record.ownerUsername) return record.ownerUsername
        if (record.ownerUserId) return `用户#${record.ownerUserId}`
        return '—'
      },
    },
    {
      title: 'Token',
      dataIndex: 'token',
      key: 'token',
      width: 160,
      render: (_, record) => {
        return (
          <Input.Password
            value={record.token}
            readOnly
            iconRender={visible =>
              visible ? <EyeTwoTone /> : <EyeInvisibleOutlined />
            }
            style={{ maxWidth: 160 }}
          />
        )
      },
    },
    {
      title: '状态',
      width: 140,
      dataIndex: 'isEnabled',
      key: 'isEnabled',
      render: (_, record) => {
        if (!record.isEnabled) {
          return <Tag color="red">禁用</Tag>
        }

        const isInfinite = record.dailyCallLimit === -1
        const percent = isInfinite
          ? 0
          : Math.round(
              (record.dailyCallCount / record.dailyCallLimit) * 100
            )
        const limitText = isInfinite ? '∞' : record.dailyCallLimit

        return (
          <Space size="small" align="center">
            <Progress
              percent={percent}
              size="small"
              showInfo={false}
              status={isInfinite ? 'normal' : 'normal'}
              strokeColor={isInfinite ? '#1677ff' : undefined}
              className="!w-[60px]"
            />
            <span style={{ minWidth: '50px', display: 'inline-block' }}>
              {record.dailyCallCount} / {limitText}
            </span>
          </Space>
        )
      },
    },
    {
      title: '创建时间',
      dataIndex: 'createdAt',
      key: 'createdAt',
      width: 140,
      onCell: () => ({ style: { paddingLeft: 8, paddingRight: 8 } }),
      render: (_, record) => (
        <div>{dayjs(record.createdAt).format('YYYY-MM-DD HH:mm:ss')}</div>
      ),
    },
    {
      title: '有效期',
      dataIndex: 'expiresAt',
      key: 'expiresAt',
      width: 140,
      onCell: () => ({ style: { paddingLeft: 8, paddingRight: 8 } }),
      render: (_, record) => (
        <div>{record.expiresAt ? dayjs(record.expiresAt).format('YY/MM/DD HH:mm') : '永久'}</div>
      ),
    },
    {
      title: '操作',
      width: 220,
      fixed: 'right',
      render: (_, record) => {
        return (
          <Space>
            <Tooltip title={record.isLocked ? '已锁死' : '编辑'}>
              <span
                className="cursor-pointer hover:text-primary"
                style={{ color: record.isLocked ? '#aaa' : undefined }}
                onClick={() => !record.isLocked && handleOpenModal(true, record)}
              >
                <MyIcon icon="edit" size={20}></MyIcon>
              </span>
            </Tooltip>
            <Tooltip title="复制">
              <span
                className="cursor-pointer hover:text-primary"
                onClick={() => {
                  copy(
                    `${domain || window.location.origin}/api/v1/${record.token}`
                  )
                  messageApi.success('复制成功')
                }}
              >
                <MyIcon icon="copy" size={20}></MyIcon>
              </span>
            </Tooltip>
            <Tooltip title={record.isLocked && !canEditAllTokens ? '已锁死' : 'Token访问日志'}>
              <span
                className="cursor-pointer hover:text-primary"
                style={{ color: record.isLocked && !canEditAllTokens ? '#aaa' : undefined }}
                onClick={() => handleTokenLogs(record)}
              >
                <MyIcon icon="rizhi" size={20}></MyIcon>
              </span>
            </Tooltip>
            <Tooltip title={record.isLocked ? '已锁死' : '切换启用状态'}>
              <span
                className="cursor-pointer hover:text-primary"
                style={{ color: record.isLocked ? '#aaa' : undefined }}
                onClick={() => {
                  if (!record.isLocked) handleToggleStatus(record)
                }}
              >
                <div>
                  {record.isEnabled ? (
                    <MyIcon icon="pause" size={20}></MyIcon>
                  ) : (
                    <MyIcon icon="start" size={20}></MyIcon>
                  )}
                </div>
              </span>
            </Tooltip>
            <Tooltip title={record.isLocked ? '已锁死' : '删除Token'}>
              <span
                className="cursor-pointer hover:text-primary"
                style={{ color: record.isLocked ? '#aaa' : undefined }}
                onClick={() => !record.isLocked && handleDelete(record)}
              >
                <MyIcon icon="delete" size={20}></MyIcon>
              </span>
            </Tooltip>
            {!record.isLocked && (
              <Tooltip title="锁死Token">
                <span
                  className="cursor-pointer hover:text-primary"
                  onClick={() => {
                    modalApi.confirm({
                      title: '锁死Token',
                      zIndex: 1002,
                      content: (
                        <div>
                          锁死后你将不能调整该 Token 的任何参数，且无法查看该 Token 的访问日志。<br />
                          拥有“编辑 弹幕token”权限的管理员可解除锁定，并查看日志、调整参数。<br />
                          是否确认锁死此 Token？
                        </div>
                      ),
                      okText: '确认锁死',
                      okButtonProps: { danger: true },
                      cancelText: '取消',
                      onOk: async () => {
                        try {
                          await lockToken({ tokenId: record.id })
                          messageApi.success('已锁死该Token')
                          getTokens()
                        } catch (e) {
                          messageApi.error('锁死失败')
                        }
                      },
                    })
                  }}
                >
                  <LockOutlined style={{ fontSize: 18 }} />
                </span>
              </Tooltip>
            )}
            {record.isLocked && canEditAllTokens && (
              <Tooltip title="解除锁定">
                <span
                  className="cursor-pointer hover:text-primary"
                  onClick={() => handleUnlock(record)}
                >
                  <UnlockOutlined style={{ fontSize: 18 }} />
                </span>
              </Tooltip>
            )}
          </Space>
        )
      },
    },
  ]

  const logsColumns = [
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
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 200,
    },
    {
      title: '路径',
      width: 250,
      dataIndex: 'path',
      key: 'path',
    },
    {
      title: 'User-Agent',
      dataIndex: 'userAgent',
      key: 'userAgent',
      width: 400,
    },
  ]

  return (
    <div className="my-6">
      <Card
        loading={loading}
        title="弹幕Token管理"
        extra={
          <>
            <Button type="primary" onClick={() => handleOpenModal(false)}>
              添加Token
            </Button>
          </>
        }
      >
        <Table
          pagination={false}
          size="small"
          dataSource={tokenList}
          columns={columns}
          rowKey={'id'}
          tableLayout="fixed"
        />
      </Card>
      <Modal
        title={isEditing ? '编辑Token' : '添加新Token'}
        open={isModalOpen}
        onOk={handleSave}
        confirmLoading={confirmLoading}
        cancelText="取消"
        okText="确认"
        onCancel={() => setIsModalOpen(false)}
        footer={
          <div className="flex justify-between">
            <div>
              {isEditing && (
                <Button danger onClick={handleResetCounter}>
                  重置调用次数
                </Button>
              )}
            </div>
            <div>
              <Button onClick={() => setIsModalOpen(false)}>取消</Button>
              <Button
                type="primary"
                onClick={handleSave}
                loading={confirmLoading}
              >
                确认
              </Button>
            </div>
          </div>
        }
      >
        <Form form={form} layout="vertical">
          {canEditAllTokens && (
            <Form.Item
              name="scope"
              label="类型"
              tooltip="全局：所有用户可见；私有：仅创建者与有权限用户可见"
              className="mb-2"
            >
              <Select
                options={[
                  { value: 'private', label: '私有' },
                  { value: 'global', label: '全局' },
                ]}
              />
            </Form.Item>
          )}
          <Form.Item
            name="name"
            label="名称"
            rules={[{ required: true, message: '请输入名称' }]}
            className="mb-4"
          >
            <Input placeholder="例如：我的dandanplay客户端" />
          </Form.Item>
          {!isEditing && (
            <>
              <Form.Item name="genMode" label="生成方式" className="mb-2" initialValue={'random'}>
                <Select
                  options={[
                    { value: 'random', label: '随机生成' },
                    { value: 'custom', label: '自定义' },
                  ]}
                />
              </Form.Item>
              <Form.Item noStyle shouldUpdate={(prev, cur) => prev.genMode !== cur.genMode}>
                {() => (
                  form.getFieldValue('genMode') === 'custom' ? (
                    <Form.Item
                      name="token"
                      label="自定义Token"
                      rules={[{ required: true, message: '请输入自定义Token' }]}
                      className="mb-4"
                    >
                      <Input placeholder="8-64位，仅字母/数字/下划线/中横线" />
                    </Form.Item>
                  ) : null
                )}
              </Form.Item>
            </>
          )}
          <Form.Item
            name="validityPeriod"
            label="有效期"
            rules={[{ required: true, message: '请选择有效期' }]}
            className="mb-4"
          >
            <Select
              options={[
                isEditing && { value: 'custom', label: '不改变当前有效期' },
                { value: 'permanent', label: '永久' },
                { value: '1d', label: '1 天' },
                { value: '7d', label: '7 天' },
                { value: '30d', label: '30 天' },
                { value: '180d', label: '6 个月' },
                { value: '365d', label: '1 年' },
              ].filter(Boolean)}
            />
          </Form.Item>
          <Form.Item
            name="dailyCallLimit"
            label="每日调用上限"
            tooltip="设置此Token每日可调用的总次数。-1 代表无限次。"
            className="mb-4"
          >
            <InputNumber
              min={-1}
              style={{ width: '100%' }}
              placeholder="默认为500, -1为无限"
            />
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        title="Token访问日志"
        open={logsOpen}
        cancelText="取消"
        okText="确认"
        onCancel={() => setLogsOpen(false)}
        onOk={() => setLogsOpen(false)}
      >
        <Table
          pagination={false}
          size="small"
          dataSource={tokenLogs}
          columns={logsColumns}
          rowKey={'accessTime'}
          scroll={{
            x: '100%',
            y: 400,
          }}
        />
      </Modal>
    </div>
  )
}
  const handleUnlock = record => {
    // 使用 antd 全局确认弹窗；onOk 返回 Promise，完成后自动关闭，再显示刷新后的列表
    Modal.confirm({
      title: '解除锁定',
      zIndex: 1002,
      content: <div>确认解除对 {record.name} 的锁定？</div>,
      okText: '确认',
      cancelText: '取消',
      onOk: async () => {
        let ok = false
        let errDetail = ''
        try {
          await unlockToken({ tokenId: record.id })
          ok = true
        } catch (e) {
          errDetail = e?.detail || ''
          // 回退校验：极端网络/拦截器异常下，直接读取列表确认状态
          try {
            const res = await getTokenList()
            const updated = Array.isArray(res?.data) ? res.data.find(it => it.id === record.id) : null
            if (updated && updated.isLocked === false) {
              ok = true
            }
          } catch (_) {}
        }
        if (ok) {
          message.success('解除成功')
          // 通过全局事件通知列表刷新，避免在 onOk 闭包中直接引用组件内部方法
          try { window.dispatchEvent(new CustomEvent('danmaku:tokenUnlocked', { detail: { id: record.id } })) } catch(_) {}
        } else {
          message.error(errDetail || '解除锁定失败')
        }
      },
    })
  }
