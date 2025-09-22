import { Card, Form, Switch } from 'antd'
import { useEffect, useState } from 'react'
import { getMatchFallback, setMatchFallback } from '../../../apis'
import { useMessage } from '../../../MessageContext'

export const MatchFallbackSetting = () => {
  const [form] = Form.useForm()
  const [loading, setLoading] = useState(true)
  const messageApi = useMessage()

  const fetchSettings = async () => {
    try {
      setLoading(true)
      const res = await getMatchFallback()
      form.setFieldsValue({
        matchFallbackEnabled: res.data.value === 'true',
      })
    } catch (error) {
      messageApi.error('获取匹配后备设置失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchSettings()
  }, [])

  const handleValueChange = async changedValues => {
    try {
      await setMatchFallback({ value: String(changedValues.matchFallbackEnabled) })
      messageApi.success('设置已保存')
    } catch (error) {
      messageApi.error('保存设置失败')
      // Revert UI state on save failure
      fetchSettings()
    }
  }

  return (
    <Card title="配置" loading={loading}>
      <Form form={form} onValuesChange={handleValueChange}>
        <Form.Item
          name="matchFallbackEnabled"
          label="启用匹配后备"
          valuePropName="checked"
          tooltip="启用后，当播放客户端尝试使用match接口时，接口在本地库中找不到任何结果时，系统将自动触发一个后台任务，尝试从全网搜索并导入对应的弹幕。"
        >
          <Switch />
        </Form.Item>
      </Form>
    </Card>
  )
}
