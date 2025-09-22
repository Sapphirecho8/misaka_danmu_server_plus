import { useEffect, useState } from 'react'
import { Button, Card, Form, Input, InputNumber, Select, Table, Tag, Switch, Checkbox, message, Modal, Popconfirm, Space } from 'antd'
import { listUsers, createUser, updateUser, getUserInfo } from '../../../apis'
import { useMessage } from '../../../MessageContext'

const defaultPerms = {
  'view.home': true,
  'view.library': true,
  'view.tasks.running': true,
  'view.tasks.ratepanel': true,
  'view.bullet.tokens': true,
  'view.settings.security': true,
}

export const Accounts = () => {
  const [loading, setLoading] = useState(false)
  const [users, setUsers] = useState([])
  const [currentUser, setCurrentUser] = useState(null)
  const [permModalOpen, setPermModalOpen] = useState(false)
  const [editingUser, setEditingUser] = useState(null)
  const [permForm] = Form.useForm()
  const [passForm] = Form.useForm()
  const [form] = Form.useForm()
  const messageApi = useMessage()
  const [passModalOpen, setPassModalOpen] = useState(false)
  const [passUser, setPassUser] = useState(null)

  const permRows = [
    { key: 'changePasswordSelf', category: '操作', label: '允许修改自己密码', defaultChecked: true },
    { key: 'createUsers', category: '操作', label: '允许新增用户' },
    { key: 'viewGlobalRate', category: '操作', label: '允许查看全局配额使用情况' },
    { key: 'viewHomeStatus', category: '操作', label: '允许查看首页日志/状态' },
    { key: 'editTmdb', category: '操作', label: '允许编辑TMDB API' },
    { key: 'editProxy', category: '操作', label: '允许编辑反向代理设置' },
    { key: 'editTrustedProxies', category: '操作', label: '允许编辑受信任的反向代理' },
    { key: 'editWebhook', category: '操作', label: '允许编辑Webhook设置' },
    { key: 'editBangumi', category: '操作', label: '允许编辑Bangumi配置' },
    { key: 'editDouban', category: '操作', label: '允许编辑豆瓣设置' },
    { key: 'editTvdb', category: '操作', label: '允许编辑TVDB配置' },
    { key: 'editControl', category: '操作', label: '允许编辑外部控制' },
    { key: 'editScrapers', category: '操作', label: '允许编辑搜索源' },
    { key: 'editDanmakuOutput', category: '操作', label: '允许编辑弹幕输出控制' },
    { key: 'editWebhookTasks', category: '操作', label: '允许编辑Webhook任务' },
    { key: 'editScheduledTasks', category: '操作', label: '允许编辑定时任务' },
    { key: 'editCustomDomain', category: '操作', label: '允许编辑自定义域名设置' },
    { key: 'editUaFilter', category: '操作', label: '允许编辑全局 User-Agent 过滤' },
    { key: 'editLibrary', category: '操作', label: '允许编辑弹幕库' },
    { key: 'editUsers', category: '操作', label: '允许编辑用户权限' },
  ]

  const permColumns = [
    { title: '权限项', dataIndex: 'label' },
    { title: '允许', dataIndex: 'key', width: 120, render: (_, record) => (
      <Form.Item name={['permTable', record.key]} valuePropName='checked' initialValue={!!record.defaultChecked} noStyle>
        <Checkbox />
      </Form.Item>
    )},
  ]

  const PermissionsTable = ({ form }) => {
    return (<Table pagination={false} size='small' dataSource={permRows} columns={permColumns} rowKey='key' />)
  }


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
    getUserInfo().then(res => setCurrentUser(res?.data)).catch(()=>setCurrentUser(null))
  }, [])

  const canEditTarget = (row) => {
    if (!currentUser) return false
    // 不能更改自己的权限
    if (row?.id === currentUser.id) return false
    const isSuperAdmin = (currentUser.username || '').toLowerCase() === 'admin'
    // 不允许任何人编辑主管理员（admin）的权限
    if ((row?.username || '').toLowerCase() === 'admin') return false
    if (isSuperAdmin) return true
    const perms = currentUser.permissions || {}
    return perms.editUsers === true
  }

  const canChangePassword = (row) => {
    if (!currentUser) return false
    const isSuperAdmin = (currentUser.username || '').toLowerCase() === 'admin'
    if (isSuperAdmin) return true
    const perms = currentUser.permissions || {}
    // 非主管理员不能改主管理员密码
    if ((row?.username || '').toLowerCase() === 'admin') return false
    return perms.editUsers === true
  }

  const openPassModal = (row) => {
    setPassUser(row)
    passForm.resetFields()
    setPassModalOpen(true)
  }

  const savePassword = async () => {
    try {
      const v = await passForm.validateFields()
      await updateUser(passUser.id, { password: v.password })
      messageApi.success('密码已更新')
      setPassModalOpen(false)
      setPassUser(null)
    } catch (e) {
      messageApi.error(e?.detail || '修改密码失败')
    }
  }

  const openPermModal = (row) => {
    setEditingUser(row)
    const p = row.permissions || {}
    permForm.setFieldsValue({
      changePasswordSelf: p.changePasswordSelf !== false,
      viewGlobalRate: !!p.viewGlobalRate,
      createUsers: !!p.createUsers,
      editControl: !!p.editControl,
      editScrapers: !!p.editScrapers,
      editDanmakuOutput: !!p.editDanmakuOutput,
      editWebhookTasks: !!p.editWebhookTasks,
      editScheduledTasks: !!p.editScheduledTasks,
      editTmdb: !!p.editTmdb,
      editProxy: !!p.editProxy,
      editTrustedProxies: !!p.editTrustedProxies,
      editWebhook: !!p.editWebhook,
      editBangumi: !!p.editBangumi,
      editDouban: !!p.editDouban,
      editTvdb: !!p.editTvdb,
      editUsers: !!p.editUsers,
    })
    setPermModalOpen(true)
  }

  const savePerms = async () => {
    try {
      const v = await permForm.validateFields()
      const merged = { ...(editingUser?.permissions || {}) }
      merged.changePasswordSelf = !!v.changePasswordSelf
      merged.viewGlobalRate = !!v.viewGlobalRate
      merged.createUsers = !!v.createUsers
      merged.editControl = !!v.editControl
      merged.editScrapers = !!v.editScrapers
      merged.editDanmakuOutput = !!v.editDanmakuOutput
      merged.editWebhookTasks = !!v.editWebhookTasks
      merged.editScheduledTasks = !!v.editScheduledTasks
      merged.editTmdb = !!v.editTmdb
      merged.editProxy = !!v.editProxy
      merged.editTrustedProxies = !!v.editTrustedProxies
      merged.editWebhook = !!v.editWebhook
      merged.editBangumi = !!v.editBangumi
      merged.editDouban = !!v.editDouban
      merged.editTvdb = !!v.editTvdb
      merged.editUsers = !!v.editUsers
      await updateUser(editingUser.id, { permissions: merged })
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
    // 角色列已移除：除 username=admin 外其余皆为普通用户
    { title: '每小时限额', dataIndex: 'perHourLimit', width: 160 },
    { title: '备注', dataIndex: 'remark', width: 240, render: v => (v || '-') },
    {
      title: '操作',
      key: 'action',
      width: 260,
      render: (_, row) => {
        const can = canEditTarget(row)
        const canPwd = canChangePassword(row)
        const onEditPerm = () => {
          if (!currentUser) return messageApi.info('用户信息加载中，请稍候再试')
          if (!can) return messageApi.warning('您没有编辑权限')
          openPermModal(row)
        }
        const onEditRemark = () => {
          if (!currentUser) return messageApi.info('用户信息加载中，请稍候再试')
          if (!can) return messageApi.warning('您没有权限编辑备注')
          setEditingUser(row)
          permForm.resetFields()
          // 复用 remark 表单
          // 使用独立 remarkForm 更好，此处简单提示
        }
        const onDelete = async () => {
          if (!can) return messageApi.error('您没有删除权限')
          if ((row?.username || '').toLowerCase() === 'admin') return messageApi.error('不能删除超级管理员')
          try { await deleteUser(row.id); messageApi.success('删除成功'); fetchUsers() } catch (e) { messageApi.error(e?.detail || '删除失败') }
        }
        return (
          <Space size={8}>
            <Button size='small' type='link' onClick={onEditPerm}>编辑权限</Button>
            <Button size='small' type='link' disabled={!canPwd} onClick={() => { if (!canPwd) return messageApi.warning('您没有编辑用户权限'); openPassModal(row) }}>修改密码</Button>
            <Button size='small' type='link' onClick={() => { setEditingUser(row); messageApi.info('请在设置->账户管理中编辑备注'); }}>编辑备注</Button>
            <Popconfirm title='确认删除该用户？' onConfirm={onDelete} okText='删除' cancelText='取消'>
              <Button size='small' danger type='link'>删除</Button>
            </Popconfirm>
          </Space>
        )
      }
    }
  ]

  const onCreate = async () => {
    try {
      const v = await form.validateFields()
      const permMap = v.permTable || {};
      const permsObj = {};
      Object.keys(permMap).forEach(k => { if (permMap[k]) permsObj[k] = true; });
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
        >
          <Form form={permForm} layout='vertical'>
            <Form.Item name='changePasswordSelf' label='允许修改自己密码' valuePropName='checked'>
              <Switch />
            </Form.Item>
            <Form.Item name='viewGlobalRate' label='允许查看全局配额使用情况' valuePropName='checked'>
              <Switch />
            </Form.Item>
            <Form.Item name='createUsers' label='允许新增用户' valuePropName='checked'>
              <Switch />
            </Form.Item>
            <Form.Item name='editControl' label='允许编辑外部控制' valuePropName='checked'>
              <Switch />
            </Form.Item>
            <Form.Item name='editScrapers' label='允许编辑搜索源' valuePropName='checked'>
              <Switch />
            </Form.Item>
            <Form.Item name='editDanmakuOutput' label='允许编辑弹幕输出控制' valuePropName='checked'>
              <Switch />
            </Form.Item>
            <Form.Item name='editWebhookTasks' label='允许编辑Webhook任务' valuePropName='checked'>
              <Switch />
            </Form.Item>
            <Form.Item name='editScheduledTasks' label='允许编辑 定时任务' valuePropName='checked'>
              <Switch />
            </Form.Item>
            <Form.Item name='editTmdb' label='允许编辑 TMDB API' valuePropName='checked'>
              <Switch />
            </Form.Item>
            <Form.Item name='editProxy' label='允许编辑 代理设置' valuePropName='checked'>
              <Switch />
            </Form.Item>
            <Form.Item name='editWebhook' label='允许编辑 Webhook设置' valuePropName='checked'>
              <Switch />
            </Form.Item>
            <Form.Item name='editBangumi' label='允许编辑 Bangumi配置' valuePropName='checked'>
              <Switch />
            </Form.Item>
            <Form.Item name='editDouban' label='允许编辑 豆瓣设置' valuePropName='checked'>
              <Switch />
            </Form.Item>
            <Form.Item name='editTvdb' label='允许编辑 TVDB配置' valuePropName='checked'>
              <Switch />
            </Form.Item>
            <Form.Item name='editUsers' label='允许编辑用户权限' valuePropName='checked'>
              <Switch />
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
            {/* 权限表格（勾选） */}
            <div className='mb-4'>
              <PermissionsTable form={form} />
            </div>

            <div style={{display:'none'}}>
            <Form.Item name='allowChangePasswordSelf' label='允许修改自己密码' valuePropName='checked' initialValue={true}>
              <Switch />
            </Form.Item>
            <Form.Item name='allowCreateUsers' label='允许新增用户' valuePropName='checked' initialValue={false}>
              <Switch />
            </Form.Item>
            <Form.Item name='allowViewGlobalRate' label='允许查看全局配额使用情况' valuePropName='checked' initialValue={false}>
              <Switch />
            </Form.Item>
            <Form.Item name='allowEditTmdb' label='允许编辑 TMDB API' valuePropName='checked' initialValue={false}>
              <Switch />
            </Form.Item>
            <Form.Item name='allowEditProxy' label='允许编辑 代理设置' valuePropName='checked' initialValue={false}>
              <Switch />
            </Form.Item>
            <Form.Item name='allowEditWebhook' label='允许编辑 Webhook设置' valuePropName='checked' initialValue={false}>
              <Switch />
            </Form.Item>
            <Form.Item name='allowEditBangumi' label='允许编辑 Bangumi配置' valuePropName='checked' initialValue={false}>
              <Switch />
            </Form.Item>
            <Form.Item name='allowEditDouban' label='允许编辑 豆瓣设置' valuePropName='checked' initialValue={false}>
              <Switch />
            </Form.Item>
            <Form.Item name='allowEditTvdb' label='允许编辑 TVDB配置' valuePropName='checked' initialValue={false}>
              <Switch />
            </Form.Item>
            <Form.Item name='allowEditControl' label='允许编辑 外部控制' valuePropName='checked' initialValue={false}>
              <Switch />
            </Form.Item>
            <Form.Item name='allowEditScrapers' label='允许编辑 搜索源' valuePropName='checked' initialValue={false}>
              <Switch />
            </Form.Item>
            <Form.Item name='allowEditDanmakuOutput' label='允许编辑 弹幕输出控制' valuePropName='checked' initialValue={false}>
              <Switch />
            </Form.Item>
            <Form.Item name='allowEditWebhookTasks' label='允许编辑Webhook任务' valuePropName='checked' initialValue={false}>
              <Switch />
            </Form.Item>
            <Form.Item name='allowEditScheduledTasks' label='允许编辑 定时任务' valuePropName='checked' initialValue={false}>
              <Switch />
            </Form.Item>
          <Form.Item name='permissions' label='初始可见模块（未勾选则使用默认五项）' style={{display:'none'}}>
            <Select
              mode='multiple'
              options={[
                {value:'view.home', label:'首页'},
                  {value:'view.library', label:'弹幕库'},
                  {value:'view.tasks.running', label:'任务-进行中'},
                  {value:'view.tasks.ratepanel', label:'任务-流控面板'},
                  {value:'view.bullet.tokens', label:'弹幕-Token管理'},
                  {value:'view.settings.security', label:'设置-账户安全'},
                  {value:'view.source', label:'搜索源'},
                  {value:'view.control', label:'外部控制'},
                ]}
              />
            </Form.Item>
            </div>
            <Button type='primary' onClick={onCreate}>创建</Button>
          </Form>
        </Card>
      </Card>
      <Modal
        title={`修改密码 - ${passUser?.username || ''}`}
        open={passModalOpen}
        onCancel={() => { setPassModalOpen(false); setPassUser(null) }}
        onOk={savePassword}
        okText='保存'
      >
        <Form form={passForm} layout='vertical'>
          <Form.Item name='password' label='新密码' rules={[{ required: true, message: '请输入新密码' }, { min: 8, message: '至少8位' }]}>
            <Input.Password placeholder='至少8位' />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
