import { useEffect, useState, useRef, useMemo } from 'react'
import { getRateLimitStatus, getUsageAggregate, setGlobalRateLimit } from '../../../apis/index.js'
import { ResponsiveContainer, BarChart as RBarChart, Bar, LabelList, XAxis, YAxis, Tooltip as RTooltip, Legend, PieChart as RPieChart, Pie, Cell } from 'recharts'
import { toPng, toSvg } from 'html-to-image'
import { message } from 'antd'
import { Form, InputNumber, Button, Space, Tooltip, Switch } from 'antd'
import { SaveOutlined } from '@ant-design/icons'
import { useAtomValue } from 'jotai'
import { userinfoAtom } from '../../../../store/index.js'
import {
  Card,
  Table,
  Typography,
  Progress,
  Row,
  Col,
  Statistic,
  Alert,
} from 'antd'

const { Title, Paragraph } = Typography

const periodLabelMap = {
  second: '秒',
  minute: '分钟',
  hour: '小时',
  day: '天',
}

export const RateLimitPanel = () => {
  const [status, setStatus] = useState(null)
  const [loading, setLoading] = useState(true)
  const timer = useRef()
  const [agg, setAgg] = useState({ items: [], windowHours: 24 })
  const [selectedWindow, setSelectedWindow] = useState(24) // number | '1mo' | 'all'
  const selectedRef = useRef(24)
  const [sortOrder, setSortOrder] = useState('desc') // 'asc' | 'desc'
  const [view, setView] = useState('table') // 'table' | 'bar' | 'pie'
  const [isAdminAggregate, setIsAdminAggregate] = useState(false)
  const aggCardRef = useRef(null)
  const chartWrapRef = useRef(null)
  const [exporting, setExporting] = useState(false)
  const userinfo = useAtomValue(userinfoAtom)
  const isSuperAdmin = ((userinfo?.username || '').toLowerCase() === 'admin')
  const [editingGlobalLimit, setEditingGlobalLimit] = useState(null)
  const [unlimited, setUnlimited] = useState(false)
  const [saving, setSaving] = useState(false)

  // 先计算是否为管理员视图，避免在声明前引用（TDZ错误）
  const isAdminView = useMemo(() => {
    return !!(status && Array.isArray(status.users) && status.users.length >= 0)
  }, [status])

  const fetchStatus = async () => {
    try {
      const res = await getRateLimitStatus()
      setStatus(res.data)
      if (editingGlobalLimit === null) {
        const cur = Number(res.data?.globalLimit ?? 1000)
        if (!Number.isNaN(cur)) {
          setEditingGlobalLimit(cur < 0 ? 1000 : cur)
          setUnlimited(cur < 0)
        }
      }
      if (loading) setLoading(false)
    } catch (error) {
      console.error('获取流控状态失败:', error)
      if (loading) setLoading(false)
    }
  }

  const fetchAggregate = async (win) => {
    try {
      const choice = win ?? selectedRef.current
      let params = {}
      if (choice === 'all') {
        params = { all: true }
      } else if (choice === '1mo') {
        params = { windowHours: 720 }
      } else {
        params = { windowHours: choice }
      }
      const res = await getUsageAggregate(params)
      // 用本地窗口更新标题显示
      setAgg({ items: res.data.items || [], windowHours: choice })
      setIsAdminAggregate(true)
    } catch (e) {
      // 管理员接口，非管理员可能403
      setIsAdminAggregate(false)
    }
  }
  // 维护选择的窗口引用，避免闭包导致回退
  useEffect(() => {
    selectedRef.current = selectedWindow
  }, [selectedWindow])

  // 初次加载
  useEffect(() => {
    fetchStatus()
  }, [])

  // 根据是否为管理员视图动态调整刷新频率：管理员 1s，其余 5s
  useEffect(() => {
    if (timer.current) clearInterval(timer.current)
    const interval = isAdminView ? 1000 : 5000
    timer.current = setInterval(() => {
      fetchStatus()
      if (isAdminView) fetchAggregate(selectedRef.current)
    }, interval)
    return () => {
      if (timer.current) clearInterval(timer.current)
    }
  }, [isAdminView])

  // 管理员视图切换时立即拉一次聚合，使用当前选择窗口
  useEffect(() => {
    if (isAdminView) fetchAggregate(selectedRef.current)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAdminView])

  const windowLabel = useMemo(() => {
    if (selectedWindow === 'all') return '全部'
    if (selectedWindow === '1mo') return '1个月'
    return `${selectedWindow}小时`
  }, [selectedWindow])

  const sortedItems = useMemo(() => {
    const arr = Array.isArray(agg.items) ? [...agg.items] : []
    arr.sort((a, b) => sortOrder === 'asc' ? (a.downloads - b.downloads) : (b.downloads - a.downloads))
    return arr
  }, [agg.items, sortOrder])

  // 为不同用户分配明显不同的颜色
  const COLORS = useMemo(() => [
    '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
    '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf',
    '#a6cee3', '#fb9a99', '#b2df8a', '#fdbf6f', '#cab2d6',
    '#6a3d9a', '#ffff99', '#b15928', '#1b9e77', '#d95f02'
  ], [])

  const chartData = useMemo(() => sortedItems.map((it, idx) => ({ ...it, fill: COLORS[idx % COLORS.length] })), [sortedItems, COLORS])
  const totalDownloads = useMemo(() => sortedItems.reduce((s, it) => s + (Number(it.downloads)||0), 0), [sortedItems])

  const renderBarLabel = (props) => {
    const { x, y, width, height, value } = props
    const v = Number(value) || 0
    const pct = totalDownloads > 0 ? Math.round((v / totalDownloads) * 100) : 0
    const tx = x + width / 2
    const ty = y - 6
    const fz = exporting ? 14 : 12
    return (
      <text x={tx} y={ty} fill="#111827" textAnchor="middle" fontSize={fz}>{`${v} (${pct}%)`}</text>
    )
  }

  // 饼图自定义外部标签：引导线先斜线再横线，横线末端标注用户名
  const renderPieLabel = ({ cx, cy, midAngle, innerRadius, outerRadius, percent, index, payload }) => {
    const RADIAN = Math.PI / 180
    const angle = -midAngle * RADIAN
    const sin = Math.sin(angle)
    const cos = Math.cos(angle)
    const or = typeof outerRadius === 'number' ? outerRadius : 0
    const baseR = or || 100
    // 加粗加长引导线：增加径向延伸和水平延伸长度
    const RADIAL_EXTEND = 24
    const HORIZONTAL_EXTEND = 28
    const sx = cx + baseR * cos
    const sy = cy + baseR * sin
    const mx = cx + (baseR + RADIAL_EXTEND) * cos
    const my = cy + (baseR + RADIAL_EXTEND) * sin
    const isRight = cos >= 0
    const ex = mx + (isRight ? HORIZONTAL_EXTEND : -HORIZONTAL_EXTEND)
    const ey = my
    const name = payload?.username ?? ''
    const val = Number(payload?.downloads) || 0
    const pct = Math.round((percent || 0) * 100)
    const stroke = payload?.fill || '#374151'
    const textX = ex + (isRight ? 6 : -6)
    const anchor = isRight ? 'start' : 'end'
    const fz = 20
    const LINE_WIDTH = 3
    return (
      <g>
        <polyline points={`${sx},${sy} ${mx},${my} ${ex},${ey}`} fill="none" stroke={stroke} strokeWidth={LINE_WIDTH} />
        <circle cx={ex} cy={ey} r={1.5} fill={stroke} />
        <text x={textX} y={ey} fill="#111827" textAnchor={anchor} dominantBaseline="central" fontSize={fz}>
          {`${name} ${val} (${pct}%)`}
        </text>
      </g>
    )
  }

  return (
    <div className="my-6">
      <Card loading={loading}>
        <Typography>
          <Title level={4}>流控状态面板</Title>
          <Paragraph>
            此面板实时显示全局和各源的弹幕下载速率限制状态。特定源的配额包含在全局限制内。
          </Paragraph>
        </Typography>
        {status && (
          <>
            {/* 前端提示已移除：不再显示验证失败的警告弹窗 */}
            <Card type="inner" title="全局限制" className="!mb-6">
              <Row gutter={[16, 16]} align="middle">
                <Col xs={24} sm={12} md={8}>
                  <Statistic
                    title="全局状态"
                    value={
                      status.verificationFailed
                        ? '验证失败 (已锁定)'
                        : status.globalEnabled
                          ? '已启用'
                          : '已禁用'
                    }
                    valueStyle={{
                      color: status.verificationFailed
                        ? 'var(--color-red-600)'
                        : undefined,
                    }}
                  />
                </Col>
                <Col xs={24} sm={12} md={8}>
                  <Statistic
                    title={`全局使用量 (每${periodLabelMap[status.globalPeriod] || status.globalPeriod})`}
                    value={status.globalRequestCount}
                    suffix={status.globalLimit < 0 ? ' / ∞' : ` / ${status.globalLimit}`}
                    className={status.verificationFailed ? 'opacity-50' : ''}
                  />
                </Col>
                <Col xs={24} sm={24} md={8}>
                  <Statistic.Timer
                    title="重置倒计时"
                    value={Date.now() + status.secondsUntilReset * 1000}
                    format="HH:mm:ss"
                    type="countdown"
                    className={status.verificationFailed ? 'opacity-50' : ''}
                  />
                </Col>
                <Col span={24}>
                  <Progress
                    percent={status.globalLimit > 0 ? (status.globalRequestCount / status.globalLimit) * 100 : 0}
                    showInfo={false}
                    className={status.verificationFailed ? 'opacity-50' : ''}
                  />
                </Col>
              </Row>
              {isSuperAdmin && (
                <div className="mt-4">
                  <Form layout="inline" onFinish={async () => {
                    try {
                      setSaving(true)
                      const value = unlimited ? -1 : Number(editingGlobalLimit)
                      await setGlobalRateLimit({ globalLimit: value })
                      message.success('全局流控已更新')
                      fetchStatus()
                    } catch (e) {
                      message.error(e?.detail || '更新失败')
                    } finally {
                      setSaving(false)
                    }
                  }}>
                    <Form.Item>
                      <Space align="center">
                        <Tooltip title="开启后，本小时不限制请求次数">
                          <span className="text-sm text-gray-600">不限制</span>
                        </Tooltip>
                        <Switch checked={unlimited} onChange={checked => setUnlimited(checked)} />
                      </Space>
                    </Form.Item>
                    <Form.Item>
                      <Space align="center">
                        <span className="text-sm text-gray-600">自定义上限</span>
                        <InputNumber
                          min={0}
                          step={100}
                          size="small"
                          disabled={unlimited}
                          value={editingGlobalLimit === '' ? null : Number(editingGlobalLimit)}
                          onChange={v => setEditingGlobalLimit(typeof v === 'number' ? v : '')}
                          addonAfter="次/小时"
                          style={{ width: 180 }}
                        />
                      </Space>
                    </Form.Item>
                    <Form.Item>
                      <Button type="primary" icon={<SaveOutlined />} size="small" htmlType="submit" loading={saving} disabled={!unlimited && (editingGlobalLimit === null || editingGlobalLimit === '' || Number(editingGlobalLimit) < 0)}>
                        保存
                      </Button>
                    </Form.Item>
                  </Form>
                </div>
              )}
            </Card>
            {/* 用户配额区块：现在置于“全局限制”下方 */}
            <Card type="inner" title="用户配额使用情况" className="!mt-6">
              {/* 非管理员：显示当前用户用量 */}
              {!isAdminView && status.currentUser && (
                <Row gutter={[16, 16]} align="middle">
                  <Col xs={24} sm={12} md={8}>
                    <Statistic title="用户名" value={status.currentUser.username} />
                  </Col>
                  <Col xs={24} sm={12} md={8}>
                    <Statistic
                      title="已用 / 限额 (每小时)"
                      value={status.currentUser.used}
                      suffix={`/ ${status.currentUser.limit ?? '∞'}`}
                    />
                  </Col>
                  <Col xs={24} sm={24} md={8}>
                    <Statistic.Timer
                      title="重置倒计时"
                      value={Date.now() + (status.currentUser.secondsUntilReset ?? 0) * 1000}
                      format="HH:mm:ss"
                      type="countdown"
                    />
                  </Col>
                </Row>
              )}

              {/* 管理员：显示所有用户表格 */}
              {isAdminView && (
                <Table
                  columns={[
                    { title: '用户名', dataIndex: 'username', key: 'username' },
                    { title: '已用', dataIndex: 'used', key: 'used', width: 100 },
                    { title: '限额(每小时)', dataIndex: 'limit', key: 'limit', width: 140, render: v => (v ?? '∞') },
                    {
                      title: '重置剩余',
                      key: 'reset',
                      width: 160,
                      render: (_, r) => {
                        const ms = (r.secondsUntilReset ?? 0) * 1000
                        // 简化：用纯文本显示 mm:ss，避免每行都渲染一个计时器造成性能负担
                        const sec = Math.max(0, Math.floor(ms / 1000))
                        const mm = String(Math.floor(sec / 60)).padStart(2, '0')
                        const ss = String(sec % 60).padStart(2, '0')
                        return `${mm}:${ss}`
                      },
                    },
                  ]}
                  dataSource={status.users || []}
                  rowKey={r => `${r.userId}-${r.username}`}
                  pagination={{ pageSize: 10 }}
                />
              )}
            </Card>

            {/* 管理员：用户用量聚合（时间窗） */}
            {isAdminView && (
              <div ref={aggCardRef}>
              <Card type="inner" title="用户用量聚合" className="!mt-6">
                <div className="mb-3 flex items-center gap-3 flex-wrap">
                  <span>时间窗：</span>
                  {[1, 6, 24, 72, 168, '1mo', 'all'].map(h => (
                    <button
                      key={h}
                      className={`px-3 py-1 border rounded ${selectedWindow===h?'bg-blue-500 text-white':'bg-white'}`}
                      onClick={() => { setSelectedWindow(h); selectedRef.current = h; fetchAggregate(h) }}
                    >{h === '1mo' ? '1mo' : h === 'all' ? 'All' : `${h}h`}</button>
                  ))}
                  <span className="ml-4">排序：</span>
                  {[{k:'desc',t:'从大到小'},{k:'asc',t:'从小到大'}].map(o => (
                    <button key={o.k} className={`px-3 py-1 border rounded ${sortOrder===o.k?'bg-gray-800 text-white':'bg-white'}`} onClick={() => setSortOrder(o.k)}>{o.t}</button>
                  ))}
                  <span className="ml-4">显示：</span>
                  {[{k:'table',t:'表格'},{k:'bar',t:'条形图'},{k:'pie',t:'饼状图'}].map(v => (
                    <button key={v.k} className={`px-3 py-1 border rounded ${view===v.k?'bg-green-600 text-white':'bg-white'}`} onClick={() => setView(v.k)}>{v.t}</button>
                  ))}
                </div>

                {view === 'table' && (
                  <Table
                    columns={[
                      { title: '用户名', dataIndex: 'username', key: 'username' },
                      { title: '下载(集)', dataIndex: 'downloads', key: 'downloads', width: 120, onHeaderCell: () => ({ onClick: () => setSortOrder(s => s === 'desc' ? 'asc' : 'desc'), style: { cursor: 'pointer' } }) },
                    ]}
                    dataSource={sortedItems}
                    rowKey={r => `${r.userId}-${r.username}`}
                    pagination={{ pageSize: 10 }}
                  />
                )}

                {view === 'bar' && (
                  <div ref={chartWrapRef} style={{ width: '100%', height: 430 }}>
                    <ResponsiveContainer>
                      <RBarChart data={chartData} margin={{ top: 72, right: 20, left: 16, bottom: 46 }}>
                        <XAxis dataKey="username" angle={-30} textAnchor="end" interval={0} height={60} />
                        <YAxis label={{ value: '下载(集)', angle: -90, position: 'insideLeft', offset: 8 }} />
                        <RTooltip formatter={(v)=>[v,'下载(集)']} />
                        <Legend />
                        <Bar dataKey="downloads" name="下载(集)">
                          {chartData.map((entry, index) => (
                            <Cell key={`cell-${index}`} fill={entry.fill} />
                          ))}
                          <LabelList content={renderBarLabel} />
                        </Bar>
                      </RBarChart>
                    </ResponsiveContainer>
                  </div>
                )}

                {view === 'pie' && (
                  <div ref={chartWrapRef} style={{ width: '100%' }}>
                    <ResponsiveContainer width="100%" height={396}>
                      <RPieChart margin={{ top: 0, right: 8, left: 8, bottom: 0 }}>
                        <Pie
                          data={chartData}
                          dataKey="downloads"
                          nameKey="username"
                          cx="44%"
                          cy="50%"
                          outerRadius={126}
                          innerRadius={63}
                          labelLine={false}
                          label={renderPieLabel}
                        >
                          {chartData.map((entry, index) => (
                            <Cell key={`slice-${index}`} fill={entry.fill} />
                          ))}
                        </Pie>
                        <RTooltip formatter={(v, n, p)=>[v, p && p.payload ? p.payload.username : '用户']} />
                        <Legend />
                      </RPieChart>
                    </ResponsiveContainer>
                  </div>
                )}

                <div className="mt-4 flex gap-3 justify-end">
                  <button className="px-3 py-1 border rounded" onClick={async () => {
                    // Export CSV
                    const rows = [['用户名','下载(集)'], ...sortedItems.map(it => [it.username, String(it.downloads)])]
                    const csv = rows.map(r => r.map(f => `${String(f).replace(/"/g,'""')}`).join(',')).join('\n')
                    const blob = new Blob([`\uFEFF${csv}`], { type: 'text/csv;charset=utf-8;' })
                    const url = URL.createObjectURL(blob)
                    const a = document.createElement('a')
                    a.href = url
                    a.download = `usage_${selectedWindow}.csv`
                    document.body.appendChild(a)
                    a.click()
                    a.remove()
                    URL.revokeObjectURL(url)
                    message.success('CSV 已导出')
                  }}>导出 CSV</button>
                  <button className="px-3 py-1 border rounded" onClick={async () => {
                    // 优先导出图表中的 SVG（最稳妥），不存在则导出整卡片
                    setExporting(true)
                    await new Promise(r => setTimeout(r, 50))
                    const chartNode = chartWrapRef.current
                    if (chartNode) {
                      const svgEl = chartNode.querySelector('svg')
                      if (svgEl) {
                        try {
                          const serializer = new XMLSerializer()
                          let svgStr = serializer.serializeToString(svgEl)
                          // 添加 xmlns 以确保跨浏览器兼容
                          if (!svgStr.match(/^<svg[^>]+xmlns=/)) {
                            svgStr = svgStr.replace('<svg', '<svg xmlns="http://www.w3.org/2000/svg"')
                          }
                          const bbox = svgEl.getBoundingClientRect()
                          const img = new Image()
                          img.crossOrigin = 'anonymous'
                          const svgDataUrl = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svgStr)
                          await new Promise((resolve, reject) => {
                            img.onload = resolve
                            img.onerror = reject
                            img.src = svgDataUrl
                          })
                          const canvas = document.createElement('canvas')
                          canvas.width = Math.max(1, Math.floor(bbox.width))
                          canvas.height = Math.max(1, Math.floor(bbox.height))
                          const ctx = canvas.getContext('2d')
                          ctx.fillStyle = '#ffffff'
                          ctx.fillRect(0, 0, canvas.width, canvas.height)
                          ctx.drawImage(img, 0, 0, canvas.width, canvas.height)
                          // 等比例裁剪左右留白：左右各裁剪相同像素
                          const SHRINK_X = 140
                          console.info(`[UsageAggregate Export] Pie horizontal shrink per side: ${SHRINK_X}px`)
                          const cropW = Math.max(1, canvas.width - SHRINK_X * 2)
                          const cropH = canvas.height
                          const cropped = document.createElement('canvas')
                          cropped.width = cropW
                          cropped.height = cropH
                          const cctx = cropped.getContext('2d')
                          cctx.fillStyle = '#ffffff'
                          cctx.fillRect(0, 0, cropW, cropH)
                          cctx.drawImage(canvas, SHRINK_X, 0, cropW, cropH, 0, 0, cropW, cropH)
                          const pngUrl = cropped.toDataURL('image/png')
                          const a = document.createElement('a')
                          a.download = `usage_${selectedWindow}.png`
                          a.href = pngUrl
                          document.body.appendChild(a)
                          a.click()
                          a.remove()
                          message.success('PNG 已导出')
                          setExporting(false)
                          return
                        } catch (err) {
                          console.warn('SVG 转 PNG 失败，回退到 DOM 导出', err)
                        }
                      }
                    }
                    // 回退：导出整卡片区域
                    const node = aggCardRef.current
                    if (!node) { message.error('未找到可导出的区域'); return }
                    try {
                      const dataUrl = await toPng(node, { cacheBust: true, backgroundColor: '#ffffff', pixelRatio: 2 })
                      const link = document.createElement('a')
                      link.download = `usage_${selectedWindow}.png`
                      link.href = dataUrl
                      document.body.appendChild(link)
                      link.click()
                      link.remove()
                      message.success('PNG 已导出')
                    } catch (e) {
                      console.error('导出PNG失败', e)
                      message.error('导出PNG失败')
                    } finally { setExporting(false) }
                  }}>导出 PNG</button>
                </div>
              </Card>
              </div>
            )}

            {/* 各源配额使用情况：移动到“用户配额使用情况”之后 */}
            <Card
              type="inner"
              title="各源配额使用情况"
              className={status.verificationFailed ? 'opacity-50' : ''}
            >
              <Table
                columns={[
                  {
                    title: '搜索源',
                    dataIndex: 'providerName',
                    key: 'providerName',
                  },
                  {
                    title: '使用情况 (已用 / 配额)',
                    key: 'usage',
                    render: (_, record) =>
                      `${record.requestCount} / ${record.quota}`,
                  },
                ]}
                dataSource={status.providers}
                rowKey="providerName"
                pagination={false}
              />
            </Card>
          </>
        )}
      </Card>
    </div>
  )
}
