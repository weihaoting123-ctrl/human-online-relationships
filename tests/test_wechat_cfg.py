import hashlib
import hmac
import struct
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import read_wechat_cfg as cfg
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad


class CfgTests(unittest.TestCase):
    def test_key_must_authenticate_database_page(self):
        master=b'm'*32
        page=bytearray(b'p'*4096)
        raw=hashlib.pbkdf2_hmac('sha512',master,page[:16],256000,32)
        key=hashlib.pbkdf2_hmac('sha512',raw,bytes(x^0x3a for x in page[:16]),2,32)
        page[4032:]=hmac.new(key,bytes(page[16:4032])+struct.pack('<I',1),hashlib.sha512).digest()
        self.assertTrue(cfg.validates_page(master,bytes(page)))
        self.assertFalse(cfg.validates_page(b'x'*32,bytes(page)))
        page[150]^=1
        self.assertFalse(cfg.validates_page(master,bytes(page)))

    def test_v2_strict_decryption(self):
        dword=254
        wxid='wxid_fixture'
        plaintext=b'fixture image bytes'
        middle=b'middle'
        tail=b'tail'
        key=hashlib.md5((str(dword)+wxid).encode()).hexdigest()[:16].encode()
        encrypted=AES.new(key,AES.MODE_ECB).encrypt(pad(plaintext,16))
        data=cfg.V2_MAGIC+struct.pack('<II',len(plaintext),len(tail))+b'\x01'+encrypted+middle+bytes(x^254 for x in tail)
        self.assertEqual(cfg.decode_v2_image(data,dword,wxid),plaintext+middle+tail)
        with self.assertRaises(ValueError): cfg.decode_v2_image(data,4,wxid)
        with self.assertRaises(ValueError): cfg.decode_v2_image(data[:30],dword,wxid)

    def test_v1_public_constant_with_verified_tail_xor(self):
        plaintext=b'old format fixture'
        key=hashlib.md5(b'0').hexdigest()[:16].encode('ascii')
        encrypted=AES.new(key,AES.MODE_ECB).encrypt(pad(plaintext,16))
        data=cfg.V1_MAGIC+struct.pack('<II',len(plaintext),2)+b'\x01'+encrypted+bytes(x^45 for x in b'end'[-2:])
        self.assertEqual(cfg.decode_v2_image(data,45,'fixture'),plaintext+b'nd')

    def test_mask_requires_pe_unique_pattern_and_exact_instruction_sequence(self):
        image=bytearray(128)
        image[:2]=b'MZ'
        struct.pack_into('<I',image,0x3c,80)
        image[80:84]=b'PE\0\0'
        image+=cfg.PATTERN
        expected=b''
        for i,separator in enumerate(cfg.SEPARATORS):
            part=bytes([i+1])*8
            expected+=part
            image+=part+separator
        image+=b'z'*8
        expected+=b'z'*8
        self.assertEqual(cfg.xor_material(image),expected)
        self.assertIsNone(cfg.xor_material(image+cfg.PATTERN))
        image[-9]^=1
        self.assertIsNone(cfg.xor_material(image))

    def test_remote_string_has_hard_bounds(self):
        calls=[]
        def read(addr,n):
            calls.append((addr,n))
            return b'x'*16+struct.pack('<QQ',2**30,2**30)
        self.assertEqual(cfg.read_string(read,0x10000),b'')
        self.assertEqual(len(calls),1)


if __name__=='__main__': unittest.main()
