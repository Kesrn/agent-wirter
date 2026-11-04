import { describe, it, expect } from 'vitest'
import settingsSource from './Settings.vue?raw'
import projectListSource from './ProjectList.vue?raw'
import workspaceSource from './Workspace.vue?raw'

describe('unified logout integration across views', () => {
  it('Settings.vue calls performLogout on handleLogout without clearAuthSession', () => {
    expect(settingsSource).toContain("import { performLogout } from '../utils/authSession'")
    expect(settingsSource).not.toContain("import { clearAuthSession } from '../utils/authSession'")

    const handleLogoutMatch = settingsSource.match(/async function handleLogout\(\)\s*\{([\s\S]*?)\}/)
    expect(handleLogoutMatch).not.toBeNull()
    expect(handleLogoutMatch?.[1]).toContain('await performLogout()')
  })

  it('ProjectList.vue calls performLogout on handleLogout without clearAuthSession', () => {
    expect(projectListSource).toContain("import { performLogout } from '../utils/authSession'")
    expect(projectListSource).not.toContain("import { clearAuthSession } from '../utils/authSession'")

    const handleLogoutMatch = projectListSource.match(/async function handleLogout\(\)\s*\{([\s\S]*?)\}/)
    expect(handleLogoutMatch).not.toBeNull()
    expect(handleLogoutMatch?.[1]).toContain('await performLogout()')
  })

  it('Workspace.vue calls performLogout inside confirmLeaveIfGenerating', () => {
    expect(workspaceSource).toContain('performLogout')
    expect(workspaceSource).not.toContain("import { clearAuthSession")

    const handleLogoutMatch = workspaceSource.match(/function handleLogout\(\)\s*\{([\s\S]*?confirmLeaveIfGenerating\([\s\S]*?\)\s*)\}/)
    expect(handleLogoutMatch).not.toBeNull()
    expect(handleLogoutMatch?.[1]).toContain('confirmLeaveIfGenerating')
    expect(handleLogoutMatch?.[1]).toContain('await performLogout()')
  })
})
