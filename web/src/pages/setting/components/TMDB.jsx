import { Button, Card, Form, Input, message } from 'antd'
import { useEffect, useState } from 'react'
import { getTmdbConfig, setTmdbConfig } from '../../../apis'
import { useAtomValue } from 'jotai'
import { userinfoAtom } from '../../../../store'

export const TMDB = () => {
  const [form] = Form.useForm()
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const userinfo = useAtomValue(userinfoAtom)
  const perms = userinfo?.permissions || {}
  const canEdit = userinfo?.role === 'admin' ? (perms.editTmdb !== false) : !!perms.editTmdb
  const canView = canEdit

  useEffect(() => {
    if (!canView) { setLoading(false); return }
    setLoading(true)
    getTmdbConfig()
      .then(res => {
        // 期望返回 { tmdbApiKey, tmdbApiBaseUrl, tmdbImageBaseUrl }
        const data = res.data || {}
        form.setFieldsValue(data)
      })
      .catch(err => {
        message.error(err?.detail || '获取TMDB配置失败')
      })
      .finally(() => setLoading(false))
  }, [canView])

  const onSave = async () => {
    try {
      setSaving(true)
      const values = await form.validateFields()
      await setTmdbConfig(values)
      message.success('保存成功')
    } catch (e) {
      message.error(e?.detail || '保存失败')
    } finally { setSaving(false) }
  }

  return (
    <div className="my-6">
      <Card title="TMDB 配置" loading={loading}>
        {!canView ? (
          <div className="text-gray-500">您没有查看 TMDB 配置的权限，请联系管理员。</div>
        ) : (
          <Form form={form} layout="vertical" className="max-w-xl">
            <Form.Item name="tmdbApiKey" label="API Key (v3)" rules={[{ required: true, message: '请输入TMDB API Key' }]}>
              <Input.Password placeholder="请输入 TMDB API Key" disabled={!canEdit} />
            </Form.Item>
            <Form.Item name="tmdbApiBaseUrl" label="API 域名">
              <Input placeholder="如 https://api.themoviedb.org/3" disabled={!canEdit} />
            </Form.Item>
            <Form.Item name="tmdbImageBaseUrl" label="图片域名">
              <Input placeholder="如 https://image.tmdb.org/t/p/w500" disabled={!canEdit} />
            </Form.Item>
            <Form.Item>
              <Button type="primary" onClick={onSave} loading={saving} disabled={!canEdit}>保存</Button>
            </Form.Item>
          </Form>
        )}
      </Card>
    </div>
  )
}
