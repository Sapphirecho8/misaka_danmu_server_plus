import { Button, Card, Form, Input } from 'antd'
import { useEffect, useState } from 'react'
import { getTrustedProxiesConfig, setTrustedProxiesConfig } from '../../../apis'
import { useMessage } from '../../../MessageContext'

export const TrustedProxies = () => {
  const [loading, setLoading] = useState(true)
  const [form] = Form.useForm()
  const [isSaveLoading, setIsSaveLoading] = useState(false)
  const messageApi = useMessage()

  useEffect(() => {
    setLoading(true)
    getTrustedProxiesConfig()
      .then(res => {
        form.setFieldsValue({ trustedProxies: res.data?.value ?? '' })
      })
      .finally(() => {
        setLoading(false)
      })
  }, [form])

  const handleSave = async () => {
    try {
      setIsSaveLoading(true)
      const values = await form.validateFields()
      await setTrustedProxiesConfig({
        value: values.trustedProxies || '',
      })
      setIsSaveLoading(false)
      messageApi.success('保存成功')
    } catch (error) {
      messageApi.error('保存失败')
    } finally {
      setIsSaveLoading(false)
    }
  }

  return (
    <div className="my-6">
      <Card loading={loading} title="受信任的反向代理">
        <div className="mb-4">当请求来自这些IP时，将从 X-Forwarded-For 或 X-Real-IP 头中解析真实客户端IP。多个IP或CIDR网段请用英文逗号(,)分隔。</div>
        <Form
          form={form}
          layout="horizontal"
          onFinish={handleSave}
          className="px-6 pb-6"
        >
          <Form.Item name="trustedProxies" label="IP或CIDR列表" className="mb-6">
            <Input.TextArea rows={4} placeholder="例如: 127.0.0.1, 192.168.1.0/24, 10.0.0.1/32" />
          </Form.Item>

          <Form.Item>
            <div className="flex justify-end">
              <Button type="primary" htmlType="submit" loading={isSaveLoading}>
                保存修改
              </Button>
            </div>
          </Form.Item>
        </Form>
      </Card>
    </div>
  )
}