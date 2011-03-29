from ptrace.os_tools import HAS_PROC
if HAS_PROC:
     from ptrace.linux_proc import openProc, ProcError
from ptrace.debugger.process_error import ProcessError
from ptrace.ctypes_tools import formatAddress
import re
from weakref import ref
import ctypes, struct, mmap, model

PROC_MAP_REGEX = re.compile(
    # Address range: '08048000-080b0000 '
    r'([0-9a-f]+)-([0-9a-f]+) '
    # Permission: 'r-xp '
    r'(.{4}) '
    # Offset: '0804d000'
    r'([0-9a-f]+) '
    # Device (major:minor): 'fe:01 '
    r'([0-9a-f]{2}):([0-9a-f]{2}) '
    # Inode: '3334030'
    r'([0-9]+)'
    # Filename: '  /usr/bin/synergyc'
    r'(?: +(.*))?')

class MemoryMapping:
    """
    Process memory mapping (metadata about the mapping).

    Attributes:
     - start (int): first byte address
     - end (int): last byte address + 1
     - permissions (str)
     - offset (int): for file, offset in bytes from the file start
     - major_device / minor_device (int): major / minor device number
     - inode (int)
     - pathname (str)
     - _process: weak reference to the process

    Operations:
     - "address in mapping" checks the address is in the mapping.
     - "search(somestring)" returns the offsets of "somestring" in the mapping
     - "mmap" mmap the MemoryMap to local address space
     - "readWord()": read a memory word, from local mmap-ed memory if mmap-ed
     - "readBytes()": read some bytes, from local mmap-ed memory if mmap-ed
     - "readStruct()": read a structure, from local mmap-ed memory if mmap-ed
     - "readArray()": read an array, from local mmap-ed memory if mmap-ed
     - "readCString()": read a C string, from local mmap-ed memory if mmap-ed
     - "str(mapping)" create one string describing the mapping
     - "repr(mapping)" create a string representation of the mapping,
       useful in list contexts
    """
    def __init__(self, process, start, end, permissions, offset, major_device, minor_device, inode, pathname):
        self._process = ref(process)
        self.start = start
        self.end = end
        self.permissions = permissions
        self.offset = offset
        self.major_device = major_device
        self.minor_device = minor_device
        self.inode = inode
        self.pathname = pathname
        self.local_mmap = None

    def __contains__(self, address):
        return self.start <= address < self.end

    def __str__(self):
        text = "%s-%s" % (formatAddress(self.start), formatAddress(self.end))
        if self.pathname:
            text += " => %s" % self.pathname
        text += " (%s)" % self.permissions
        return text
    __repr__ = __str__

    def search(self, bytestr):
        process = self._process()

        bytestr_len = len(bytestr)
        buf_len = 64 * 1024 

        if buf_len < bytestr_len:
            buf_len = bytestr_len

        remaining = self.end - self.start
        covered = self.start

        while remaining >= bytestr_len:
            if remaining > buf_len:
                requested = buf_len
            else:
                requested = remaining

            data = self.readBytes(covered, requested)

            if data == "":
                break

            offset = data.find(bytestr)
            if (offset == -1):
                skip = requested - bytestr_len + 1
            else:
                yield (covered + offset)
                skip = offset + bytestr_len

            covered += skip
            remaining -= skip

    def mmap(self):
      ''' mmap-ed access gives a 20% perf increase on by tests '''
      self.local_mmap = self._process().readArray(self.start, ctypes.c_ubyte, self.end-self.start)
      return self.local_mmap

    def readWord(self, address):
        """Address have to be aligned!"""
        if self.local_mmap : # WORD is type long
            laddr = ctypes.addressof(self.local_mmap) + address-self.start
            word = ctypes.c_ulong.from_address(laddr).value # is non-aligned a pb ?
        else:
            word = self._process().readWord(address)
        return word

    def readBytes(self, address, size):
        if self.local_mmap :
            laddr = address-self.start
            data = b''.join([ struct.pack('B',x) for x in self.local_mmap[laddr:laddr+size] ])
        else:
            data = self._process().readBytes(address, size)
        return data

    def readStruct(self, address, struct):
        if self.local_mmap :
            laddr = ctypes.addressof(self.local_mmap) + address-self.start
            struct = struct.from_address(laddr)
        else:
            struct = self._process().readStruct(address, struct)
        return struct

    def readArray(self, address, basetype, count):
        if self.local_mmap :
            laddr = ctypes.addressof(self.local_mmap) + address-self.start
            array = (basetype *count).from_address(laddr)
        else:
            array = self._process().readArray(address, basetype, count)
        return array

    def readCString(self, address, max_size, chunk_length=256):
        ''' identic to process.readCString '''
        string = []
        size = 0
        truncated = False
        while True:
            done = False
            data = self.readBytes(address, chunk_length)
            if '\0' in data:
                done = True
                data = data[:data.index('\0')]
            if max_size <= size+chunk_length:
                data = data[:(max_size-size)]
                string.append(data)
                truncated = True
                break
            string.append(data)
            if done:
                break
            size += chunk_length
            address += chunk_length
        return ''.join(string), truncated


class MemoryDumpMemoryMapping(MemoryMapping):
    """ A memoryMapping wrapper around a memory file dump"""
    def __init__(self, memdump, start, end):
        self._process = None
        self.start = start
        self.end = end
        self.permissions = 'rwx-'
        self.offset = 0x0
        self.major_device = 0x0
        self.minor_device = 0x0
        self.inode = 0x0
        self.pathname = 'MEMORYDUMP'
        self.local_mmap = mmap.mmap(memdump.fileno(), end-start, access=mmap.ACCESS_READ)

    def search(self, bytestr):
        self.local_mmap.find(bytestr)

    def readWord(self, address):
        """Address have to be aligned!"""
        laddr = address-self.start
        word = ctypes.c_ulong.from_buffer_copy(self.local_mmap, laddr).value # is non-aligned a pb ?
        return word

    def readBytes(self, address, size):
        laddr = address-self.start
        data = self.local_mmap[laddr:laddr+size]
        return data

    def readStruct(self, address, structType):
        laddr = address-self.start
        structLen = ctypes.sizeof(structType)
        st = self.local_mmap[laddr:laddr+structLen]
        structtmp = model.bytes2array(st, ctypes.c_ubyte)
        struct = structType.from_buffer(structtmp)
        return struct

    def readArray(self, address, basetype, count):
        laddr = address-self.start
        array = (basetype *count).from_buffer_copy(self.local_mmap, laddr)
        return array

    def __str__(self):
        text = "0x%lx-%s" % (self.start, formatAddress(self.end))
        text += " => %s" % self.pathname
        text += " (%s)" % self.permissions
        return text


def readProcessMappings(process):
    """
    Read all memory mappings of the specified process.

    Return a list of MemoryMapping objects, or empty list if it's not possible
    to read the mappings.

    May raise a ProcessError.
    """
    maps = []
    if not HAS_PROC:
        return maps
    try:
        mapsfile = openProc("%s/maps" % process.pid)
    except ProcError, err:
        raise ProcessError(process, "Unable to read process maps: %s" % err)
    try:
        for line in mapsfile:
            line = line.rstrip()
            match = PROC_MAP_REGEX.match(line)
            if not match:
                raise ProcessError(process, "Unable to parse memoy mapping: %r" % line)
            map = MemoryMapping(
                process,
                int(match.group(1), 16),
                int(match.group(2), 16),
                match.group(3),
                int(match.group(4), 16),
                int(match.group(5), 16),
                int(match.group(6), 16),
                int(match.group(7)),
                match.group(8))
            maps.append(map)
    finally:
        mapsfile.close()
    return maps

