import { useEffect, useState } from 'react'

import { getMe, type UserDto } from '../lib/api'
import { GraphCredentialsPanel } from './admin/GraphCredentialsPanel'
import { OriginsPanel } from './admin/OriginsPanel'
import { StatsPanel } from './admin/StatsPanel'
import { UsersPanel } from './admin/UsersPanel'

type Section = 'users' | 'graph' | 'origins' | 'stats'

const SECTIONS: { key: Section; label: string; hint: string }[] = [
  { key: 'users', label: '用户管理', hint: '添加 / 暂停 / 删除账号' },
  { key: 'graph', label: 'Azure 凭证', hint: 'Graph 通道与连通性自检' },
  { key: 'origins', label: '访问白名单', hint: '防跨站（当前未启用）' },
  { key: 'stats', label: '系统状态', hint: '任务与存储占用' },
]

/**
 * Admin 面板，侧边栏分区。
 *
 * 未登录或非 admin 一律跳回主页。但**跳转只是体验**：真正的边界在后端，
 * 每个 /api/admin/* 端点都挂了 require_admin。绕过前端直接打 API 是最
 * 基本的渗透手法，只做前端等于没做。
 */
export function AdminPage() {
  const [checking, setChecking] = useState(true)
  const [user, setUser] = useState<UserDto | null>(null)
  const [section, setSection] = useState<Section>('users')

  useEffect(() => {
    let alive = true
    getMe()
      .then((u) => {
        if (!alive) return
        if (u?.role === 'admin') {
          setUser(u)
          setChecking(false)
        } else {
          // replace 而不是 href：不留历史记录，用户按后退不会又弹回
          // /admin 再跳一次
          window.location.replace('/')
        }
      })
      .catch(() => window.location.replace('/'))
    return () => {
      alive = false
    }
  }, [])

  if (checking || !user) {
    return (
      <div className="layout-wide">
        <p style={{ color: 'var(--c-text-muted)' }}>正在核验身份…</p>
      </div>
    )
  }

  return (
    <div className="layout-wide">
      <header className="page-head" style={{ position: 'relative' }}>
        <div>
          <h1 className="page-title">管理面板</h1>
          <span className="page-sub">{user.username}</span>
        </div>
        <div style={{ position: 'absolute', top: 0, right: 0 }}>
          <a className="btn btn-ghost" href="/">
            返回上传页
          </a>
        </div>
      </header>

      <div className="admin-shell">
        <nav aria-label="面板分区" className="admin-nav">
          {SECTIONS.map((s) => (
            <button
              key={s.key}
              type="button"
              className="admin-nav-item"
              aria-current={s.key === section ? 'page' : undefined}
              onClick={() => setSection(s.key)}
            >
              <span className="admin-nav-label">{s.label}</span>
              <span className="admin-nav-hint">{s.hint}</span>
            </button>
          ))}
        </nav>

        <div style={{ display: 'grid', gap: 'var(--space-4)' }}>
          {section === 'users' && <UsersPanel currentUserId={user.user_id} />}
          {section === 'graph' && <GraphCredentialsPanel />}
          {section === 'origins' && <OriginsPanel />}
          {section === 'stats' && <StatsPanel />}
        </div>
      </div>
    </div>
  )
}
