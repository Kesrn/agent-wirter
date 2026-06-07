import { mkdir, rm } from 'node:fs/promises'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const execFileAsync = promisify(execFile)
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const assetsDir = path.join(root, 'build-assets')
const iconsetDir = path.join(assetsDir, 'app-icon.iconset')
const macIconEntries = [
  { size: 16, scale: 1 },
  { size: 16, scale: 2 },
  { size: 32, scale: 1 },
  { size: 32, scale: 2 },
  { size: 128, scale: 1 },
  { size: 128, scale: 2 },
  { size: 256, scale: 1 },
  { size: 256, scale: 2 },
  { size: 512, scale: 1 },
  { size: 512, scale: 2 },
]

async function renderSvg(svgPath, pngPath, size) {
  await execFileAsync('qlmanage', ['-t', '-s', String(size), '-o', path.dirname(pngPath), svgPath])
  const generatedPath = path.join(path.dirname(pngPath), `${path.basename(svgPath)}.png`)
  await execFileAsync('mv', [generatedPath, pngPath])
}

async function renderSvgWithSips(svgPath, pngPath) {
  await execFileAsync('sips', ['-s', 'format', 'png', svgPath, '--out', pngPath])
}

await mkdir(assetsDir, { recursive: true })
await rm(iconsetDir, { recursive: true, force: true })
await mkdir(iconsetDir, { recursive: true })

const iconSvg = path.join(assetsDir, 'app-icon.svg')
const iconPng = path.join(assetsDir, 'app-icon.png')
await renderSvg(iconSvg, iconPng, 1024)

for (const entry of macIconEntries) {
  const renderedSize = entry.size * entry.scale
  const scaleSuffix = entry.scale === 2 ? '@2x' : ''
  await renderSvg(iconSvg, path.join(iconsetDir, `icon_${entry.size}x${entry.size}${scaleSuffix}.png`), renderedSize)
}

await execFileAsync('iconutil', ['-c', 'icns', iconsetDir, '-o', path.join(assetsDir, 'app-icon.icns')])

const dmgSvg = path.join(assetsDir, 'dmg-background.svg')
await renderSvgWithSips(dmgSvg, path.join(assetsDir, 'dmg-background.png'))
