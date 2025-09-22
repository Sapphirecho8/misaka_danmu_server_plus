import { useEffect, useMemo, useRef, useState } from 'react'
import { Card, Table, Space, Button, message, Checkbox, Select } from 'antd'
import { ResponsiveContainer, BarChart as RBarChart, Bar, LabelList, XAxis, YAxis, Tooltip as RTooltip, Legend, PieChart as RPieChart, Pie, Cell } from 'recharts'
import { toPng } from 'html-to-image'
import { getTokenUsageAggregate } from '../../../apis'

export const TokenUsage = () => {
  const [items, setItems] = useState([])
  const [windowChoice, setWindowChoice] = useState(24) // 1|6|24|72|168|'1mo'|'all'
  const [loading, setLoading] = useState(false)
  const [sortOrder, setSortOrder] = useState('desc') // 'asc' | 'desc'
  const [view, setView] = useState('table') // 'table' | 'bar' | 'pie'
  const [exporting, setExporting] = useState(false)
  const cardRef = useRef(null)
  const chartWrapRef = useRef(null)
  const [includeZero, setIncludeZero] = useState(false)
  const [topN, setTopN] = useState(10) // 5|10|20|'all'
  const placedLeftRef = useRef([])
  const placedRightRef = useRef([])

  // 每次数据或视图切换时重置已放置标签的记录，避免累积偏移
  useEffect(() => {
    placedLeftRef.current = []
    placedRightRef.current = []
  }, [items, sortOrder, view, includeZero, topN])

  const fetchAgg = async (win = windowChoice) => {
    try {
      setLoading(true)
      let params = {}
      if (win === 'all') params = { all: true }
      else if (win === '1mo') params = { windowHours: 720 }
      else params = { windowHours: win }
      const res = await getTokenUsageAggregate(params)
      setItems(Array.isArray(res.data?.items) ? res.data.items : [])
    } catch (e) {
      // 普通用户也可查看（仅可见范围），这里不额外处理错误
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { fetchAgg(windowChoice) }, [])

  const sorted = useMemo(() => {
    const arr = [...(items || [])]
    arr.sort((a,b)=> (sortOrder==='desc' ? (b.calls - a.calls) : (a.calls - b.calls)))
    return arr
  }, [items, sortOrder])

  const chartData = useMemo(() => sorted.map(it => {
    const creator = it.createdByUsername || (it.createdByUserId ? `用户#${it.createdByUserId}` : '—')
    return {
      name: it.name,
      creator,
      label: `${creator}（${it.name}）`,
      calls: it.calls,
    }
  }), [sorted])

  const renderPieLabel = (props) => {
    const { cx, cy, midAngle, outerRadius, percent, value, payload } = props || {}
    const p = Math.round((percent || 0) * 100)
    if (p < 3) return null // 小于3%不渲染标签，避免拥挤
    const creatorRaw = payload?.creator ?? ''
    const tokenRaw = payload?.name ?? ''
    const used = value ?? payload?.calls ?? 0
    const creator = creatorRaw || (tokenRaw === '其他' ? '其他' : '')
    const token = tokenRaw === '其他' ? '' : tokenRaw
    const text = creator && token ? `${creator} ${token} ${used} (${p}%)` : `${creator || token} ${used} (${p}%)`
    // 根据角度计算文本基准点
    const RAD = Math.PI / 180
    const angle = -midAngle * RAD
    const ex = cx + (outerRadius + 30) * Math.cos(angle)
    const ey = cy + (outerRadius + 30) * Math.sin(angle)
    const isRight = Math.cos(angle) >= 0
    const baseX = ex + (isRight ? 12 : -12)
    let baseY = ey
    // 同侧标签避免重叠：逐行错开至少 22px（略高于 20px 字号）
    const arr = isRight ? placedRightRef.current : placedLeftRef.current
    const MIN_GAP = 22
    if (arr.length) {
      const last = arr[arr.length - 1]
      if (Math.abs(baseY - last) < MIN_GAP) {
        baseY = last + (baseY >= last ? MIN_GAP : -MIN_GAP)
      }
    }
    arr.push(baseY)
    return (
      <text x={baseX} y={baseY} dy={3} textAnchor={isRight ? 'start' : 'end'} fill="#333" style={{ fontSize: 20, fontWeight: 600 }}>
        {text}
      </text>
    )
  }

  const renderLabelLine = (props) => {
    const { points, stroke } = props || {}
    if (!points || points.length < 2) return null
    const p0 = points[0]
    const p1 = points[points.length - 2]
    const p2 = points[points.length - 1]
    const dx = (p2.x - p1.x)
    const dy = (p2.y - p1.y)
    const scale = 1.6 // 拉长引导线长度（相对基础长度的1.6倍）
    const ex = p1.x + dx * scale
    const ey = p1.y + dy * scale
    const d = `M${p0.x},${p0.y} L${p1.x},${p1.y} L${ex},${ey}`
    return <path d={d} stroke={stroke || '#999'} strokeWidth={2} fill="none" />
  }

  const pieData = useMemo(() => {
    // 过滤 0 调用
    let arr = includeZero ? chartData : chartData.filter(d => (d.calls || 0) > 0)
    // 仅显示 TopN，其余聚合为“其他”
    if (topN !== 'all') {
      const n = Number(topN) || 0
      if (n > 0 && arr.length > n) {
        const head = arr.slice(0, n)
        const tail = arr.slice(n)
        const otherSum = tail.reduce((s, x) => s + (x.calls || 0), 0)
        if (otherSum > 0) {
          head.push({ name: '其他', creator: '', label: '其他', calls: otherSum })
        }
        arr = head
      }
    }
    return arr
  }, [chartData, includeZero, topN])

  const COLORS = ['#1677ff', '#52c41a', '#faad14', '#eb2f96', '#722ed1', '#13c2c2', '#a0d911', '#fa541c']

  const exportCSV = () => {
    try {
      const headers = ['名称','类型','创建者','调用(次)']
      const lines = [headers.join(',')]
      for (const r of sorted) {
        const row = [
          JSON.stringify(r.name ?? ''),
          JSON.stringify(r.ownerUserId ? '私有' : '全局'),
          JSON.stringify(r.createdByUsername || (r.createdByUserId ? `用户#${r.createdByUserId}` : '—')),
          String(r.calls ?? 0),
        ]
        lines.push(row.join(','))
      }
      const csv = lines.join('\n')
      const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `token_usage_${windowChoice}.csv`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
      message.success('CSV 已导出')
    } catch (e) {
      console.error('CSV 导出失败', e)
      message.error('CSV 导出失败')
    }
  }

  const exportPNG = async () => {
    try {
      setExporting(true)
      await new Promise(r => setTimeout(r, 50))
      const chartNode = chartWrapRef.current
      if (chartNode) {
        const svgEl = chartNode.querySelector('svg')
        if (svgEl) {
          try {
            const serializer = new XMLSerializer()
            let svgStr = serializer.serializeToString(svgEl)
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
            const pngUrl = canvas.toDataURL('image/png')
            const a = document.createElement('a')
            a.download = `token_usage_${windowChoice}.png`
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
      const node = cardRef.current
      if (!node) { message.error('未找到可导出的区域'); setExporting(false); return }
      const dataUrl = await toPng(node, { cacheBust: true, backgroundColor: '#ffffff', pixelRatio: 2 })
      const link = document.createElement('a')
      link.download = `token_usage_${windowChoice}.png`
      link.href = dataUrl
      document.body.appendChild(link)
      link.click()
      link.remove()
      message.success('PNG 已导出')
    } catch (e) {
      console.error('导出PNG失败', e)
      message.error('导出PNG失败')
    } finally { setExporting(false) }
  }

  return (
    <Card type="inner" title="Token 使用情况" className="!mt-6" ref={cardRef}>
      <div className="mb-3 flex items-center gap-3 flex-wrap">
        <span>时间窗：</span>
        {[1, 6, 24, 72, 168, '1mo', 'all'].map(h => (
          <button
            key={h}
            className={`px-3 py-1 border rounded ${windowChoice===h?'bg-blue-500 text-white':'bg-white'}`}
            onClick={() => { setWindowChoice(h); fetchAgg(h) }}
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
        {view === 'pie' && (
          <>
            <span className="ml-4">饼图选项：</span>
            <Checkbox checked={includeZero} onChange={e=>setIncludeZero(e.target.checked)}>显示0调用</Checkbox>
            <span>Top：</span>
            <Select size="small" value={topN} style={{ width: 90 }} onChange={setTopN}
              options={[{value:5,label:'5'},{value:10,label:'10'},{value:20,label:'20'},{value:'all',label:'全部'}]}
            />
          </>
        )}
      </div>

      {view === 'table' && (
        <Table
          loading={loading}
          pagination={false}
          rowKey={r => r.tokenId}
          dataSource={sorted}
          columns={[
            { title: '名称', dataIndex: 'name', key: 'name', width: 160 },
            { title: '类型', key: 'scope', width: 80, render: (_, r) => (r.ownerUserId ? '私有' : '全局') },
            { title: '创建者', key: 'creator', width: 140, render: (_, r) => r.createdByUsername || (r.createdByUserId ? `用户#${r.createdByUserId}` : '—') },
            { title: '调用(次)', dataIndex: 'calls', key: 'calls', width: 120 },
          ]}
        />
      )}

      {view === 'bar' && (
        <div ref={chartWrapRef} style={{ width: '100%', height: 430 }}>
          <ResponsiveContainer>
            <RBarChart data={chartData} margin={{ top: 48, right: 20, left: 16, bottom: 46 }}>
              <XAxis dataKey="label" angle={-30} textAnchor="end" interval={0} height={80} />
              <YAxis allowDecimals={false} />
              <RTooltip formatter={(v) => [v, '调用(次)']} />
              <Legend />
              <Bar dataKey="calls" name="调用(次)" fill="#1677ff">
                <LabelList dataKey="calls" position="top" formatter={(v)=>String(v)} />
              </Bar>
            </RBarChart>
          </ResponsiveContainer>
        </div>
      )}

      {view === 'pie' && (
        <div ref={chartWrapRef} style={{ width: '100%', height: 430 }}>
          <ResponsiveContainer>
            <RPieChart>
              <RTooltip formatter={(v, n, o) => [`${v} 次`, (o?.payload?.creator ? `${o.payload.creator} ${o.payload.name}` : (o?.payload?.name || 'Token'))]} />
              <Legend />
              <Pie data={pieData} dataKey="calls" nameKey="name" outerRadius={140} cx="50%" cy="50%" label={renderPieLabel} labelLine={renderLabelLine}>
                {pieData.map((entry, index) => (
                  <Cell key={`cell-${index}`} fill={COLORS[index % COLORS.length]} />
                ))}
              </Pie>
            </RPieChart>
          </ResponsiveContainer>
        </div>
      )}
      <div className="mt-3 flex justify-end gap-2">
        <button className="px-3 py-1 border rounded" onClick={exportCSV}>导出 CSV</button>
        <button className="px-3 py-1 border rounded" onClick={exportPNG} disabled={exporting}>{exporting ? '导出中...' : '导出 PNG'}</button>
      </div>
    </Card>
  )
}
