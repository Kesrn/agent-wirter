import { access, mkdir } from 'node:fs/promises'
import { constants } from 'node:fs'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const execFileAsync = promisify(execFile)
const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const projectRoot = path.resolve(frontendRoot, '..')
const backendRoot = path.join(projectRoot, 'backend')
const pythonPath = process.platform === 'win32'
  ? path.join(backendRoot, 'venv', 'Scripts', 'python.exe')
  : path.join(backendRoot, 'venv', 'bin', 'python')

async function ensurePyInstaller() {
  try {
    await execFileAsync(pythonPath, ['-m', 'PyInstaller', '--version'])
  } catch {
    await execFileAsync(pythonPath, ['-m', 'pip', 'install', 'pyinstaller>=6.0.0'], {
      cwd: backendRoot,
      stdio: 'inherit',
    })
  }
}

async function main() {
  await access(pythonPath, constants.X_OK)
  await ensurePyInstaller()
  await mkdir(path.join(frontendRoot, 'backend-dist'), { recursive: true })

  const separator = process.platform === 'win32' ? ';' : ':'
  const args = [
    '-m',
    'PyInstaller',
    '--clean',
    '--noconfirm',
    '--onefile',
    '--name',
    'ai-creative-backend',
    '--distpath',
    path.join(frontendRoot, 'backend-dist'),
    '--workpath',
    path.join(frontendRoot, '.pyinstaller-build'),
    '--specpath',
    path.join(frontendRoot, '.pyinstaller-spec'),
    '--add-data',
    `${path.join(backendRoot, 'skills')}${separator}skills`,
    '--hidden-import',
    'aiosqlite',
    '--hidden-import',
    'sqlalchemy.dialects.sqlite.aiosqlite',
    path.join(backendRoot, 'desktop_server.py'),
  ]

  const { stdout, stderr } = await execFileAsync(pythonPath, args, {
    cwd: backendRoot,
    maxBuffer: 1024 * 1024 * 16,
  })
  if (stdout) process.stdout.write(stdout)
  if (stderr) process.stderr.write(stderr)
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})
