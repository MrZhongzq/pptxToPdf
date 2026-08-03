import { useEffect, useState } from 'react'

import { useI18n } from '../i18n'
import { getMe, type UserDto } from '../lib/api'
import { GraphCredentialsPanel } from './admin/GraphCredentialsPanel'
import { BlockedPanel } from './admin/BlockedPanel'
import { FontsPanel } from './admin/FontsPanel'
import { OriginsPanel } from './admin/OriginsPanel'
import { StatsPanel } from './admin/StatsPanel'
import { UsersPanel } from './admin/UsersPanel'

type Section = 'users' | 'graph' | 'fonts' | 'origins' | 'blocked' | 'stats'

const SECTIONS: { key: Section; labelKey: string; hintKey: string }[] = [
  { key: 'users', labelKey: 'admin.section.users', hintKey: 'admin.section.users.hint' },
  { key: 'graph', labelKey: 'admin.section.graph', hintKey: 'admin.section.graph.hint' },
  { key: 'fonts', labelKey: 'admin.section.fonts', hintKey: 'admin.section.fonts.hint' },
  { key: 'origins', labelKey: 'admin.section.origins', hintKey: 'admin.section.origins.hint' },
  { key: 'blocked', labelKey: 'admin.section.blocked', hintKey: 'admin.section.blocked.hint' },
  { key: 'stats', labelKey: 'admin.section.stats', hintKey: 'admin.section.stats.hint' },
]

/**
 * Admin 面板，侧边栏分区。
 *
 * 未登录或非 admin 一律跳回主页。但**跳转只是体验**：真正的边界在后端，
 * 每个 /api/admin/* 端点都挂了 require_admin。绕过前端直接打 API 是最
 * 基本的渗透手法，只做前端等于没做。
 */
export function AdminPage() {
  const { t } = useI18n()
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
        <p style={{ color: 'var(--c-text-dim)' }}>{t('admin.checking')}</p>
      </div>
    )
  }

  return (
    <div className="layout-wide">
      <header className="page-head" style={{ position: 'relative' }}>
        <div>
          <h1 className="page-title">{t('admin.title')}</h1>
          <span className="page-sub">{user.username}</span>
        </div>
        <div style={{ position: 'absolute', top: 0, right: 0 }}>
          <a className="btn btn-ghost" href="/">
            {t('nav.backToUpload')}
          </a>
        </div>
      </header>

      <div className="admin-shell">
        <nav aria-label={t('admin.nav.label')} className="admin-nav">
          {SECTIONS.map((s) => (
            <button
              key={s.key}
              type="button"
              className="admin-nav-item"
              aria-current={s.key === section ? 'page' : undefined}
              onClick={() => setSection(s.key)}
            >
              <span className="admin-nav-label">{t(s.labelKey)}</span>
              <span className="admin-nav-hint">{t(s.hintKey)}</span>
            </button>
          ))}
        </nav>

        <div style={{ display: 'grid', gap: 'var(--space-4)' }}>
          {section === 'users' && <UsersPanel currentUserId={user.user_id} />}
          {section === 'graph' && <GraphCredentialsPanel />}
          {section === 'fonts' && <FontsPanel />}
          {section === 'origins' && <OriginsPanel />}
          {section === 'blocked' && <BlockedPanel />}
          {section === 'stats' && <StatsPanel />}
        </div>
      </div>
    </div>
  )
}
