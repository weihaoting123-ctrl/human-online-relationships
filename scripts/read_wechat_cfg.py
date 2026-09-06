"""Bounded read-only adapter for WeChat 4.1.12 local configuration.

Algorithm reference (third-party, NOT Tencent): fanyuantaier/wechatauto-replica,
Apache-2.0, commit b9a9f5619f34c6a3e6eb15c27ec73d0adbbb4386, db.py/media.py.
This independent adapter never installs/imports that package, executes its DLL,
injects code, changes process memory, controls the client, or uses the network.
Only a key that authenticates real local database pages can be cached by DPAPI.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import hmac
import json
import re
import struct
import sys
import time
from pathlib import Path

PATTERN = bytes.fromhex('83ec404889d64889cb0f57c00f1142100f11024c8bb1c80200004883b9d0020000107209488b9bb8020000eb074881c3b80200004d85f60f880a0200004983fe10736d4c89761048c746180f0000000f10030f110648b8')
SEPARATORS = tuple(bytes.fromhex(x) for x in ('488944242048b8','488944242848b8','488944243048b8'))
MAX_MODULE = 512 * 1024 * 1024
MAX_READ = 768 * 1024 * 1024
MAX_SECONDS = 45
LANDMARK = b'global_config'
V2_MAGIC = b'\x07\x08V2\x08\x07'
V1_MAGIC = b'\x07\x08V1\x08\x07'


def xor_material(image):
    if len(image) > MAX_MODULE or not image.startswith(b'MZ') or len(image) < 64:
        return None
    pe = struct.unpack_from('<I',image,0x3c)[0]
    if pe > len(image)-4 or image[pe:pe+4] != b'PE\0\0':
        return None
    if image.count(PATTERN) != 1:
        return None
    offset = image.index(PATTERN)+len(PATTERN)
    parts = []
    for separator in SEPARATORS:
        if image[offset+8:offset+15] != separator:
            return None
        parts.append(image[offset:offset+8])
        offset += 15
    parts.append(image[offset:offset+8])
    return b''.join(parts) if all(len(p)==8 for p in parts) else None


def read_string(read, address, limit=128):
    header=read(address,32)
    if len(header)!=32:
        return b''
    size,capacity=struct.unpack_from('<QQ',header,16)
    if not 0<size<=limit or size>capacity or capacity>4096:
        return b''
    if capacity<=15:
        return header[:size]
    pointer=struct.unpack_from('<Q',header)[0]
    return read(pointer,size)


def candidate_from_landmark(read,address,material,account_id):
    import sync_all_wechat as sync
    header=read(address,32)
    if len(header)!=32 or header[:13]!=LANDMARK or struct.unpack_from('<QQ',header,16)!=(13,15):
        return None
    def pointer(where):
        data=read(where,8)
        return struct.unpack('<Q',data)[0] if len(data)==8 else 0
    node=pointer(address+16-0x138)
    if not 0x10000<=node<0x800000000000:
        return None
    config=pointer(node+0x68)
    if not 0x10000<=config<0x800000000000:
        return None
    try:
        wxid=read_string(read,config+0x48).decode('utf-8')
    except UnicodeDecodeError:
        return None
    if not re.fullmatch(r'[A-Za-z0-9_@.-]{1,128}',wxid) or not sync._is_self_sender(wxid,account_id):
        return None
    cipher=read_string(read,config+0x2b8,32)
    dword=read(config+0x40,4)
    if len(cipher)!=32 or len(dword)!=4:
        return None
    return {'master':bytes(a^b for a,b in zip(cipher,material)), 'cfg_u32':struct.unpack('<I',dword)[0], 'wxid':wxid}


def validates_page(master,page):
    if len(master)!=32 or len(page)!=4096:
        return False
    salt=page[:16]
    raw=hashlib.pbkdf2_hmac('sha512',master,salt,256000,32)
    mac_key=hashlib.pbkdf2_hmac('sha512',raw,bytes(x^0x3a for x in salt),2,32)
    actual=hmac.new(mac_key,page[16:4032]+struct.pack('<I',1),hashlib.sha512).digest()
    return hmac.compare_digest(actual,page[4032:4096])


class MemoryInfo(ctypes.Structure):
    _fields_=[('BaseAddress',ctypes.c_void_p),('AllocationBase',ctypes.c_void_p),('AllocationProtect',ctypes.c_ulong),('PartitionId',ctypes.c_ushort),('RegionSize',ctypes.c_size_t),('State',ctypes.c_ulong),('Protect',ctypes.c_ulong),('Type',ctypes.c_ulong)]


def extract(account_id,pages):
    if sys.platform!='win32' or not pages:
        return None
    import pymem.process
    import sync_all_wechat as sync
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong]
    kernel.OpenProcess.restype=ctypes.c_void_p
    kernel.CloseHandle.argtypes=[ctypes.c_void_p]
    kernel.ReadProcessMemory.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.POINTER(ctypes.c_size_t)]
    kernel.ReadProcessMemory.restype=ctypes.c_int
    kernel.VirtualQueryEx.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.POINTER(MemoryInfo),ctypes.c_size_t]
    kernel.VirtualQueryEx.restype=ctypes.c_size_t
    deadline=time.monotonic()+MAX_SECONDS
    total=0
    candidates=0
    for process_id in sync._weixin_pids()[:8]:
        if time.monotonic()>deadline or total>=MAX_READ:
            break
        handle=kernel.OpenProcess(0x10|0x400,False,process_id)
        if not handle:
            continue
        try:
            module=pymem.process.module_from_name(handle,'Weixin.dll')
            if not module or not 0<module.SizeOfImage<=MAX_MODULE:
                continue
            dll_path=Path(module.filename)
            if dll_path.name.lower()!='weixin.dll' or dll_path.is_symlink() or dll_path.stat().st_size>MAX_MODULE:
                continue
            material=xor_material(dll_path.read_bytes())
            if not material:
                continue
            base=int(module.lpBaseOfDll)
            end=base+int(module.SizeOfImage)
            def read(address,count):
                nonlocal total
                if not 0x10000<=address<0x800000000000 or not 0<count<=2*1024*1024 or total+count>MAX_READ or time.monotonic()>deadline:
                    return b''
                total+=count
                buffer=ctypes.create_string_buffer(count)
                received=ctypes.c_size_t()
                if not kernel.ReadProcessMemory(handle,address,buffer,count,ctypes.byref(received)) or received.value!=count:
                    return b''
                return buffer.raw
            position=base
            while position<end and time.monotonic()<deadline and total<MAX_READ and candidates<64:
                info=MemoryInfo()
                if not kernel.VirtualQueryEx(handle,position,ctypes.byref(info),ctypes.sizeof(info)):
                    break
                region_end=min(end,int(info.BaseAddress or 0)+info.RegionSize)
                if region_end<=position:
                    break
                if info.State==0x1000 and not (info.Protect & (0x100|0x01)):
                    chunk_pos=position
                    while chunk_pos<region_end and time.monotonic()<deadline and total<MAX_READ:
                        size=min(1024*1024,region_end-chunk_pos)
                        block=read(chunk_pos,min(size+32,region_end-chunk_pos))
                        offset=block.find(LANDMARK)
                        while offset>=0 and candidates<64:
                            candidates+=1
                            candidate=candidate_from_landmark(read,chunk_pos+offset,material,account_id)
                            if candidate and all(validates_page(candidate['master'],page) for page in pages):
                                return candidate
                            offset=block.find(LANDMARK,offset+1)
                        chunk_pos+=size
                position=region_end
        except (OSError,ValueError,AttributeError):
            continue
        finally:
            kernel.CloseHandle(handle)
    return None


def decode_v2_image(data,cfg_u32,wxid):
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    if not data.startswith((V1_MAGIC,V2_MAGIC)) or not 31<=len(data)<=128*1024*1024:
        raise ValueError('invalid_v2_image')
    aes_plain,xor_size=struct.unpack_from('<II',data,6)
    aes_size=(aes_plain//16+1)*16
    if not 0<aes_plain<=128*1024*1024 or 15+aes_size+xor_size>len(data):
        raise ValueError('invalid_v2_lengths')
    if not 0<=cfg_u32<=0xffffffff:
        raise ValueError('invalid_image_key')
    key=hashlib.md5((str(cfg_u32)+wxid).encode('utf-8')).hexdigest()[:16].encode('ascii')
    if data.startswith(V1_MAGIC):
        # Public V1 format constant; documented by xinyao27/wechat-log/dat2img.
        # Tail XOR still uses the verified account configuration, never a guess.
        key=hashlib.md5(b'0').hexdigest()[:16].encode('ascii')
    plain=unpad(AES.new(key,AES.MODE_ECB).decrypt(data[15:15+aes_size]),16)
    if len(plain)!=aes_plain:
        raise ValueError('invalid_v2_plain_length')
    tail_start=len(data)-xor_size
    return plain+data[15+aes_size:tail_start]+bytes(x^(cfg_u32&255) for x in data[tail_start:])


def initialize(cache=False):
    import sync_all_wechat as sync
    account,base=sync.discover_unique_account()
    root=base if base.name==account else base/account
    if root.is_symlink() or not root.is_dir():
        raise ValueError('account_invalid')
    databases=[root/'db_storage/contact/contact.db']
    databases+=sorted((root/'db_storage/message').glob('message_*.db'))[:2]
    pages=[]
    for database in databases:
        if database.is_file() and not database.is_symlink():
            with database.open('rb') as handle:
                page=handle.read(4096)
            if len(page)==4096:
                pages.append(page)
    candidate=extract(account,pages)
    status={'candidate_found':bool(candidate),'database_validated':bool(candidate),'image_key_candidate_found':bool(candidate),'cache_initialized':False,'source_untouched':True}
    if candidate and cache:
        sync.cache_captured_passphrase(candidate['master'].hex(),expected_account_id=account)
        protected=sync._protect_secret(json.dumps({'cfg_u32':candidate['cfg_u32'],'wxid':candidate['wxid']}))
        sync._atomic_write_json(sync.PRIVATE_ROOT/'image-key-cache.json',{'version':1,'account_hash':sync._account_hash(account),'protected':protected})
        status['cache_initialized']=True
    return status


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--initialize',action='store_true')
    parser.add_argument('--probe',action='store_true')
    args=parser.parse_args(argv)
    try:
        status=initialize(cache=args.initialize)
    except Exception:
        status={'candidate_found':False,'database_validated':False,'cache_initialized':False,'error_code':'local_cfg_probe_failed','source_untouched':True}
    print(json.dumps(status))
    return 0 if status.get('database_validated') else 1


if __name__=='__main__':
    raise SystemExit(main())
