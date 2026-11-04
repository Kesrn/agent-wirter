import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const execFileAsync = promisify(execFile)
const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const requestedTarget = process.argv[2] ?? 'current'
const targetPlatform = requestedTarget === 'current' ? process.platform : requestedTarget

const targets = {
  darwin: { builderFlag: '--mac', label: 'macOS DMG/ZIP' },
  win32: { builderFlag: '--win', label: 'Windows NSIS EXE' },
  linux: { builderFlag: '--linux', label: 'Linux AppImage' },
}

if (!targets[targetPlatform]) {
  console.error(`Unsupported desktop target: ${requestedTarget}`)
  process.exit(1)
}

if (targetPlatform !== process.platform) {
  console.error(
    [
      `Cannot build ${targets[targetPlatform].label} on ${process.platform}.`,
      'The desktop package embeds a PyInstaller backend binary, and PyInstaller must run on the target OS.',
      targetPlatform === 'win32'
        ? 'Run `npm run desktop:build:win` on a Windows machine to produce a usable .exe.'
        : `Run the matching build command on ${targetPlatform}.`,
    ].join('\n'),
  )
  process.exit(1)
}

const npmCommand = process.platform === 'win32' ? 'npm.cmd' : 'npm'
const electronBuilder = path.join(
  frontendRoot,
  'node_modules',
  '.bin',
  process.platform === 'win32' ? 'electron-builder.cmd' : 'electron-builder',
)

async function run(command, args) {
  const child = execFile(command, args, {
    cwd: frontendRoot,
    maxBuffer: 1024 * 1024 * 32,
  })
  child.stdout?.pipe(process.stdout)
  child.stderr?.pipe(process.stderr)
  await new Promise((resolve, reject) => {
    child.on('error', reject)
    child.on('exit', (code) => {
      if (code === 0) resolve()
      else reject(new Error(`${command} ${args.join(' ')} exited with code ${code}`))
    })
  })
}

async function verifyEmbeddedBackend() {
  // 打包产物 FTS5 自动化验收（与 .github/workflows/build-windows-desktop.yml 的
  // verify-fts5 步骤同源）：检索索引依赖 SQLite FTS5，PyInstaller 捆绑的运行时
  // 必须实际执行建表 + 中文插入/检索/bm25 排序断言。desktop_server.py 的
  // --verify-fts5 内联了与 verify_fts5.py 等价的全套断言，退出码非 0 即中止构建。
  const exe = path.join(
    frontendRoot,
    'backend-dist',
    process.platform === 'win32' ? 'ai-creative-backend.exe' : 'ai-creative-backend',
  )
  console.log(`[verify-fts5] ${exe} --verify-fts5`)
  try {
    const { stdout, stderr } = await execFileAsync(exe, ['--verify-fts5'], {
      maxBuffer: 1024 * 1024 * 4,
    })
    if (stdout) process.stdout.write(stdout)
    if (stderr) process.stderr.write(stderr)
    console.log('[verify-fts5] PASS')
  } catch (error) {
    console.error('[verify-fts5] FAIL:', error.message || error)
    throw new Error('embedded backend FTS5 verification failed')
  }
}

async function main() {
  await run(npmCommand, ['run', 'assets:desktop'])
  await run(npmCommand, ['run', 'backend:build'])
  await verifyEmbeddedBackend()
  await run(npmCommand, ['run', 'build:desktop'])
  await execFileAsync(electronBuilder, [targets[targetPlatform].builderFlag], {
    cwd: frontendRoot,
    maxBuffer: 1024 * 1024 * 32,
  }).then(({ stdout, stderr }) => {
    if (stdout) process.stdout.write(stdout)
    if (stderr) process.stderr.write(stderr)
  })
}

main().catch((error) => {
  console.error(error.message || error)
  process.exit(1)
})
