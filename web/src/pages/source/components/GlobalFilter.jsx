import { Button, Card, Form, Input, message, Tooltip } from 'antd'
import { useEffect, useState } from 'react'
import { getGlobalFilter, setGlobalFilter } from '../../../apis'
import { QuestionCircleOutlined } from '@ant-design/icons'
import { useMessage } from '../../../MessageContext'

export const GlobalFilter = () => {
  const [loading, setLoading] = useState(true)
  const [form] = Form.useForm()
  const [isSaveLoading, setIsSaveLoading] = useState(false)
  const messageApi = useMessage()

  useEffect(() => {
    getGlobalFilter()
      .then(res => {
        form.setFieldsValue(res.data ?? { cn: '', eng: '' })
      })
      .finally(() => {
        setLoading(false)
      })
  }, [form])

  const handleSave = async () => {
    try {
      setIsSaveLoading(true)
      const values = await form.validateFields()
      await setGlobalFilter(values)
      messageApi.success('保存成功')
    } catch (error) {
      messageApi.error('保存失败')
    } finally {
      setIsSaveLoading(false)
    }
  }

  return (
    <div className="my-6">
      <Card loading={loading} title="全局搜索结果标题过滤">
        <div className="mb-4">
          此处配置的正则表达式将应用于所有搜索源返回的结果标题，用于在搜索阶段过滤掉非正片内容（如预告合集、幕后花絮等）。
        </div>
        <Form
          form={form}
          layout="vertical"
          onFinish={handleSave}
          className="px-6 pb-6"
        >
          <Form.Item
            name="cn"
            label={
              <span>
                中文规则 (关键词)
                <Tooltip title="用于匹配标题中任何位置出现的中文关键词，例如'特典|预告|广告'。">
                  <QuestionCircleOutlined className="ml-2 cursor-pointer text-gray-400" />
                </Tooltip>
              </span>
            }
            className="mb-6"
          >
            <Input.TextArea
              rows={4}
              placeholder="请输入中文过滤关键词，使用 | 分隔"
            />
          </Form.Item>

          <Form.Item
            name="eng"
            label={
              <span>
                英文/缩写规则 (独立词)
                <Tooltip title="用于匹配独立的英文缩写或单词，例如'OP|ED|PV'。系统会自动处理单词边界。">
                  <QuestionCircleOutlined className="ml-2 cursor-pointer text-gray-400" />
                </Tooltip>
              </span>
            }
            className="mb-6"
          >
            <Input.TextArea
              rows={4}
              placeholder="请输入英文/缩写过滤关键词，使用 | 分隔"
            />
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
