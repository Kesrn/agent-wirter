import { mkdir, rm, writeFile } from 'node:fs/promises'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { deflateSync } from 'node:zlib'

const execFileAsync = promisify(execFile)
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const assetsDir = path.join(root, 'build-assets')
const iconsetDir = path.join(assetsDir, 'app-icon.iconset')
const iconSvg = path.join(assetsDir, 'app-icon.svg')
const iconPng = path.join(assetsDir, 'app-icon.png')
const iconIco = path.join(assetsDir, 'app-icon.ico')
const dmgSvg = path.join(assetsDir, 'dmg-background.svg')
const dmgPng = path.join(assetsDir, 'dmg-background.png')

const iconSvgSource = `<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
  <defs>
    <radialGradient id="aura" cx="29%" cy="18%" r="88%">
      <stop offset="0" stop-color="#eef2ff"/><stop offset="0.18" stop-color="#8dd3ff"/><stop offset="0.42" stop-color="#3159d9"/><stop offset="0.72" stop-color="#102033"/><stop offset="1" stop-color="#07111f"/>
    </radialGradient>
    <linearGradient id="paper" x1="274" y1="154" x2="725" y2="860" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#fffdf5"/><stop offset="1" stop-color="#cad8ef"/></linearGradient>
    <linearGradient id="gold" x1="566" y1="224" x2="720" y2="744" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#fff7c2"/><stop offset="0.36" stop-color="#f4c95d"/><stop offset="1" stop-color="#6b3f12"/></linearGradient>
  </defs>
  <rect x="64" y="64" width="896" height="896" rx="210" fill="url(#aura)"/>
  <rect x="82" y="82" width="860" height="860" rx="194" fill="none" stroke="rgba(255,255,255,0.38)" stroke-width="34"/>
  <path d="M193 667c88-33 158-88 210-165 78-116 163-178 255-187 82-8 154 24 215 95" fill="none" stroke="#67e8f9" stroke-width="38" stroke-linecap="round" opacity="0.13"/>
  <rect x="230" y="144" width="498" height="737" rx="78" fill="url(#paper)"/>
  <path d="M338 282h224M338 358h254M338 434h173M338 650h250M338 726h198" stroke="#35528f" stroke-width="24" stroke-linecap="round" opacity="0.42"/>
  <path d="M618 185c77 52 119 108 126 168 8 68-31 130-105 188l-173 135-83 95-42-29 52-116 171-137c51-42 74-82 68-120-5-35-30-70-76-106l42-78z" fill="url(#gold)"/>
  <path d="M384 746l82-70 40 33-105 56-60 94 24-111z" fill="#153b76"/>
</svg>`

const dmgSvgSource = `<svg xmlns="http://www.w3.org/2000/svg" width="660" height="420" viewBox="0 0 660 420">
  <defs>
    <radialGradient id="bg" cx="24%" cy="8%" r="92%"><stop offset="0" stop-color="#dde9ff"/><stop offset="0.2" stop-color="#3159d9"/><stop offset="0.62" stop-color="#172033"/><stop offset="1" stop-color="#07111f"/></radialGradient>
    <linearGradient id="gold" x1="440" y1="74" x2="532" y2="310" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#fff7c2"/><stop offset="0.45" stop-color="#f4c95d"/><stop offset="1" stop-color="#8a541c"/></linearGradient>
    <linearGradient id="paper" x1="374" y1="55" x2="512" y2="333" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#fffdf5"/><stop offset="1" stop-color="#cad8ef"/></linearGradient>
  </defs>
  <rect width="660" height="420" rx="28" fill="url(#bg)"/>
  <path d="M32 325c72-31 128-79 168-144 56-91 122-137 198-139 78-2 148 43 211 136" fill="none" stroke="#67e8f9" stroke-width="32" stroke-linecap="round" opacity="0.12"/>
  <text x="52" y="86" fill="#f8fafc" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif" font-size="30" font-weight="700">AI 创作平台</text>
  <text x="52" y="124" fill="#cbd5e1" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif" font-size="15">完整桌面工作台，内置后端与本地数据</text>
  <text x="52" y="342" fill="#e2e8f0" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif" font-size="14">拖动到 Applications 后启动；服务与 SQLite 数据库会自动就绪。</text>
  <g transform="translate(392 58)">
    <rect width="150" height="250" rx="28" fill="#020617" opacity="0.25"/>
    <rect x="-12" y="-10" width="150" height="250" rx="28" fill="url(#paper)"/>
    <path d="M28 58h76M28 88h92M28 118h58M28 190h88" stroke="#35528f" stroke-width="11" stroke-linecap="round" opacity="0.46"/>
    <path d="M110 20c39 31 57 63 53 96-4 36-32 69-84 99l-45 55-27-19 28-70 72-56c28-22 39-43 32-64-4-13-14-26-30-39z" fill="url(#gold)"/>
  </g>
</svg>`

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

function crc32(buffer) {
  let crc = 0xffffffff
  for (const byte of buffer) {
    crc ^= byte
    for (let bit = 0; bit < 8; bit += 1) {
      crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1))
    }
  }
  return (crc ^ 0xffffffff) >>> 0
}

function pngChunk(type, data) {
  const typeBuffer = Buffer.from(type)
  const length = Buffer.alloc(4)
  length.writeUInt32BE(data.length)
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(Buffer.concat([typeBuffer, data])))
  return Buffer.concat([length, typeBuffer, data, crc])
}

function makeIconPng(size) {
  const rows = []
  for (let y = 0; y < size; y += 1) {
    const row = Buffer.alloc(1 + size * 4)
    row[0] = 0
    for (let x = 0; x < size; x += 1) {
      const i = 1 + x * 4
      const nx = x / (size - 1)
      const ny = y / (size - 1)
      const inset = size * 0.06
      const radius = size * 0.2
      const inside =
        x >= inset &&
        y >= inset &&
        x <= size - inset &&
        y <= size - inset &&
        (x > inset + radius || y > inset + radius || (x - inset - radius) ** 2 + (y - inset - radius) ** 2 <= radius ** 2) &&
        (x < size - inset - radius || y > inset + radius || (x - size + inset + radius) ** 2 + (y - inset - radius) ** 2 <= radius ** 2) &&
        (x > inset + radius || y < size - inset - radius || (x - inset - radius) ** 2 + (y - size + inset + radius) ** 2 <= radius ** 2) &&
        (x < size - inset - radius || y < size - inset - radius || (x - size + inset + radius) ** 2 + (y - size + inset + radius) ** 2 <= radius ** 2)

      let r = 0
      let g = 0
      let b = 0
      let a = inside ? 255 : 0
      if (inside) {
        r = Math.round(8 + 42 * (1 - ny) + 28 * (1 - nx))
        g = Math.round(17 + 72 * (1 - ny) + 105 * nx)
        b = Math.round(31 + 145 * (1 - ny))
        const paper = x > size * 0.23 && x < size * 0.71 && y > size * 0.14 && y < size * 0.86
        if (paper) {
          r = 236
          g = 242
          b = 250
        }
        const pen = Math.abs((x - size * 0.36) - (size * 0.92 - y) * 0.58) < size * 0.035 && x > size * 0.36 && x < size * 0.72 && y > size * 0.2 && y < size * 0.78
        if (pen) {
          r = 232
          g = 180
          b = 65
        }
      }
      row[i] = r
      row[i + 1] = g
      row[i + 2] = b
      row[i + 3] = a
    }
    rows.push(row)
  }

  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(size, 0)
  ihdr.writeUInt32BE(size, 4)
  ihdr[8] = 8
  ihdr[9] = 6
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    pngChunk('IHDR', ihdr),
    pngChunk('IDAT', deflateSync(Buffer.concat(rows))),
    pngChunk('IEND', Buffer.alloc(0)),
  ])
}

function makeIco(png) {
  const header = Buffer.alloc(6)
  header.writeUInt16LE(0, 0)
  header.writeUInt16LE(1, 2)
  header.writeUInt16LE(1, 4)
  const entry = Buffer.alloc(16)
  entry[0] = 0
  entry[1] = 0
  entry[2] = 0
  entry[3] = 0
  entry.writeUInt16LE(1, 4)
  entry.writeUInt16LE(32, 6)
  entry.writeUInt32LE(png.length, 8)
  entry.writeUInt32LE(22, 12)
  return Buffer.concat([header, entry, png])
}

async function renderSvg(svgPath, pngPath, size) {
  await execFileAsync('qlmanage', ['-t', '-s', String(size), '-o', path.dirname(pngPath), svgPath])
  const generatedPath = path.join(path.dirname(pngPath), `${path.basename(svgPath)}.png`)
  await execFileAsync('mv', [generatedPath, pngPath])
}

async function renderSvgWithSips(svgPath, pngPath) {
  await execFileAsync('sips', ['-s', 'format', 'png', svgPath, '--out', pngPath])
}

await mkdir(assetsDir, { recursive: true })
await writeFile(iconSvg, iconSvgSource)
await writeFile(dmgSvg, dmgSvgSource)
await writeFile(iconPng, makeIconPng(1024))
await writeFile(iconIco, makeIco(makeIconPng(256)))

if (process.platform === 'darwin') {
  await rm(iconsetDir, { recursive: true, force: true })
  await mkdir(iconsetDir, { recursive: true })
  await renderSvg(iconSvg, iconPng, 1024)
  for (const entry of macIconEntries) {
    const renderedSize = entry.size * entry.scale
    const scaleSuffix = entry.scale === 2 ? '@2x' : ''
    await renderSvg(iconSvg, path.join(iconsetDir, `icon_${entry.size}x${entry.size}${scaleSuffix}.png`), renderedSize)
  }
  await execFileAsync('iconutil', ['-c', 'icns', iconsetDir, '-o', path.join(assetsDir, 'app-icon.icns')])
  await renderSvgWithSips(dmgSvg, dmgPng)
}
