import { Button, Card, Form, Input, message, Alert } from 'antd'
import { useEffect, useState } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import { registerByInvite, validateInvite } from '../../apis'

export const Register = () => {
  const [form] = Form.useForm()
  const [search] = useSearchParams()
  const navigate = useNavigate()
  const code = search.get('code') || ''
  const [invalid, setInvalid] = useState({ valid: true, message: '' })
  useEffect(()=>{ form.setFieldsValue({ code }) }, [code])
  useEffect(() => {
    const run = async () => {
      if (!code) return
      try {
        const res = await validateInvite({ code })
        const d = res?.data
        if (!d?.valid) {
          setInvalid({ valid: false, message: d?.message || '邀请码无效' })
          message.error(d?.message || '邀请码无效')
        } else {
          setInvalid({ valid: true, message: '' })
        }
      } catch (e) {
        // 后端异常也视为无效
        setInvalid({ valid: false, message: '邀请码无效' })
      }
    }
    run()
  }, [code])

  const onSubmit = async () => {
    try {
      if (!invalid.valid) { message.error(invalid.message || '邀请码无效'); return }
      const v = await form.validateFields()
      await registerByInvite({ code: v.code, username: v.username, password: v.password })
      message.success('注册成功，请登录')
      navigate('/login', { replace: true })
    } catch (e) {
      if (e?.detail) message.error(e.detail)
    }
  }

  return (
    <div className="w-full flex justify-center items-center" style={{ minHeight: '60vh' }}>
      <Card
        title={<div style={{ textAlign: 'center', fontSize: 48, fontWeight: 700, paddingTop: 16, paddingBottom: 9.6 }}>受邀注册</div>}
        style={{ width: 420 }}
      >
        {!invalid.valid && (
          <Alert type="error" message={invalid.message || '邀请码无效'} showIcon className="mb-4" />
        )}
        <Form form={form} layout="vertical">
          <Form.Item label="邀请码" name="code" rules={[{ required: true, message: '缺少邀请码' }]}>
            <Input placeholder="请输入邀请码" />
          </Form.Item>
          <Form.Item label="用户名" name="username" rules={[{ required: true, message: '请输入用户名' }]}>
            <Input placeholder="用户名" disabled={!invalid.valid} />
          </Form.Item>
          <Form.Item label="密码" name="password" rules={[{ required: true, message: '请输入密码' }, { min: 8, message: '至少8位' }]}>
            <Input.Password placeholder="至少8位" disabled={!invalid.valid} />
          </Form.Item>
          <div className="flex justify-end">
            <Button type="primary" onClick={onSubmit} disabled={!invalid.valid}>注册</Button>
          </div>
        </Form>
      </Card>
    </div>
  )
}
