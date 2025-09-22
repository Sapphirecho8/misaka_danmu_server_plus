import {
  EyeInvisibleOutlined,
  EyeOutlined,
  LockOutlined,
} from '@ant-design/icons'
import { Button, Card, Form, Input } from 'antd'
import { useEffect, useState } from 'react'
import { changePassword, getUserInfo } from '../../../apis'
import { useMessage } from '../../../MessageContext'
import { TrustedProxies } from './TrustedProxies'

export const Security = () => {
  const [form] = Form.useForm()
  const [showPassword1, setShowPassword1] = useState(false)
  const [showPassword2, setShowPassword2] = useState(false)
  const [showPassword3, setShowPassword3] = useState(false)
  const [isLoading, setIsLoading] = useState(false)
  const messageApi = useMessage()
  const [currentUser, setCurrentUser] = useState()

  // 权限：是否允许当前用户修改自己的密码（默认允许；当权限显式为 false 时禁止）

  useEffect(() => {
    getUserInfo()
      .then(res => setCurrentUser(res?.data))
      .catch(() => setCurrentUser(undefined))
  }, [])

  const getErrorMessage = err => {
    const d = err?.detail || err?.message || ''
    if (!d && typeof err === 'string') return err
    if (typeof d === 'string') {
      if (d.includes('Missing oldPassword')) return '请输入当前密码与新密码'
      if (d.includes('New password must be at least')) return '新密码长度不足（至少8位）'
      if (d.includes('Incorrect old password')) return '当前密码不正确'
      if (d.includes('disabled by admin')) return '已被管理员禁止修改密码'
      return d
    }
    return '修改失败'
  }

  const onSave = async () => {
    try {
      setIsLoading(true)
      const values = await form.validateFields()
      await changePassword(values)
      form.resetFields()
      messageApi.success('修改成功')
    } catch (error) {
      messageApi.error(getErrorMessage(error))
    } finally {
      setIsLoading(false)
    }
  }

  const canChange = (currentUser?.permissions?.changePasswordSelf !== false)
  const canEditTrustedProxies = ((currentUser?.username || '').toLowerCase() === 'admin') || !!currentUser?.permissions?.editTrustedProxies
  if (!canChange) {
    return (
      <div className="my-6">
        <Card title="修改密码">
          <div className="text-gray-600 text-sm px-6 pb-4">您的账户已被管理员禁止修改密码。</div>
        </Card>
        {canEditTrustedProxies && <TrustedProxies />}
      </div>
    )
  }

  return (
    <div className="my-6">
      <Card title="修改密码">
        <div className="mb-4">
          如果您是使用初始随机密码登录的，建议您在此修改为自己的密码。
        </div>
        <Form
          form={form}
          layout="horizontal"
          onFinish={onSave}
          className="px-6 pb-6"
        >
          {/* 密码输入 */}
          <Form.Item
            name="oldPassword"
            label="当前密码"
            rules={[{ required: true, message: '请输入当前密码' }]}
          >
            <Input.Password
              prefix={<LockOutlined className="text-gray-400" />}
              placeholder="请输入当前密码"
              visibilityToggle={{
                visible: showPassword1,
                onVisibleChange: setShowPassword1,
              }}
              iconRender={visible =>
                visible ? <EyeOutlined /> : <EyeInvisibleOutlined />
              }
            />
          </Form.Item>
          <Form.Item
            name="newPassword"
            label="新密码"
            rules={[
              { required: true, message: '请输入新密码' },
              { min: 8, message: '新密码长度不足（至少8位）' },
            ]}
          >
            <Input.Password
              prefix={<LockOutlined className="text-gray-400" />}
              placeholder="请输入新密码"
              visibilityToggle={{
                visible: showPassword2,
                onVisibleChange: setShowPassword2,
              }}
              iconRender={visible =>
                visible ? <EyeOutlined /> : <EyeInvisibleOutlined />
              }
            />
          </Form.Item>
          <Form.Item
            name="checkPassword"
            label="确认新密码"
            dependencies={['newPassword']}
            rules={[
              {
                required: true,
                message: '请输入新密码',
              },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value || getFieldValue('newPassword') === value) {
                    return Promise.resolve()
                  }
                  return Promise.reject(new Error('新密码不匹配'))
                },
              }),
            ]}
          >
            <Input.Password
              prefix={<LockOutlined className="text-gray-400" />}
              placeholder="请输入新密码"
              visibilityToggle={{
                visible: showPassword3,
                onVisibleChange: setShowPassword3,
              }}
              iconRender={visible =>
                visible ? <EyeOutlined /> : <EyeInvisibleOutlined />
              }
            />
          </Form.Item>

          <Form.Item>
            <div className="flex justify-end">
              <Button type="primary" htmlType="submit" loading={isLoading}>
                确认修改
              </Button>
            </div>
          </Form.Item>
        </Form>
      </Card>
      {canEditTrustedProxies && <TrustedProxies />}
    </div>
  )
}
