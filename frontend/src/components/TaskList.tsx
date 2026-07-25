import { TaskCard } from './TaskCard'

export function TaskList({ taskIds }: { taskIds: string[] }) {
  if (taskIds.length === 0) {
    return (
      <p style={{ color: 'var(--g-text-dim)' }}>还没有任务。上传一个 pptx 开始。</p>
    )
  }
  return (
    <div style={{ display: 'grid', gap: 12 }}>
      {taskIds.map((id) => (
        <TaskCard key={id} taskId={id} />
      ))}
    </div>
  )
}
