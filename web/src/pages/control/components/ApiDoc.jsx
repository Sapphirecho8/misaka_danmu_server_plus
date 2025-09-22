import { Button, Card } from 'antd'

export const ApiDoc = () => {
  return (
    <div className="my-6">
      <Card
        title="API文档"
        extra={
          <Button
            onClick={() => {
              window.open('/api/control/docs', '_blank')
            }}
          >
            文档链接
          </Button>
        }
      >
        <div className="w-full">
          <iframe
            className="w-full"
            style={{
              height: `calc(100vh - 300px)`,
            }}
            src="/api/control/docs"
          ></iframe>
        </div>
      </Card>
    </div>
  )
}
