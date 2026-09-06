/**
 * Minimal local bridge around the pinned weflow-cli read services.
 *
 * This process is only invoked by sync_all_wechat.py.  Account-discovery stdout
 * is captured and never forwarded verbatim.  A captured key is returned only
 * inside an ephemeral AES-256-GCM envelope; plaintext never enters stdout,
 * stderr, argv, or the filesystem.  The bridge exposes no message-reading or
 * message-sending operation.
 */

import { existsSync } from 'node:fs'
import { createCipheriv, randomBytes } from 'node:crypto'
import { isAbsolute, join, relative, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

const MARKER = '__SHE_LOVE_ME_PRIVATE_JSON__'
const KEY_WRAP_ENV = 'SHE_LOVE_ME_KEY_WRAP_KEY'
const KEY_ENVELOPE_AAD = Buffer.from('she-love-me-wechat-key-v1', 'ascii')

function emit(payload) {
  process.stdout.write(`${MARKER}${JSON.stringify(payload)}\n`)
}

function moduleUrl(packageRoot, relativePath) {
  const target = resolve(packageRoot, relativePath)
  const root = resolve(packageRoot)
  const rel = relative(root, target)
  if (!rel || rel.startsWith('..') || isAbsolute(rel)) {
    throw new Error('module_path_outside_runtime')
  }
  if (!existsSync(target)) throw new Error('runtime_module_missing')
  return pathToFileURL(target).href
}

function emitEncryptedKey(key) {
  const wrappingHex = String(process.env[KEY_WRAP_ENV] || '')
  if (!/^[0-9a-fA-F]{64}$/.test(wrappingHex)) {
    throw new Error('key_wrap_invalid')
  }
  const wrappingKey = Buffer.from(wrappingHex, 'hex')
  const nonce = randomBytes(12)
  const plaintext = Buffer.from(key, 'ascii')
  let ciphertext
  let tag
  try {
    const cipher = createCipheriv('aes-256-gcm', wrappingKey, nonce)
    cipher.setAAD(KEY_ENVELOPE_AAD)
    ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()])
    tag = cipher.getAuthTag()
    emit({
      ok: true,
      key_envelope: {
        version: 1,
        algorithm: 'A256GCM',
        nonce: nonce.toString('base64'),
        ciphertext: ciphertext.toString('base64'),
        tag: tag.toString('base64'),
      },
    })
  } finally {
    wrappingKey.fill(0)
    plaintext.fill(0)
    nonce.fill(0)
    if (ciphertext) ciphertext.fill(0)
    if (tag) tag.fill(0)
  }
}

async function discoverAccounts(packageRoot) {
  const { dbPathService } = await import(
    moduleUrl(packageRoot, 'dist/src/core/dbPathService.js')
  )
  const { configService } = await import(
    moduleUrl(packageRoot, 'dist/src/services/configService.js')
  )

  let dbPath = String(configService.get('dbPath') || '').trim()
  if (!dbPath || !existsSync(dbPath)) {
    const detected = await dbPathService.autoDetect()
    if (!detected?.success || !detected.path) {
      emit({ ok: false, code: 'wechat_data_not_found' })
      return
    }
    dbPath = detected.path
  }

  const accounts = dbPathService.scanWxids(dbPath).map((item) => ({
    id: String(item.wxid || ''),
    nickname: String(item.nickname || ''),
    modified_time: Number(item.modifiedTime || 0),
  })).filter((item) => item.id)

  emit({
    ok: true,
    configured: Boolean(configService.isConfigured()),
    configured_account: String(configService.get('wxid') || ''),
    db_path: dbPath,
    accounts,
  })
}

async function captureDatabaseKey(packageRoot) {
  if (!/^[0-9a-fA-F]{64}$/.test(String(process.env[KEY_WRAP_ENV] || ''))) {
    emit({ ok: false, code: 'key_wrap_invalid' })
    return
  }

  const dllPath = join(packageRoot, 'resources', 'key', 'win32', 'x64', 'wx_key.dll')
  if (!existsSync(dllPath)) {
    emit({ ok: false, code: 'key_helper_missing' })
    return
  }
  process.env.WX_KEY_DLL_PATH = dllPath
  let keyService
  try {
    ({ keyService } = await import(
      moduleUrl(packageRoot, 'dist/src/core/keyService.js')
    ))
  } catch {
    emit({ ok: false, code: 'key_helper_load_failed' })
    return
  }
  const pids = process.argv.slice(4)
    .map((value) => Number.parseInt(value, 10))
    .filter((value) => Number.isInteger(value) && value > 0)
  if (!pids.length) {
    emit({ ok: false, code: 'wechat_not_running' })
    return
  }
  if (!keyService.ensureLoaded() || !keyService.ensureKernel32()) {
    emit({ ok: false, code: 'key_helper_load_failed' })
    return
  }
  const requestedTimeout = Number.parseInt(
    process.env.SHE_LOVE_ME_HOOK_TIMEOUT_MS || '15000',
    10,
  )
  const timeoutMs = Number.isInteger(requestedTimeout)
    ? Math.max(5_000, Math.min(requestedTimeout, 180_000))
    : 15_000
  let permissionDenied = false
  let hookFailed = false
  for (const pid of pids) {
    let result
    try {
      result = await keyService.extractKeyFromPid(pid, [], undefined, timeoutMs)
    } catch {
      hookFailed = true
      continue
    }
    if (result?.success && /^[0-9a-fA-F]{64}$/.test(String(result.key || ''))) {
      try {
        emitEncryptedKey(String(result.key))
      } catch {
        emit({ ok: false, code: 'key_wrap_failed' })
        return
      }
      return
    }
    const error = String(result?.error || '')
    if (error.includes('权限不足') || error.includes('ACCESS_DENIED')) {
      permissionDenied = true
    }
  }
  emit({
    ok: false,
    code: permissionDenied
      ? 'permission_required'
      : (hookFailed ? 'key_hook_failed' : 'database_key_unavailable'),
  })
}

async function main() {
  const operation = process.argv[2]
  const packageRoot = resolve(process.argv[3] || '')
  if (!operation || !packageRoot || !existsSync(join(packageRoot, 'package.json'))) {
    emit({ ok: false, code: 'invalid_bridge_arguments' })
    process.exitCode = 2
    return
  }
  if (operation === 'accounts') {
    await discoverAccounts(packageRoot)
    return
  }
  if (operation === 'capture-key') {
    await captureDatabaseKey(packageRoot)
    return
  }
  emit({ ok: false, code: 'unsupported_bridge_operation' })
  process.exitCode = 2
}

try {
  await main()
} catch {
  emit({ ok: false, code: 'bridge_error' })
  process.exitCode = 1
}
