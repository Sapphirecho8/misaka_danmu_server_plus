import {
  Button,
  Card,
  Col,
  Form,
  Input,
  Row,
  Tooltip,
  Select,
  Switch,
  Spin,
  Tag,
  Divider,
  Space,
} from 'antd'
import { useEffect, useState } from 'react'
import { getProxyConfig, setProxyConfig, testProxy } from '../../../apis'
import { useAtomValue } from 'jotai'
import { userinfoAtom } from '../../../../store'
import { useMessage } from '../../../MessageContext'

export const Proxy = () => {
  const userinfo = useAtomValue(userinfoAtom)
  const perms = userinfo?.permissions || {}
  const isSuperAdmin = (userinfo?.username || '').toLowerCase() === 'admin'
  const canEdit = isSuperAdmin || !!perms.editProxy
  const [proxyEnabled, setProxyEnabled] = useState(false)
  const [loading, setLoading] = useState(true)
  const [form] = Form.useForm()
  const [isSaveLoading, setIsSaveLoading] = useState(false)
  const [isTestLoading, setIsTestLoading] = useState(false)
  const [testResult, setTestResult] = useState(null)
  const messageApi = useMessage()

  useEffect(() => {
    getProxyConfig()
      .then(res => {
        setProxyEnabled(res.data?.proxyEnabled ?? false)
        // 修正：确保 proxySslVerify 字段即使在API未返回时也有一个默认值
        form.setFieldsValue({
          ...res.data,
          proxySslVerify: res.data?.proxySslVerify ?? true,
        })
      })
      .finally(() => {
        setLoading(false)
      })
  }, [form])

  const handleSave = async () => {
    try {
      if (!canEdit) return
      setIsSaveLoading(true)
      const values = await form.validateFields()
      setProxyEnabled(values.proxyEnabled)
      await setProxyConfig(values)
      setIsSaveLoading(false)
      messageApi.success('保存成功')
    } catch (error) {
      messageApi.error('保存失败')
    } finally {
      setIsSaveLoading(false)
    }
  }

  const handleTest = async () => {
    try {
      if (!canEdit) return
      setIsTestLoading(true)
      setTestResult(null)
      const values = await form.validateFields()
      let proxyUrl = ''
      if (
        values.proxyEnabled &&
        values.proxyHost &&
        values.proxyPort &&
        values.proxyProtocol
      ) {
        let userinfo = ''
        if (values.proxyUsername) {
          userinfo = encodeURIComponent(values.proxyUsername)
          if (values.proxyPassword) {
            userinfo += ':' + encodeURIComponent(values.proxyPassword)
          }
          userinfo += '@'
        }
        proxyUrl = `${values.proxyProtocol}://${userinfo}${values.proxyHost}:${values.proxyPort}`
      }
      const res = await testProxy({ proxy_url: proxyUrl })
      setTestResult(res.data)
    } catch (error) {
      messageApi.error('测试请求失败')
    } finally {
      setIsTestLoading(false)
    }
  }

  const ResultTag = ({ result }) => {
    const isSuccess = result.status === 'success'
    const color = isSuccess ? 'green' : 'red'
    const text = isSuccess ? `成功 (${result.latency.toFixed(0)} ms)` : `失败: ${result.error}`
    return <Tag color={color}>{text}</Tag>
  }
  return (
    <div className="my-6">
      <Card loading={loading} title="代理配置" className={canEdit ? '' : 'opacity-60'}>
        <div className="mb-4">
          配置一个全局代理，可用于访问受限的网络资源。支持 http, https, socks5
          协议。
        </div>

        <Form
          form={form}
          layout="horizontal"
          onFinish={handleSave}
          className="px-6 pb-6"
          disabled={!canEdit}
        >
          <Form.Item
            name="proxyEnabled"
            label="启用代理"
            className="mb-6"
            valuePropName="checked"
          >
            <Switch onChange={checked => setProxyEnabled(checked)} />
          </Form.Item>
          <Form.Item name="proxyProtocol" label="协议" className="mb-6">
            <Select
              disabled={!proxyEnabled}
              options={[
                {
                  value: 'http',
                  label: 'http',
                },
                {
                  value: 'https',
                  label: 'https',
                },
                {
                  value: 'socks5',
                  label: 'socks5',
                },
              ]}
            />
          </Form.Item>
          <Row gutter={[12, 12]}>
            <Col md={12} xs={24}>
              <Form.Item name="proxyHost" label="主机" className="mb-4">
                <Input placeholder="例如：127.0.0.1" disabled={!proxyEnabled} />
              </Form.Item>
            </Col>
            <Col md={12} xs={24}>
              <Form.Item name="proxyPort" label="端口" className="mb-4">
                <Input placeholder="例如：7890" disabled={!proxyEnabled} />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={[12, 12]}>
            <Col md={12} xs={24}>
              <Form.Item
                name="proxyUsername"
                label="用户名(可选)"
                className="mb-4"
              >
                <Input disabled={!proxyEnabled} />
              </Form.Item>
            </Col>
            <Col md={12} xs={24}>
              <Form.Item
                name="proxyPassword"
                label="密码(可选)"
                className="mb-4"
              >
                <Input disabled={!proxyEnabled} />
              </Form.Item>
            </Col>
            <Col md={12} xs={24}>
              <Form.Item
                label="跳过SSL证书验证"
                name="proxySslVerify"
                valuePropName="checked"
                tooltip="当您的HTTPS代理使用自签名证书时，请开启此项以避免SSL验证错误。"
                className="mb-4"
              >
                <Switch disabled={!proxyEnabled} />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item>
            <div className="flex justify-end">
              <Space>
                <Button onClick={handleTest} loading={isTestLoading} disabled={!canEdit}>
                  测试连接
                </Button>
                <Button
                  type="primary"
                  htmlType="submit"
                  loading={isSaveLoading}
                  disabled={!canEdit}
                >
                  保存修改
                </Button>
              </Space>
            </div>
          </Form.Item>
        </Form>
        {!canEdit && (
          <div className="text-gray-500 text-sm">您没有权限编辑代理设置。</div>
        )}
        {isTestLoading && (
          <div className="text-center">
            <Spin />
            <p>正在测试连接，请稍候...</p>
          </div>
        )}
        {testResult && (
          <div>
            <Divider>测试结果</Divider>
            <div className="flex flex-col gap-2">
              {testResult.proxy_connectivity &&
                testResult.proxy_connectivity.status !== 'skipped' && (
                  <div className="flex justify-between">
                    <span>代理服务器连通性:</span>
                    <ResultTag result={testResult.proxy_connectivity} />
                  </div>
                )}
              {Object.entries(testResult.target_sites).map(([site, result]) => (
                <div key={site} className="flex justify-between">
                  <span>
                    {site.replace('https://', '').replace('http://', '')}:
                  </span>
                  <ResultTag result={result} />
                </div>
              ))}
            </div>
          </div>
        )}
      </Card>
    </div>
  )
}
