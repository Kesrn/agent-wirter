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

async function main() {
  await run(npmCommand, ['run', 'assets:desktop'])
  await run(npmCommand, ['run', 'backend:build'])
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
