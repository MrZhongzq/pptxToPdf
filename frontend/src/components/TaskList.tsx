import { TaskCard } from './TaskCard'

export function TaskList({ taskIds }: { taskIds: string[] }) {
  if (taskIds.length === 0) {
    return (
      <div
        className="card"
        style={{
          padding: 'var(--space-6) var(--space-4)',
          textAlign: 'center',
          color: 'var(--c-text-dim)',
          fontSize: 14,
          borderStyle: 'dashed',
        }}
      >
        还没有任务，上传一个 pptx 开始
      </div>
    )
  }
  return (
    <div style={{ display: 'grid', gap: 'var(--space-3)' }}>
      {taskIds.map((id) => (
        <TaskCard key={id} taskId={id} />
      ))}
    </div>
  )
}
