"""One-pixel synthetic PNGs for protocol tests, containing no real content."""

import base64
import struct
import zlib


def image_url(red=100):
    def chunk(name,data):
        return struct.pack('!I',len(data))+name+data+struct.pack('!I',zlib.crc32(name+data)&0xffffffff)
    png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',1,1,8,2,0,0,0))
    png+=chunk(b'IDAT',zlib.compress(bytes([0,red,100,100])))+chunk(b'IEND',b'')
    return 'data:image/png;base64,'+base64.b64encode(png).decode()
