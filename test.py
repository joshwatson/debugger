#!/usr/bin/env python3
#
# unit tests for debugger

import os
import sys
import time
import platform
import threading

from struct import unpack

sys.path.append('..')
import debugger.lldb as lldb
import debugger.dbgeng as dbgeng
import debugger.DebugAdapter as DebugAdapter
import debugger.utils as utils

# globals
adapter = None

#--------------------------------------------------------------------------
# UTILITIES
#--------------------------------------------------------------------------

def parse_image(fpath):
	print('finding entrypoint for %s' % fpath)
	with open(fpath, 'rb') as fp:
		data = fp.read()

	# little endian macho
	if data[0:4] == b'\xCF\xFA\xED\xFE':
		assert data[4:8] == b'\x07\x00\x00\x01' # CPU_TYPE_X86_X64
		ncmds = unpack('<I', data[16:20])[0]
		#print('ncmds: %d' % ncmds)
		vmaddr = None
		entryoff1 = None # offset given by COMMAND entry_point_command (priority)
		entryoff2 = None # offset of __text section inside __TEXT segment
		offs = 0x20
		for i in range(ncmds):
			cmd = unpack('<I', data[offs:offs+4])[0]
			cmdsize = unpack('<I', data[offs+4:offs+8])[0]
			if cmd == 0x19: # segment_command_64
				if data[offs+8:offs+16] == b'\x5F\x5F\x54\x45\x58\x54\x00\x00': # __TEXT
					vmaddr = unpack('<Q', data[offs+24:offs+32])[0]
					print('vmaddr: %X' % vmaddr)

					nsects = unpack('<I', data[offs+64:offs+68])[0]
					#print('segment __TEXT nsects: %d' % nsects)

					# advance past command to first section
					o_scn = offs + 0x48
					for i in range(nsects):
						name = data[o_scn+0:o_scn+16]
						#print('__TEXT section %d: %s' % (i, name))
						if name == b'__text\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00':
							entryoff2 = unpack('<I', data[o_scn+0x30:o_scn+0x34])[0]
							break;
						o_scn += 0x50

					if entryoff2 == None:
						raise Exception('couldn\'t locate section __text in segment __TEXT in %s' % fpath)
			if cmd == 0x80000028: # entry_point_command
				entryoff = unpack('<I', data[offs+8:offs+12])[0]
				#print('entryoff: %X' % entryoff)
			offs += cmdsize
		if not vmaddr:
			raise Exception('couldn\'t locate segment_command_64 (where __TEXT loads) in %s' % fpath)
		if entryoff1 == None and entryoff2 == None:
			raise Exception('couldn\'t locate entry_point_command in macho (where main is)' % fpath)

		entryoff = entryoff1 or entryoff2
		return (vmaddr, entryoff)

	# PE
	if data[0:2] == b'\x4d\x5a':
		e_lfanew = unpack('<I', data[0x3C:0x40])[0]
		assert data[e_lfanew:e_lfanew+6] == b'\x50\x45\x00\x00\x64\x86'
		entryoff = unpack('<I', data[e_lfanew+0x28:e_lfanew+0x2C])[0]
		vmaddr = unpack('<Q', data[e_lfanew+0x30:e_lfanew+0x38])[0]
		return (vmaddr, entryoff)

	# ELF
	if data[0:4] == b'\x7FELF':
		assert data[4] == 2 # EI_CLASS 64-bit
		assert data[5] == 1 # EI_DATA little endian

		assert data[0x10:0x12] in [b'\x02\x00', b'\x03\x00'] # e_type ET_EXEC or ET_DYN (pie)
		assert data[0x12:0x14] == b'\x3E\x00' # e_machine EM_X86_64
		e_entry = unpack('<Q', data[0x18:0x20])[0]
		e_phoff = unpack('<Q', data[0x20:0x28])[0]
		e_phentsize = unpack('<H', data[0x36:0x38])[0]
		e_phnum = unpack('<H', data[0x38:0x3a])[0]
		print('e_entry:0x%X e_phoff:0x%X e_phentsize:0x%X e_phnum:0x%X' %
			(e_entry, e_phoff, e_phentsize, e_phnum))

		# find first PT_LOAD
		p_vaddr = None
		offs = e_phoff
		for i in range(e_phnum):
			p_type = unpack('<I', data[offs:offs+4])[0]
			#print('at offset 0x%X p_type:0x%X' % (offs, p_type))
			if p_type == 1:
				p_vaddr = unpack('<Q', data[offs+16:offs+24])[0]
				break
			offs += e_phentsize

		if p_vaddr == None:
			raise Exception('couldnt locate a single PT_LOAD program header')

		return (p_vaddr, e_entry-p_vaddr)

	raise Exception('unrecognized file type')

# 'helloworld' -> '.\testbins\helloworld.exe'
# or
# 'helloworld' -> './testbins/helloworld
def test_prog_to_fpath(prog):
	if platform.system() == 'Windows':
		prog = prog + '.exe'
	tmp =  os.path.join('testbins', prog)
	if '~' in tmp:
		tmp = os.expanduser(tmp)
	tmp = os.path.abspath(tmp)
	return tmp

def break_into(adapter):
	print('sending break')
	adapter.break_into()

def assert_general_error(func):
	raised = False
	try:
		func()
	except DebugAdapter.GeneralError:
		raised = True
	assert raised

def test_prologue(prog, testtype):
	fpath = test_prog_to_fpath(prog)

	print('----------------------------------------------------------------')
	print('%s test on %s' % (testtype.upper(), fpath))
	print('----------------------------------------------------------------')

	(load_addr, entry_offs) = parse_image(fpath)
	entry = load_addr + entry_offs
	print('(file) load addr: 0x%X' % load_addr)
	print('(file) entry offset: 0x%X' % entry_offs)

	print('launching')
	adapter = DebugAdapter.get_adapter_for_current_system()
	adapter.exec(fpath, '')

	# learn load address, entrypoint
	#
	module2addr = adapter.mem_modules()
	if not fpath in module2addr:
		print('module2addr: ', module2addr)
		print('fpath: ', fpath)
		assert fpath in module2addr

	if '_pie' in prog:
		load_addr = module2addr[fpath]
		print('pie module, file load 0x%X overridden with 0x%X, new entry 0x%X' %
			(load_addr, module2addr[fpath], module2addr[fpath]+entry_offs))
		entry = load_addr + entry_offs
	else:
		print('non-pie module should hold file\'s specified load and entry')
		print('load_addr: 0x%X' % load_addr)
		print('module2addr[fpath]: 0x%X' % module2addr[fpath])
		assert module2addr[fpath] == load_addr

	return (adapter, entry)

#------------------------------------------------------------------------------
# MAIN
#------------------------------------------------------------------------------

if __name__ == '__main__':
	arg = sys.argv[1] if sys.argv[1:] else None

	# one-off tests
	if arg == 'oneoff':
		fpath = test_prog_to_fpath('helloworld_thread')
		adapter = DebugAdapter.get_adapter_for_current_system()
		adapter.exec(fpath)
		print(adapter.mem_modules())
		print(type(adapter) == dbgeng.DebugAdapterDbgeng)
		sys.exit(0)

	tests = []
	if arg in ['asm', 'assembly', 'assembler', 'asmtest']:
		tests = ['assembly']
	elif arg in ['thread', 'threads', 'threading']:
		tests = ['thread']
	elif arg in ['basic']:
		tests = ['basic']
	else:
		tests = ['assembly', 'thread', 'basic']

	if 'assembly' in tests:
		(adapter, entry) = test_prologue('asmtest', 'ASSEMBLY')

		loader = adapter.reg_read('rip') != entry
		if loader:
			print('entrypoint is the program, no library or loader')
		else:
			print('loader detected, gonna step a few times for fun')

		# a few steps in the loader
		if loader:
			assert adapter.step_into()[0] == DebugAdapter.STOP_REASON.SIGNAL_TRAP
			assert adapter.step_into()[0] == DebugAdapter.STOP_REASON.SIGNAL_TRAP
			assert adapter.step_into()[0] == DebugAdapter.STOP_REASON.SIGNAL_TRAP
			assert adapter.step_into()[0] == DebugAdapter.STOP_REASON.SIGNAL_TRAP

		# set bp entry
		print('setting entry breakpoint at 0x%X' % entry)
		adapter.breakpoint_set(entry)

		# few more steps
		if loader:
			assert adapter.step_into()[0] == DebugAdapter.STOP_REASON.SIGNAL_TRAP
			assert adapter.step_into()[0] == DebugAdapter.STOP_REASON.SIGNAL_TRAP
			assert adapter.step_into()[0] == DebugAdapter.STOP_REASON.SIGNAL_TRAP
			assert adapter.step_into()[0] == DebugAdapter.STOP_REASON.SIGNAL_TRAP

		# go to entry
		adapter.go()
		assert adapter.reg_read('rip') == entry
		adapter.breakpoint_clear(entry)
		# step into nop
		adapter.step_into()
		assert adapter.reg_read('rip') == entry+1
		# step into call, return
		adapter.step_into()
		adapter.step_into()
		# back
		assert adapter.reg_read('rip') == entry+6
		adapter.step_into()
		# step into call, return
		adapter.step_into()
		adapter.step_into()
		# back
		assert adapter.reg_read('rip') == entry+12

		adapter.quit()

	if 'basic' in tests:
		for prog in ['helloworld', 'helloworld_loop', 'helloworld_thread',
			'helloworld_func', 'helloworld_pie', 'helloworld_thread_pie',
			'helloworld_loop_pie', 'helloworld_func_pie',
			]:

			(adapter, entry) = test_prologue(prog, 'BASIC')

			print('rip: 0x%X' % adapter.reg_read('rip'))

			# breakpoint set/clear should fail at 0
			print('breakpoint failures')
			try:
				adapter.breakpoint_clear(0)
			except DebugAdapter.BreakpointClearError:
				pass

			try:
				adapter.breakpoint_set(0)
			except DebugAdapter.BreakpointSetError:
				pass

			# breakpoint set/clear should succeed at entrypoint
			print('setting breakpoint at 0x%X' % entry)
			adapter.breakpoint_set(entry)
			print('clearing breakpoint at 0x%X' % entry)
			adapter.breakpoint_clear(entry)
			print('setting breakpoint at 0x%X' % entry)
			adapter.breakpoint_set(entry)

			# proceed to breakpoint
			print('going')
			(reason, info) = adapter.go()
			assert reason == DebugAdapter.STOP_REASON.SIGNAL_TRAP
			rip = adapter.reg_read('rip')
			print('rip: 0x%X' % rip)
			assert rip == entry

			# single step
			data = adapter.mem_read(rip, 15)
			assert len(data) == 15
			(asmstr, asmlen) = utils.disasm1(data, 0)
			adapter.breakpoint_clear(entry)
			(reason, info) = adapter.step_into()
			assert reason == DebugAdapter.STOP_REASON.SIGNAL_TRAP
			rip2 = adapter.reg_read('rip')
			print('rip2: 0x%X' % rip2)
			assert rip + asmlen == rip2

			print('registers')
			for (ridx,rname) in enumerate(adapter.reg_list()):
				width = adapter.reg_bits(rname)
				#print('%d: %s (%d bits)' % (ridx, rname, width))
			assert adapter.reg_bits('rax') == 64
			assert adapter.reg_bits('rbx') == 64
			assert_general_error(lambda: adapter.reg_bits('rzx'))

			print('registers read/write')
			rax = adapter.reg_read('rax')
			rbx = adapter.reg_read('rbx')
			assert_general_error(lambda: adapter.reg_read('rzx'))
			adapter.reg_write('rax', 0xDEADBEEFAAAAAAAA)
			assert adapter.reg_read('rax') == 0xDEADBEEFAAAAAAAA
			adapter.reg_write('rbx', 0xCAFEBABEBBBBBBBB)
			assert_general_error(lambda: adapter.reg_read('rzx'))
			assert adapter.reg_read('rbx') == 0xCAFEBABEBBBBBBBB
			adapter.reg_write('rax', rax)
			assert adapter.reg_read('rax') == rax
			adapter.reg_write('rbx', rbx)
			assert adapter.reg_read('rbx') == rbx

			print('mem read/write')
			addr = adapter.reg_read('rip')
			data = adapter.mem_read(addr, 256)
			assert_general_error(lambda: adapter.mem_write(0, b'heheHAHAherherHARHAR'))
			data2 = b'\xAA' * 256
			adapter.mem_write(addr, data2)
			assert_general_error(lambda: adapter.mem_read(0, 256))
			assert adapter.mem_read(addr, 256) == data2
			adapter.mem_write(addr, data)
			assert adapter.mem_read(addr, 256) == data

			print('quiting')
			adapter.quit()
			adapter = None

	if 'thread' in tests:
		(adapter, entry) = test_prologue('helloworld_thread', 'THREAD')

		print('scheduling break in .5 seconds')
		threading.Timer(.5, break_into, [adapter]).start()
		print('going')
		adapter.go()
		print('back')
		print('switching to bad thread')
		assert_general_error(lambda: adapter.thread_select(999))
		print('asking for threads')
		if platform.system() == 'Windows':
			# main thread at WaitForMultipleObjects() + 4 created threads + debugger thread
			nthreads_expected = 6
		else:
			# main thread at pthread_join() + 4 created threads
			nthreads_expected = 5
		tids = adapter.thread_list()
		assert len(tids) == nthreads_expected
		tid_active = adapter.thread_selected()
		rips = []
		for tid in tids:
			adapter.thread_select(tid)
			rip = adapter.reg_read('rip')
			rips.append(rip)
			seltxt = '<--' if tid == tid_active else ''
			print('thread %02d: rip=0x%016X %s' % (tid, rip, seltxt))
		assert rips[0] != rips[1] # thread at WaitForMultipleObjects()/pthread_join() should be different
		print('switching to bad thread')
		assert_general_error(lambda: adapter.thread_select(999))
		secs = .5
		print('scheduling break in %d second(s)' % secs)
		threading.Timer(secs, break_into, [adapter]).start()
		print('going')
		adapter.go()
		print('back')
		print('checking for %d threads' % nthreads_expected)
		assert len(adapter.thread_list()) == nthreads_expected
		# ensure the rip's are in different locations (that the continue actually continued)
		rips2 = []
		for tid in tids:
			adapter.thread_select(tid)
			rip = adapter.reg_read('rip')
			rips2.append(rip)
		print('checking that at least one thread progressed')
		#print(' rips: ', rips)
		#print('rips2: ', rips2)
		if list(filter(lambda x: not x, [rips[i]==rips2[i] for i in range(len(rips))])) == []:
			print('did any threads progress?')
			print('rips:   ', rips)
			print('rips2:  ', rips2)
			assert False
		print('done')
		adapter.quit()

	print('TESTS PASSED!')
