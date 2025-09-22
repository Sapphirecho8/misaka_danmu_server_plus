import { Form, Input, Modal, Select, message } from 'antd'
import { useState } from 'react'
import { addSourceToAnime } from '../apis'
import { useMessage } from '../MessageContext'
import { MyIcon } from '@/components/MyIcon'
import { generateRandomStr } from '../utils/data'

// 通用数据源列表，将来可以从后端动态获取
const PROVIDER_OPTIONS = [
  { value: 'custom', label: '自定义 (Custom)' },
  { value: 'bilibili', label: 'Bilibili' },
  { value: 'tencent', label: '腾讯视频 (Tencent)' },
  { value: 'iqiyi', label: '爱奇艺 (iQiyi)' },
  { value: 'youku', label: '优酷 (Youku)' },
  { value: 'mgtv', label: '芒果TV (MGTV)' },
  { value: 'renren', label: '人人视频' },
]

export const AddSourceModal = ({ open, animeId, onCancel, onSuccess }) => {
  const [form] = Form.useForm()
  const [loading, setLoading] = useState(false)
  const messageApi = useMessage()

  const handleOk = async () => {
    if (!animeId) return
    try {
      const values = await form.validateFields()
      setLoading(true)
      // 修正：将 animeId 和表单值合并成一个对象再传递
      const res = await addSourceToAnime({ ...values, animeId })
      if (res.data) {
        messageApi.success('数据源添加成功！')
        onSuccess(res.data) // 将新创建的数据源信息传递回去
        form.resetFields()
      }
    } catch (error) {
      console.error('添加数据源失败:', error)
      messageApi.error(error.detail || '添加数据源失败，请检查日志')
    } finally {
      setLoading(false)
    }
  }

  return (
    <Modal
      title="添加数据源"
      open={open}
      onOk={handleOk}
      onCancel={onCancel}
      confirmLoading={loading}
      destroyOnHidden
    >
      <Form
        form={form}
        layout="vertical"
        name="add_source_form"
        className="!px-4 !pt-6"
      >
        <Form.Item
          name="providerName"
          label="数据源平台"
          rules={[{ required: true, message: '请选择平台！' }]}
          initialValue="custom"
        >
          <Select
            showSearch
            options={PROVIDER_OPTIONS}
            placeholder="选择一个平台"
          />
        </Form.Item>
        <Form.Item
          name="mediaId"
          label="媒体ID"
          rules={[{ required: true, message: '请输入媒体ID！' }]}
          help="对于'自定义'源，可填写任意唯一标识，如'manual-1'。"
        >
          <Input
            placeholder="例如：ss28235"
            addonAfter={
              <div
                className="cursor-pointer"
                onClick={() => {
                  const value = generateRandomStr()
                  form.setFieldsValue({
                    mediaId: value,
                  })
                }}
              >
                <MyIcon icon="refresh" size={20} />
              </div>
            }
          />
        </Form.Item>
      </Form>
    </Modal>
  )
}
