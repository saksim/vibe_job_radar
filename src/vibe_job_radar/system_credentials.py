"""OS-backed workspace encryption; no backend discovery or plaintext fallback."""
from __future__ import annotations
import base64
import hashlib
import importlib.metadata
import importlib.util
import json
import logging
import multiprocessing
import os
from pathlib import Path
import sys
import threading
import time

SERVICE = 'org.vibe-job-radar.workspace-key.v1'
SCHEME = 'system-keyring-aesgcm-v1'
PROVIDERS = {'linux': 'secret-service', 'darwin': 'macos-keychain'}
MESSAGES = {
    'credential_store_unavailable': '系统密钥库不可用。Linux 需要 Secret Service 默认密钥库与 secure-store 组件；macOS 需要默认钥匙串。原配置保留。',
    'credential_store_locked': '系统密钥库已锁定或未授权。请在系统密钥库中解锁或授权此程序后重试；不会自动弹窗、匿名连接或改写为明文。',
    'credential_store_missing': '此工作区的加密密钥不存在。请使用原系统账号和原工作区位置，或重新输入凭据；不会自动重建读取密钥。',
    'credential_store_invalid': '加密凭据或密钥无法验证。请核对原工作区和系统账号后重新配置；原文件保留。',
    'credential_store_timeout': '系统密钥库未在限定时间内响应，请检查系统密钥库后重试。原配置保留。',
    'credential_store_busy': '系统密钥库正在处理其他操作，请稍后重试。原配置保留。',
}
_SLOTS = threading.BoundedSemaphore(2)
_TIMEOUT = 10.0


class CredentialStoreError(ValueError):
    def __init__(self, code):
        self.code = code if code in MESSAGES else 'credential_store_invalid'
        super().__init__(self.code)


def provider():
    return PROVIDERS.get(sys.platform, '')


def available():
    # Capability only: status never accesses, creates, unlocks or prompts.
    try:
        if not provider() or importlib.util.find_spec('cryptography') is None:
            return False
        return sys.platform == 'darwin' or (
            importlib.metadata.version('SecretStorage') == '3.5.0'
            and importlib.util.find_spec('secretstorage') is not None)
    except (ImportError, OSError, ValueError):
        return False


def _account(root):
    return hashlib.sha256(os.path.normcase(str(Path(root).resolve())).encode('utf-8')).hexdigest()


def _linux_key(account, create):
    import secretstorage
    from secretstorage.collection import Collection
    from secretstorage.exceptions import LockedException, ItemNotFoundException
    from secretstorage.item import Item
    from secretstorage.util import open_session, format_secret, DBusAddressWrapper
    connection = secretstorage.dbus_init()
    try:
        try:
            collection = Collection(connection)  # Never create a default/session collection.
            collection.ensure_not_locked()
            session = open_session(connection)
            if not session.encrypted:
                raise CredentialStoreError('credential_store_unavailable')
            collection.session = session
            attributes = {'application': SERVICE, 'workspace': account}
            from itertools import islice
            matches = list(islice(collection.search_items(attributes), 2))
            if len(matches) > 1:
                raise CredentialStoreError('credential_store_invalid')
            if matches:
                return matches[0].get_secret()
            if not create:
                raise CredentialStoreError('credential_store_missing')
            # Pinned SecretStorage 3.5 API; never execute an interactive Prompt.
            secret = os.urandom(32)
            props = {'org.freedesktop.Secret.Item.Label': ('s', 'Vibe Job Radar workspace key'),
                     'org.freedesktop.Secret.Item.Attributes': ('a{ss}', attributes)}
            path, prompt = collection._collection.call(
                'CreateItem', 'a{sv}(oayays)b', props,
                format_secret(session, secret, 'application/octet-stream'), False)
            if prompt != '/':
                DBusAddressWrapper(prompt, 'org.freedesktop.Secret.Prompt', connection).call('Dismiss', '')
                raise CredentialStoreError('credential_store_locked')
            if path == '/':
                raise CredentialStoreError('credential_store_invalid')
            saved = Item(connection, path, session).get_secret()
            if saved != secret:
                raise CredentialStoreError('credential_store_invalid')
            return saved
        except LockedException:
            raise CredentialStoreError('credential_store_locked') from None
        except ItemNotFoundException:
            raise CredentialStoreError('credential_store_unavailable') from None
    finally:
        connection.close()


def _mac_key(account, create):
    """Security.framework, restricted to the existing default keychain."""
    import ctypes as c
    P, I, U = c.c_void_p, c.c_int32, c.c_uint32
    cf = c.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    sec = c.CDLL('/System/Library/Frameworks/Security.framework/Security')
    def function(lib, name, result, *args):
        fn = getattr(lib, name); fn.restype = result; fn.argtypes = list(args)
        return fn
    release = function(cf, 'CFRelease', None, P)
    string = function(cf, 'CFStringCreateWithCString', P, P, c.c_char_p, U)
    data = function(cf, 'CFDataCreate', P, P, P, c.c_long)
    length = function(cf, 'CFDataGetLength', c.c_long, P)
    bytes_ptr = function(cf, 'CFDataGetBytePtr', P, P)
    type_id = function(cf, 'CFGetTypeID', c.c_ulong, P)
    data_id = function(cf, 'CFDataGetTypeID', c.c_ulong)
    array = function(cf, 'CFArrayCreate', P, P, c.POINTER(P), c.c_long, P)
    dictionary = function(cf, 'CFDictionaryCreate', P, P, c.POINTER(P), c.POINTER(P), c.c_long, P, P)
    copy_default = function(sec, 'SecKeychainCopyDefault', I, c.POINTER(P))
    status = function(sec, 'SecKeychainGetStatus', I, P, c.POINTER(U))
    no_ui = function(sec, 'SecKeychainSetUserInteractionAllowed', I, c.c_ubyte)
    find = function(sec, 'SecItemCopyMatching', I, P, c.POINTER(P))
    add = function(sec, 'SecItemAdd', I, P, c.POINTER(P))
    owned = []
    def own(value):
        if not value: raise CredentialStoreError('credential_store_unavailable')
        owned.append(value); return value
    def const(lib, name):
        return P.in_dll(lib, name).value
    def callbacks(name):
        return c.addressof((c.c_byte * 1).in_dll(cf, name))
    def cfstring(value):
        return own(string(None, value.encode('utf-8'), 0x08000100))
    def mapping(pairs):
        keys = (P * len(pairs))(*(const(sec, k) for k, _ in pairs))
        vals = (P * len(pairs))(*(v for _, v in pairs))
        return own(dictionary(None, keys, vals, len(pairs),
            callbacks('kCFTypeDictionaryKeyCallBacks'), callbacks('kCFTypeDictionaryValueCallBacks')))
    def checked(code):
        if code in (-25308, -25293, -128):
            raise CredentialStoreError('credential_store_locked')
        if code != 0: raise CredentialStoreError('credential_store_unavailable')
    keychain = P()
    try:
        checked(no_ui(0))
        checked(copy_default(c.byref(keychain))); own(keychain.value)
        flags = U(); checked(status(keychain, c.byref(flags)))
        if not flags.value & 1: raise CredentialStoreError('credential_store_locked')
        search = own(array(None, (P * 1)(keychain.value), 1, callbacks('kCFTypeArrayCallBacks')))
        attrs = [('kSecClass', const(sec, 'kSecClassGenericPassword')),
                 ('kSecAttrService', cfstring(SERVICE)), ('kSecAttrAccount', cfstring(account))]
        query = mapping(attrs + [('kSecMatchSearchList', search),
            ('kSecReturnData', const(cf, 'kCFBooleanTrue')),
            ('kSecMatchLimit', const(sec, 'kSecMatchLimitOne'))])
        result = P(); code = find(query, c.byref(result))
        if code == -25300:
            if not create: raise CredentialStoreError('credential_store_missing')
            secret = os.urandom(32)
            buffer = c.create_string_buffer(secret)
            encoded = own(data(None, buffer, len(secret)))
            checked(add(mapping(attrs + [('kSecUseKeychain', keychain.value),
                ('kSecAttrLabel', cfstring('Vibe Job Radar workspace key')),
                ('kSecValueData', encoded)]), None))
            code = find(query, c.byref(result))
        checked(code); own(result.value)
        if type_id(result) != data_id() or length(result) != 32:
            raise CredentialStoreError('credential_store_invalid')
        return c.string_at(bytes_ptr(result), 32)
    finally:
        for obj in reversed(owned): release(obj)


def _worker(pipe, account, create):
    try:
        logging.disable(logging.CRITICAL)
        key = _linux_key(account, create) if sys.platform == 'linux' else _mac_key(account, create)
        if not isinstance(key, bytes) or len(key) != 32:
            raise CredentialStoreError('credential_store_invalid')
        pipe.send_bytes(b'K' + key)
    except CredentialStoreError as error:
        pipe.send_bytes(error.code.encode('ascii'))
    except Exception:
        # Native/library messages can contain secret material or private paths.
        pipe.send_bytes(b'credential_store_unavailable')
    finally:
        pipe.close()


def _key(root, *, create):
    if not available():
        raise CredentialStoreError('credential_store_unavailable')
    if not _SLOTS.acquire(blocking=False):
        raise CredentialStoreError('credential_store_busy')
    process = parent = child = None
    deadline = time.monotonic() + _TIMEOUT
    try:
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe(duplex=False)
        process = context.Process(target=_worker, args=(child, _account(root), create), daemon=True)
        process.start(); child.close(); child = None
        while time.monotonic() < deadline:
            if parent.poll(min(.1, max(0, deadline-time.monotonic()))):
                result = parent.recv_bytes(128)
                if len(result) == 33 and result[:1] == b'K':
                    return result[1:]
                raise CredentialStoreError(result.decode('ascii'))
            if not process.is_alive():
                raise CredentialStoreError('credential_store_unavailable')
        raise CredentialStoreError('credential_store_timeout')
    except CredentialStoreError:
        raise
    except Exception:
        raise CredentialStoreError('credential_store_unavailable') from None
    finally:
        try:
            if process is not None and process.pid is not None:
                process.join(.2)
                if process.is_alive(): process.terminate(); process.join(1)
                if process.is_alive(): process.kill(); process.join(1)
                process.close()
            if parent is not None: parent.close()
            if child is not None: child.close()
        finally:
            _SLOTS.release()


def _aad(root, purpose, context):
    return json.dumps([SCHEME, _account(root), purpose, context],
                      ensure_ascii=True, separators=(',', ':')).encode('ascii')


def seal(root, purpose, context, raw):
    """Caller holds the workspace writer lock across key creation and atomic save."""
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= 2048:
        raise CredentialStoreError('credential_store_invalid')
    key = _key(root, create=True)
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = os.urandom(12)
        return base64.b64encode(nonce + AESGCM(key).encrypt(nonce, raw, _aad(root,purpose,context))).decode('ascii')
    except Exception:
        raise CredentialStoreError('credential_store_invalid') from None


def restore(root, purpose, context, payload):
    try:
        if not isinstance(payload, str) or not 40 <= len(payload) <= 3072: raise ValueError()
        raw = base64.b64decode(payload, validate=True)
        if base64.b64encode(raw).decode('ascii') != payload or len(raw) < 29: raise ValueError()
    except Exception:
        raise CredentialStoreError('credential_store_invalid') from None
    key = _key(root, create=False)
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(key).decrypt(raw[:12], raw[12:], _aad(root,purpose,context))
    except Exception:
        raise CredentialStoreError('credential_store_invalid') from None
