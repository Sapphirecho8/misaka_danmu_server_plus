import { useState } from 'react'
import { Form, Input, InputNumber, Modal, Select, message } from 'antd'
import { createAnimeEntry } from '../apis'
import { useMessage } from '../MessageContext'

export const CreateAnimeModal = ({ open, onCancel, onSuccess }) => {
  const [form] = Form.useForm()
  const [loading, setLoading] = useState(false)
  const messageApi = useMessage()

  const handleOk = async () => {
    try {
      const values = await form.validateFields()
      setLoading(true)
      const res = await createAnimeEntry(values)
      if (res.data) {
        messageApi.success('作品创建成功！')
        onSuccess(res.data) // 将新创建的作品数据传递回去，以便刷新列表
        form.resetFields()
      }
    } catch (error) {
      console.error('创建作品失败:', error)
      messageApi.error(error.detail || '创建作品失败，请检查日志')
    } finally {
      setLoading(false)
    }
  }

  return (
    <Modal
      title="新建作品条目"
      open={open}
      onOk={handleOk}
      onCancel={onCancel}
      confirmLoading={loading}
      destroyOnHidden
    >
      <Form
        form={form}
        layout="vertical"
        name="create_anime_form"
        className="!px-4 !pt-6"
      >
        <Form.Item
          name="title"
          label="作品标题"
          rules={[{ required: true, message: '请输入作品标题！' }]}
        >
          <Input placeholder="例如：亮剑" />
        </Form.Item>
        <Form.Item
          name="type"
          label="类型"
          rules={[{ required: true, message: '请选择作品类型！' }]}
          initialValue="tv_series"
        >
          <Select>
            <Select.Option value="tv_series">电视剧/番剧</Select.Option>
            <Select.Option value="movie">电影/剧场版</Select.Option>
          </Select>
        </Form.Item>
        <Form.Item name="season" label="季度" initialValue={1}>
          <InputNumber min={1} style={{ width: '100%' }} />
        </Form.Item>
        <Form.Item name="year" label="年份">
          <InputNumber
            placeholder="例如：2005"
            min={1900}
            max={new Date().getFullYear() + 5}
            style={{ width: '100%' }}
          />
        </Form.Item>
      </Form>
    </Modal>
  )
}
