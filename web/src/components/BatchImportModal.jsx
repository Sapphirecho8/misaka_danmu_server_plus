import {
  Button,
  Empty,
  Form,
  Input,
  InputNumber,
  List,
  Modal,
  Tag,
  Tooltip,
  Upload,
  message,
} from 'antd'
import { useEffect, useRef, useState } from 'react'
import { batchManualImport } from '../apis'
import {
  CloseCircleOutlined,
  CloudUploadOutlined,
  UploadOutlined,
} from '@ant-design/icons'
import { useMessage } from '../MessageContext'

import { MyIcon } from '@/components/MyIcon'

import {
  closestCorners,
  DndContext,
  DragOverlay,
  MouseSensor,
  TouchSensor,
  useSensor,
  useSensors,
} from '@dnd-kit/core'
import {
  SortableContext,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'

export const BatchImportModal = ({ open, sourceInfo, onCancel, onSuccess }) => {
  const messageApi = useMessage()
  const [loading, setLoading] = useState(false)
  // 存储解析后的XML数据列表
  const [xmlDataList, setXmlDataList] = useState([])
  const [fileList, setFileList] = useState([])
  // 存储上传状态
  const [uploading, setUploading] = useState(false)
  // 用于跟踪文件拖入顺序的计数器
  const orderCounter = useRef(0)
  // 存储文件ID与拖入顺序的映射
  const fileOrderMap = useRef({})
  const dragOverlayRef = useRef(null)
  const uploadRef = useRef(null)

  const [pageSize, setPageSize] = useState(10)
  const [activeItem, setActiveItem] = useState(null)

  const sensors = useSensors(
    useSensor(MouseSensor, {
      activationConstraint: {
        distance: 5,
      },
    }),
    useSensor(TouchSensor, {
      activationConstraint: {
        distance: 8,
        delay: 100,
      },
    })
  )

  const handleOk = async () => {
    if (!sourceInfo?.sourceId) return
    try {
      if (xmlDataList.length === 0) {
        messageApi.warn('未解析到任何有效条目！')
        return
      }
      setLoading(true)
      const res = await batchManualImport({
        sourceId: sourceInfo.sourceId,
        items: xmlDataList.map(it => {
          return {
            episodeIndex: it.episodeIndex,
            content: it.content,
            title: it.title ?? undefined,
          }
        }),
      })
      if (res.data) {
        // messageApi.success('批量导入任务已提交！')
        onSuccess(res.data)
        clearAll()
      }
    } catch (error) {
      console.error('批量导入失败:', error)
      messageApi.error(
        error.detail || error.message || '批量导入失败，请检查内容格式和日志'
      )
    } finally {
      setLoading(false)
    }
  }

  const clearAll = () => {
    // 清空文件列表
    setXmlDataList([])
    // 重置顺序计数器
    orderCounter.current = 0
    // 重置上传状态
    setUploading(false)
    // 清空上传组件的内部文件列表
    setFileList([])
  }

  const handleDelete = item => {
    setXmlDataList(list => {
      const activeIndex = list.findIndex(o => o.id === item.id)
      const newList = [...list]
      newList.splice(activeIndex, 1)
      return newList
    })
  }

  const handleEditIndex = (item, value) => {
    setXmlDataList(list => {
      return list.map(it => {
        if (it.id === item.id) {
          return {
            ...it,
            episodeIndex: value,
          }
        } else {
          return it
        }
      })
    })
  }

  const handleEditTitle = (item, value) => {
    setXmlDataList(list => {
      return list.map(it => {
        if (it.id === item.id) {
          return {
            ...it,
            title: value,
          }
        } else {
          return it
        }
      })
    })
  }

  const handleDragStart = event => {
    const { active } = event
    // 找到当前拖拽的项
    const item = xmlDataList.find(item => item.id === active.id)
    setActiveItem(item)
  }

  const handleDragEnd = event => {
    const { active, over } = event
    // 拖拽无效或未改变位置
    if (!over || active.id === over.id) {
      setActiveItem(null)
      return
    }

    setXmlDataList(list => {
      const activeIndex = list.findIndex(
        item => item.id === active.data.current.item.id
      )
      const overIndex = list.findIndex(
        item => item.id === over.data.current.item.id
      )

      if (activeIndex !== -1 && overIndex !== -1) {
        // 1. 重新排列数组
        const newList = [...xmlDataList]
        const [movedItem] = newList.splice(activeIndex, 1)
        newList.splice(overIndex, 0, movedItem)

        return newList
      }
      return list
    })

    setActiveItem(null)
  }

  /**
   * 处理文件上传
   * @param {Array} files - 上传的文件列表
   */
  const handleUpload = async ({ file }) => {
    const fileOrder = orderCounter.current++
    fileOrderMap.current[file.uid] = fileOrder
    setUploading(true)

    try {
      // 创建文件读取器
      const reader = new FileReader()

      reader.onload = async e => {
        try {
          const xmlContent = e.target.result
          // 将解析结果添加到列表
          setXmlDataList(prev => {
            const newItems = [
              ...prev,
              {
                id: file.uid,
                episodeIndex: fileOrder + 1,
                title: file.name?.split('.')?.[0],
                content: xmlContent,
                size: file.size,
                order: fileOrder,
              },
            ]

            newItems.sort((a, b) => a.order - b.order)
            const firstIndex = newItems?.[0]?.episodeIndex ?? 1
            return newItems.map((it, i) => {
              return {
                ...it,
                episodeIndex: firstIndex + i,
              }
            })
          })
        } catch (error) {
          messageApi.error(`文件 ${file.name} 解析失败: ${error.message}`)
        }
      }

      reader.readAsText(file)
    } catch (error) {
      messageApi.error(`文件处理失败: ${error.message}`)
    } finally {
      setUploading(false)
    }
  }

  const handleChange = ({ file, fileList }) => {
    // 更新文件列表状态
    setFileList(fileList)

    if (file.status === 'uploading') {
      setUploading(true)
    }
    if (file.status === 'done' || file.status === 'error') {
      setUploading(false)
    }
  }

  const uploadProps = {
    accept: '.xml',
    multiple: true,
    showUploadList: false,
    beforeUpload: () => true,
    customRequest: handleUpload,
    fileList: fileList,
    onChange: handleChange,
    maxCount: 20,
  }

  const renderDragOverlay = () => {
    if (!activeItem) return null

    return (
      <div ref={dragOverlayRef} style={{ width: '100%', maxWidth: '100%' }}>
        <List.Item
          style={{
            boxShadow: '0 4px 12px rgba(0, 0, 0, 0.15)',
            opacity: 0.9,
          }}
        >
          {/* 保留你原有的列表项渲染逻辑 */}
          <div className="w-full flex items-center justify-between">
            <div>
              <MyIcon icon="drag" size={24} />
            </div>
            <div className="w-full flex items-center justify-start gap-3">
              <InputNumber value={activeItem.episodeIndex} />
              <Input
                style={{
                  width: '100%',
                }}
                value={activeItem.title}
              />
              <div>
                <CloseCircleOutlined />
              </div>
            </div>
          </div>
        </List.Item>
      </div>
    )
  }

  return (
    <Modal
      title={`批量导入 - ${sourceInfo?.animeName} (${sourceInfo?.providerName})`}
      open={open}
      onOk={handleOk}
      onCancel={onCancel}
      confirmLoading={loading}
      destroyOnHidden
    >
      <div className="p-4 text-center">
        <Upload {...uploadProps} ref={uploadRef}>
          <div>
            <CloudUploadOutlined style={{ fontSize: 48, marginBottom: 16 }} />
            <div className="flex items-center justify-center">
              <Button
                type="primary"
                icon={<UploadOutlined />}
                loading={uploading}
                disabled={uploading}
              >
                选择文件
              </Button>
              <div className="ml-2">或直接拖拽文件到此处</div>
            </div>
            <div className="mt-2">
              支持批量上传，仅接受.xml格式文件，单次最多20个
            </div>
          </div>
        </Upload>
      </div>
      {xmlDataList.length === 0 ? (
        <div className="text-center py-10 text-gray-500">
          <Empty description="暂无解析数据，请上传XML文件" />
        </div>
      ) : (
        <>
          <Tooltip title="以第一个文件集为准依次自增1">
            <Button
              onClick={() => {
                setXmlDataList(list => {
                  const index = list[0]?.episodeIndex
                  return list.map((it, i) => {
                    return {
                      ...it,
                      episodeIndex: index + i,
                    }
                  })
                })
              }}
            >
              一键应用集数
            </Button>
          </Tooltip>
          <DndContext
            sensors={sensors}
            collisionDetection={closestCorners}
            onDragStart={handleDragStart}
            onDragEnd={handleDragEnd}
          >
            <SortableContext
              items={xmlDataList.map(item => item.id)}
              strategy={verticalListSortingStrategy}
            >
              <List
                itemLayout="vertical"
                size="large"
                pagination={{
                  pageSize: pageSize,
                  onShowSizeChange: (_, size) => {
                    setPageSize(size)
                  },
                  hideOnSinglePage: true,
                }}
                dataSource={xmlDataList}
                renderItem={(item, index) => (
                  <SortableItem
                    key={item.id}
                    item={item}
                    index={index}
                    handleDelete={() => handleDelete(item)}
                    handleEditTitle={value => handleEditTitle(item, value)}
                    handleEditIndex={value => handleEditIndex(item, value)}
                  />
                )}
              />
            </SortableContext>

            {/* 拖拽覆盖层 */}
            <DragOverlay>{renderDragOverlay()}</DragOverlay>
          </DndContext>
        </>
      )}
    </Modal>
  )
}

const SortableItem = ({
  item,
  index,
  handleDelete,
  handleEditTitle,
  handleEditIndex,
}) => {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({
    id: item.id,
    data: {
      item,
      index,
    },
  })

  const inputRef = useRef(null)
  const [isFocused, setIsFocused] = useState(false)

  const inputNumberRef = useRef(null)
  const [isNumberFocused, setIsNumberFocused] = useState(false)

  useEffect(() => {
    if (isFocused && inputRef.current) {
      inputRef.current.focus()
    }
  }, [isFocused, item.title])

  useEffect(() => {
    if (isNumberFocused && inputNumberRef.current) {
      inputNumberRef.current.focus()
    }
  }, [isNumberFocused, item.episodeIndex])

  // 拖拽样式
  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : 1,
    cursor: 'grab',
    touchAction: 'none', // 关键：阻止浏览器默认触摸行为
    userSelect: 'none', // 防止拖拽时选中文本
    ...(isDragging && { cursor: 'grabbing' }),
  }

  return (
    <List.Item ref={setNodeRef} style={style} {...attributes}>
      {/* 保留你原有的列表项渲染逻辑 */}
      <div className="w-full flex items-center justify-between">
        <div {...listeners} style={{ cursor: 'grab' }}>
          <MyIcon icon="drag" size={24} />
        </div>
        <div className="w-full flex items-center justify-start gap-3">
          <InputNumber
            ref={inputNumberRef}
            value={item.episodeIndex}
            onChange={value => {
              handleEditIndex(value)
            }}
            onFocus={() => setIsNumberFocused(true)}
            onBlur={() => setIsNumberFocused(false)}
          />
          <Input
            ref={inputRef}
            style={{
              width: '100%',
            }}
            key={item.title}
            value={item.title}
            onChange={e => {
              console.log(e.target.value, 'e.target.value')
              handleEditTitle(e.target.value)
            }}
            onFocus={() => setIsFocused(true)}
            onBlur={() => setIsFocused(false)}
          />
          <div onClick={() => handleDelete(item)}>
            <CloseCircleOutlined />
          </div>
        </div>
      </div>
    </List.Item>
  )
}
