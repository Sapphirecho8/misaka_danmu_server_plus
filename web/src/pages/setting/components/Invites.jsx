import { useEffect, useMemo, useState } from 'react'
import { Button, Card, Form, Input, InputNumber, Modal, Space, Table, Tooltip, message, Select, DatePicker, Checkbox } from 'antd'
import { listInvites, createInvite, deleteInviteApi } from '../../../apis'
import copy from 'copy-to-clipboard'
import dayjs from 'dayjs'
import PermissionEditor from '@/components/PermissionEditor.jsx'
import { useAtomValue } from 'jotai'
import { userinfoAtom } from '../../../../store/index.js'

export const Invites = () => {
  const [loading, setLoading] = useState(false)
  const [list, setList] = useState([])
  const [open, setOpen] = useState(false)
  const [confirmLoading, setConfirmLoading] = useState(false)
  const [form] = Form.useForm()
  const userinfo = useAtomValue(userinfoAtom)
  const [filterType, setFilterType] = useState('all') // all|used|unused

  const refresh = async () => {
    try {
      setLoading(true)
      const res = await listInvites()
      setList(res.data || [])
    } catch (e) {
      // No permission will be disabled by Settings tab
    } finally { setLoading(false) }
  }

  useEffect(() => { refresh() }, [])

  const filteredList = useMemo(() => {
    const arr = list || []
    if (filterType === 'used') {
      // 定义“已使用”：达到上限 或 被禁用 或 已过期
      return arr.filter(i => (i.isExpired === true) || (i.isEnabled === false) || ((i.usedCount || 0) >= (i.maxUses || 0)))
    }
    if (filterType === 'unused') {
      // 定义“未使用”：未达上限 且 启用 且 未过期
      return arr.filter(i => (i.isExpired !== true) && (i.isEnabled !== false) && ((i.usedCount || 0) < (i.maxUses || 0)))
    }
    return arr
  }, [list, filterType])

  const handleCreate = async () => {
    try {
      const values = await form.validateFields()
      setConfirmLoading(true)
      const payload = {
        maxUses: values.maxUses,
        perHourLimit: values.perHourLimit ?? null,
        remark: values.remark || null,
        permissions: values.permissions || null,
        expiresAt: values.permanent ? null : (values.expiresAt ? dayjs(values.expiresAt).format('YYYY-MM-DDTHH:mm:ss') : null),
      }
      const res = await createInvite(payload)
      setOpen(false)
      message.success('邀请创建成功')
      refresh()
    } catch (e) {
      console.error('createInvite error:', e)
      message.error(e?.detail || '创建失败')
    } finally { setConfirmLoading(false) }
  }

  const columns = [
    { title: '邀请码', dataIndex: 'code', key: 'code', width: 240, onCell: () => ({ style: { paddingRight: 32 } }), render: (v, r) => (
      <Space>
        <Button size="small" onClick={()=>{
          const url = `${window.location.origin}/register?code=${v}`
          copy(url)
          message.success('注册链接已复制')
        }}>复制链接</Button>
        <span>{v}</span>
      </Space>
    ) },
    { title: '创建时间', dataIndex: 'createdAt', key: 'createdAt', width: 180, render: (_, r)=> dayjs(r.createdAt).format('YYYY-MM-DD HH:mm:ss') },
    { title: '过期时间', dataIndex: 'expiresAt', key: 'expiresAt', width: 180, render: (_, r)=> r.expiresAt ? dayjs(r.expiresAt).format('YYYY-MM-DD HH:mm:ss') : '永久' },
    { title: '已用/上限', key: 'usage', width: 120, render: (_, r)=> `${r.usedCount} / ${r.maxUses}` },
    { title: '限额(每小时)', dataIndex: 'perHourLimit', key: 'perHourLimit', width: 120, render: v => (v ?? '—') },
    { title: '备注', dataIndex: 'remark', key: 'remark', ellipsis: true },
    { title: '操作', key: 'op', width: 120, render: (_, r)=> (
      <Space>
        <Button size="small" danger onClick={async()=>{
          Modal.confirm({
            title:'删除邀请',
            content:`确定删除邀请码 ${r.code} 吗？`,
            onOk: async()=>{
              try{ await deleteInviteApi(r.id); message.success('已删除'); refresh() }catch(e){ console.error('deleteInvite error:', e); message.error(e?.detail || '删除失败') }
            }
          })
        }}>删除</Button>
      </Space>
    ) }
  ]

  return (
    <div>
      <Card
        title="邀请注册管理"
        extra={
          <Space>
            <Select
              size="small"
              value={filterType}
              style={{ width: 120 }}
              onChange={setFilterType}
              options={[
                { value: 'all', label: '全部' },
                { value: 'used', label: '已使用' },
                { value: 'unused', label: '未使用' },
              ]}
            />
            <Button type="primary" onClick={()=>{form.resetFields(); form.setFieldsValue({maxUses:1, permanent:true, expiresAt:null}); setOpen(true)}}>新建邀请</Button>
          </Space>
        }
      >
        <Table
          loading={loading}
          dataSource={filteredList}
          columns={columns}
          rowKey="id"
          pagination={{ pageSize: 10 }}
        />
      </Card>

      <Modal
        title="新建邀请"
        open={open}
        onCancel={()=>setOpen(false)}
        onOk={handleCreate}
        confirmLoading={confirmLoading}
      >
        <Form form={form} layout="vertical">
          <Form.Item label="可注册用户数量" name="maxUses" rules={[{required:true, message:'请输入数量'}]}>
            <InputNumber min={1} max={1000} style={{width:'100%'}} placeholder="例如：1" />
          </Form.Item>
          <Form.Item label="新用户每小时限额" name="perHourLimit">
            <InputNumber min={-1} style={{width:'100%'}} placeholder="留空则不限制" />
          </Form.Item>
          <Form.Item label="注册用户备注" name="remark">
            <Input />
          </Form.Item>
          <Form.Item label="过期时间" shouldUpdate={(prev, cur) => prev.permanent !== cur.permanent}>
            {() => (
              <Space>
                <Form.Item name="permanent" valuePropName="checked" noStyle initialValue={true}>
                  <Checkbox onChange={e => { if (e.target.checked) form.setFieldsValue({ expiresAt: null }) }}>永久有效</Checkbox>
                </Form.Item>
                <Form.Item name="expiresAt" noStyle>
                  <DatePicker
                    showTime
                    format="YYYY-MM-DD HH:mm:ss"
                    disabled={form.getFieldValue('permanent') !== false}
                    allowClear
                    style={{ pointerEvents: (form.getFieldValue('permanent') !== false) ? 'none' : 'auto' }}
                  />
                </Form.Item>
              </Space>
            )}
          </Form.Item>
          <Form.Item label="注册用户权限" shouldUpdate>
            <Form.Item name="permissions" noStyle>
              <PermissionEditor value={form.getFieldValue('permissions')||{}} onChange={v=>form.setFieldsValue({permissions:v})} />
            </Form.Item>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
