import { useEffect, useState } from 'react'
import { Button, Card, Form, Input, InputNumber, message, Modal, Table, Space, Popconfirm } from 'antd'
import { listUsers, createUser, updateUser, getUserInfo, deleteUser } from '../../../apis'
import { useMessage } from '../../../MessageContext'
import PermissionEditor from '../../components/PermissionEditor'
import { PERMISSIONS, defaultBoolOf } from '../../constants/permissions'

export const Accounts = () => {
  const [loading, setLoading] = useState(false)
  const [users, setUsers] = useState([])
  const [currentUser, setCurrentUser] = useState(null)
  const [permModalOpen, setPermModalOpen] = useState(false)
  const [editingUser, setEditingUser] = useState(null)
  const [permForm] = Form.useForm()
  const [form] = Form.useForm()
  const messageApi = useMessage()
  const [remarkModalOpen, setRemarkModalOpen] = useState(false)
  const [remarkForm] = Form.useForm()

  const fetchUsers = async () => {
    try {
      setLoading(true)
      const res = await listUsers()
      setUsers(res.data)
    } catch (e) {
      messageApi.error('获取用户列表失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchUsers()
    // __init_permStates__
    form.setFieldsValue({ permStates: initStatesForCreate() })
    getUserInfo().then(res => setCurrentUser(res?.data)).catch(()=>setCurrentUser(null))
  }, [])

  const canEditTarget = (row) => {
    if (!currentUser) return false
    if (row?.id === currentUser.id) return false
    const isSuperAdmin = (currentUser.username || '').toLowerCase() === 'admin'
    if ((row?.username || '').toLowerCase() === 'admin') return false
    if (isSuperAdmin) return true
    const perms = currentUser.permissions || {}
    return perms.editUsers === true
  }

  

  const initStatesForCreate = () => {
    const out = {}
    ;(PERMISSIONS || []).forEach(p => {
      out[p.key] = defaultBoolOf(p.key) ? 'allow' : 'deny'
    })
    return out
  }
const initStatesFromPerms = (permsObj) => {
    const out = {}
    (PERMISSIONS || []).forEach(p => {
      const v = (permsObj || {})[p.key]
      if (v === true) out[p.key] = 'allow'
      else if (v === false) out[p.key] = 'deny'
      else out[p.key] = (defaultBoolOf(p.key)?'allow':'deny')
    })
    return out
  }

  const openPermModal = (row) => {
    setEditingUser(row)
    setPermModalOpen(true)
    try {
      const p = row.permissions || {}
      permForm.setFieldsValue({
        permStates: initStatesFromPerms(p),
      })
    } catch (e) {
      messageApi.warning('初始化权限表单失败，但已打开编辑窗口')
    }
  }

  const computeUpdateDelta = (existing, states) => {
    const delta = {}
    ;(PERMISSIONS || []).forEach(p => {
      const st = (states || {})[p.key] || (defaultBoolOf(p.key)?'allow':'deny')
      const desired = (st === 'allow')
      const exists = (existing || {})
      const had = Object.prototype.hasOwnProperty.call(exists, p.key) ? exists[p.key] : undefined
      if (had === undefined) {
        // 不存在：仅当偏离默认或需要显式覆盖时发送
        const def = defaultBoolOf(p.key)
        if (desired !== def) delta[p.key] = desired
      } else if (had !== desired) {
        // 已存在且不同：发送覆盖
        delta[p.key] = desired
      }
    })
    return delta
  }

  const savePerms = async () => {
    try {
      const v = await permForm.validateFields()
      const states = v.permStates || {}
      const delta = computeUpdateDelta(editingUser?.permissions || {}, states)
      await updateUser(editingUser.id, { permissions: delta })
      messageApi.success('权限已更新')
      setPermModalOpen(false)
      setEditingUser(null)
      fetchUsers()
    } catch (e) {
      const detail = e?.detail || ''
      messageApi.error(`更新失败${detail?': '+detail:''}`)
    }
  }

  const columns = [
    { title: 'ID', dataIndex: 'id', width: 80 },
    { title: '用户名', dataIndex: 'username', width: 180 },
    { title: '每小时限额', dataIndex: 'perHourLimit', width: 160 },
    { title: '备注', dataIndex: 'remark', width: 240, render: v => (v || '-') },
    {
      title: '操作',
      key: 'action',
      width: 200,
      render: (_, row) => {
        const onClick = () => {
          const can = canEditTarget(row)
          if (!currentUser) return messageApi.info('用户信息加载中，请稍候再试')
          if (!can) {
            const isSelf = row?.id === currentUser?.id
            const isTargetAdmin = ((row?.username || '').toLowerCase() === 'admin')
            if (isSelf) return messageApi.warning('不能编辑自己的权限')
            if (isTargetAdmin) return messageApi.warning('不能编辑超级管理员的权限')
            return messageApi.warning('您没有编辑用户权限（需要 editUsers）')
          }
          openPermModal(row)
        }
        const onEditRemark = () => {
          if (!currentUser) return messageApi.info('用户信息加载中，请稍候再试')
          if (!canEditTarget(row)) return messageApi.warning('您没有权限编辑备注')
          setEditingUser(row)
          remarkForm.setFieldsValue({ remark: row.remark || '' })
          setRemarkModalOpen(true)
        }
        const onDelete = async () => {
          if (!currentUser) return
          if ((row?.username || '').toLowerCase() === 'admin') return messageApi.warning('不能删除超级管理员')
          if (row?.id === currentUser?.id) return messageApi.warning('不能删除自己')
          try {
            await deleteUser(row.id)
            message.success('删除成功')
            fetchUsers()
          } catch (e) {
            message.error(e?.detail || '删除失败')
          }
        }
        return (
          <Space size={8}>
            <Button size='small' type='link' onClick={onClick}>编辑权限</Button>
            <Button size='small' type='link' onClick={onEditRemark}>编辑备注</Button>
            <Popconfirm title='确认删除该用户？' onConfirm={onDelete} okText='删除' cancelText='取消'>
              <Button size='small' danger type='link'>删除</Button>
            </Popconfirm>
          </Space>
        )
      }
    }
  ]

  const buildPermsForCreate = (states) => {
    const out = {}
    ;(PERMISSIONS || []).forEach(p => {
      const st = (states || {})[p.key] || (defaultBoolOf(p.key)?'allow':'deny')
      const def = defaultBoolOf(p.key)
      if (st === 'allow') { if (def !== true) out[p.key] = true }
      else if (st === 'deny') { if (def !== false) out[p.key] = false }
      // inherit 不写入，后端使用默认
    })
    return out
  }

  const onCreate = async () => {
    try {
      const v = await form.validateFields()
      const states = v.permStates || {}
      const permsObj = buildPermsForCreate(states)
      const payload = {
        username: v.username,
        password: v.password,
        role: 'user',
        perHourLimit: v.perHourLimit ?? null,
        canCreateAdmin: false,
        permissions: permsObj,
        remark: v.remark || '',
      }
      await createUser(payload)
      messageApi.success('创建成功')
      form.resetFields()
      form.setFieldsValue({ permStates: initStatesForCreate() })
      fetchUsers()
    } catch (e) {
      messageApi.error('创建失败')
    }
  }

  return (
    <div className='my-6'>
      <Card loading={loading} title='账户管理'>
        <div className='mb-4'>
          <Table pagination={false} size='small' dataSource={users} columns={columns} rowKey='id' />
        </div>
        <Modal
          title={`编辑权限 - ${editingUser?.username || ''}`}
          open={permModalOpen}
          onCancel={() => { setPermModalOpen(false); setEditingUser(null) }}
          onOk={savePerms}
          okText='保存'
          width={900}
        >
          <Form form={permForm} layout='vertical'>
            <Form.Item name='permStates' label=''>
              <PermissionEditor />
            </Form.Item>
          </Form>
        </Modal>
        <Modal
          title={`编辑备注 - ${editingUser?.username || ''}`}
          open={remarkModalOpen}
          onCancel={() => { setRemarkModalOpen(false); setEditingUser(null) }}
          onOk={async () => {
            const v = await remarkForm.validateFields()
            try {
              await updateUser(editingUser.id, { remark: v.remark || '' })
              messageApi.success('备注已更新')
              setRemarkModalOpen(false)
              setEditingUser(null)
              fetchUsers()
            } catch (e) {
              messageApi.error(e?.detail || '更新失败')
            }
          }}
          okText='保存'
        >
          <Form form={remarkForm} layout='vertical'>
            <Form.Item name='remark' label='备注'>
              <Input.TextArea rows={3} placeholder='请输入用户备注（仅账户管理可见）' />
            </Form.Item>
          </Form>
        </Modal>
        <Card title='新增用户' size='small'>
          <Form form={form} layout='vertical'>
            <Form.Item name='username' label='用户名' rules={[{ required: true, message: '请输入用户名'}]}>
              <Input placeholder='用户名' />
            </Form.Item>
            <Form.Item name='password' label='初始密码' rules={[{ required: true, message: '请输入初始密码'}]}>
              <Input.Password placeholder='初始密码' />
            </Form.Item>
            <Form.Item name='perHourLimit' label='每小时限额（留空为不限制）'>
              <InputNumber min={-1} style={{width:'100%'}} />
            </Form.Item>
            <Form.Item name='remark' label='备注（可选）'>
              <Input.TextArea rows={2} placeholder='仅拥有新增用户权限时可见' />
            </Form.Item>
            <Form.Item name='permStates' label=''>
              <PermissionEditor />
            </Form.Item>
            <Button type='primary' onClick={onCreate}>创建</Button>
          </Form>
        </Card>
      </Card>
    </div>
  )
}
