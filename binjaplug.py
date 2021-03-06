import os
import re
import time

import binaryninja
from binaryninja import Symbol, SymbolType, Type, Structure, StructureType

from . import DebugAdapter, ProcessView

try:
	# create the widgets, debugger, etc.
	from . import ui
	ui.initialize_ui()
	have_ui = True
except (ModuleNotFoundError, ImportError) as e:
	have_ui = False
	print(e)
	print("Could not initialize UI, using headless mode only")

#------------------------------------------------------------------------------
# globals
#------------------------------------------------------------------------------

def get_state(bv):
	# Try to find an existing state object
	for state in DebuggerState.states:
		if state.bv == bv:
			return state

	# Else make a new one, initially inactive
	state = DebuggerState(bv)
	DebuggerState.states.append(state)
	return state

def delete_state(bv):
	print("Detroying debugger state for {}".format(bv))

	# Try to find an existing state object
	for (i, state) in enumerate(DebuggerState.states):
		if state.bv == bv:
			DebuggerState.states.pop(i)
			return

#------------------------------------------------------------------------------
# DEBUGGER STATE / CONTROLLER
#
# Controller of the debugger for a single BinaryView
#------------------------------------------------------------------------------

class DebuggerState:
	states = []

	def __init__(self, bv):
		self.bv = bv
		self.adapter = None
		self.state = 'INACTIVE'
		# address -> adapter id
		self.breakpoints = {}
		self.memory_view = ProcessView.DebugProcessView(bv)
		self.old_symbols = []
		self.old_dvs = set()
		self.last_rip = 0
		self.command_line_args = []

		if have_ui:
			self.ui = ui.DebuggerUI(self)
		else:
			self.ui = None

	#--------------------------------------------------------------------------
	# SUPPORT FUNCTIONS (HIGHER LEVEL)
	#--------------------------------------------------------------------------

	# Mark memory as dirty, will refresh memory view
	def memory_dirty(self):
		self.memory_view.mark_dirty()

	# Create symbols and variables for the memory view
	def update_memory_view(self):
		if self.adapter == None:
			raise Exception('missing adapter')
		if self.memory_view == None:
			raise Exception('missing memory_view')

		addr_regs = {}
		reg_addrs = {}

		for reg in self.adapter.reg_list():
			addr = self.adapter.reg_read(reg)
			reg_symbol_name = '$' + reg

			if addr not in addr_regs.keys():
				addr_regs[addr] = [reg_symbol_name]
			else:
				addr_regs[addr].append(reg_symbol_name)
			reg_addrs[reg] = addr

		for symbol in self.old_symbols:
			# Symbols are immutable so just destroy the old one
			self.memory_view.undefine_auto_symbol(symbol)

		for dv in self.old_dvs:
			self.memory_view.undefine_data_var(dv)

		self.old_symbols = []
		self.old_dvs = set()
		new_dvs = set()

		for (reg, addr) in reg_addrs.items():
			symbol_name = '$' + reg
			self.memory_view.define_auto_symbol(Symbol(SymbolType.ExternalSymbol, addr, symbol_name, namespace=symbol_name))
			self.old_symbols.append(self.memory_view.get_symbol_by_raw_name(symbol_name, namespace=symbol_name))
			new_dvs.add(addr)

		for new_dv in new_dvs:
			self.memory_view.define_data_var(new_dv, Type.int(8))
			self.old_dvs.add(new_dv)

		# Special struct for stack frame
		if self.bv.arch.name == 'x86_64':
			width = reg_addrs['rbp'] - reg_addrs['rsp'] + self.bv.arch.address_size
			if width > 0:
				if width > 0x1000:
					width = 0x1000
				struct = Structure()
				struct.type = StructureType.StructStructureType
				struct.width = width
				for i in range(0, width, self.bv.arch.address_size):
					var_name = "var_{:x}".format(width - i)
					struct.insert(i, Type.pointer(self.bv.arch, Type.void()), var_name)
				self.memory_view.define_data_var(reg_addrs['rsp'], Type.structure_type(struct))
				self.memory_view.define_auto_symbol(Symbol(SymbolType.ExternalSymbol, reg_addrs['rsp'], "$stack_frame", raw_name="$stack_frame"))

				self.old_symbols.append(self.memory_view.get_symbol_by_raw_name("$stack_frame"))
				self.old_dvs.add(reg_addrs['rsp'])
		else:
			raise NotImplementedError('only x86_64 so far')

	# Highlight lines
	def update_highlights(self):
		# Clear old highlighted rip
		for func in self.bv.get_functions_containing(self.last_rip):
			func.set_auto_instr_highlight(self.last_rip, binaryninja.HighlightStandardColor.NoHighlightColor)

		for bp in self.breakpoints:
			for func in self.bv.get_functions_containing(bp):
				func.set_auto_instr_highlight(bp, binaryninja.HighlightStandardColor.RedHighlightColor)

		if self.adapter is not None:
			if self.bv.arch.name == 'x86_64':
				remote_rip = self.adapter.reg_read('rip')
				local_rip = self.memory_view.remote_addr_to_local(remote_rip)
			else:
				raise NotImplementedError('only x86_64 so far')

			for func in self.bv.get_functions_containing(local_rip):
				func.set_auto_instr_highlight(local_rip, binaryninja.HighlightStandardColor.BlueHighlightColor)

	def breakpoint_tag_add(self, local_address):
		# create tag
		tt = self.bv.tag_types["Crashes"]
		for func in self.bv.get_functions_containing(local_address):
			tags = [tag for tag in func.get_address_tags_at(local_address) if tag.data == 'breakpoint']
			if len(tags) == 0:
				tag = func.create_user_address_tag(local_address, tt, "breakpoint")

	# breakpoint TAG removal - strictly presentation
	# (doesn't remove actual breakpoints, just removes the binja tags that mark them)
	#
	def breakpoint_tag_del(self, local_addresses=None):
		if local_addresses == None:
			local_addresses = [self.memory_view.local_addr_to_remote(addr) for addr in self.breakpoints]

		for local_address in local_addresses:
			# delete breakpoint tags from all functions containing this address
			for func in self.bv.get_functions_containing(local_address):
				func.set_auto_instr_highlight(local_address, binaryninja.HighlightStandardColor.NoHighlightColor)
				delqueue = [tag for tag in func.get_address_tags_at(local_address) if tag.data == 'breakpoint']
				for tag in delqueue:
					func.remove_user_address_tag(local_address, tag)

	def on_stdout(self, output):
		# TODO: Send to debugger console when stdin is working
		print(output)

	#--------------------------------------------------------------------------
	# DEBUGGER FUNCTIONS (MEDIUM LEVEL, BLOCKING)
	#--------------------------------------------------------------------------

	def run(self):
		fpath = self.bv.file.original_filename

		if not os.path.exists(fpath):
			raise Exception('cannot find debug target: ' + fpath)

		self.adapter = DebugAdapter.get_adapter_for_current_system(stdout=self.on_stdout)
		self.adapter.exec(fpath, self.command_line_args)

		self.memory_view.update_base()

		if self.bv and self.bv.entry_point:
			local_entry = self.bv.entry_point
			remote_entry = self.memory_view.local_addr_to_remote(local_entry)
			self.breakpoint_set(remote_entry)
		self.memory_dirty()

	def quit(self):
		if self.adapter is not None:
			try:
				self.adapter.quit()
			except BrokenPipeError:
				pass
			except ConnectionResetError:
				pass
			except OSError:
				pass
			finally:
				self.adapter = None
		self.memory_dirty()

	def restart(self):
		self.quit()
		time.sleep(1)
		self.run() # sets state

	def detach(self):
		if self.adapter is not None:
			try:
				self.adapter.detach()
			except BrokenPipeError:
				pass
			except ConnectionResetError:
				pass
			except OSError:
				pass
			finally:
				self.adapter = None
		self.memory_dirty()

	def pause(self):
		if not self.adapter:
			raise Exception('missing adapter')
		result = self.adapter.break_into()
		self.memory_dirty()
		return result

	def go(self):
		if not self.adapter:
			raise Exception('missing adapter')

		remote_rip = self.adapter.reg_read('rip')
		local_rip = self.memory_view.remote_addr_to_local(remote_rip)
		bphere = local_rip in self.breakpoints

		seq = []
		if bphere:
			seq.append((self.adapter.breakpoint_clear, (remote_rip,)))
			seq.append((self.adapter.go, ()))
			seq.append((self.adapter.breakpoint_set, (remote_rip,)))
		else:
			seq.append((self.adapter.go, ()))

		result = self.exec_adapter_sequence(seq)
		self.memory_dirty()
		return result

	def step_into(self):
		if not self.adapter:
			raise Exception('missing adapter')

		(reason, data) = (None, None)

		if self.bv.arch.name == 'x86_64':
			remote_rip = self.adapter.reg_read('rip')
			local_rip = self.memory_view.remote_addr_to_local(remote_rip)

			# if currently a breakpoint at rip, temporarily clear it
			# TODO: detect windbg adapter because dbgeng's step behavior might ignore breakpoint
			# at current rip

			seq = []
			if local_rip in self.breakpoints:
				seq.append((self.adapter.breakpoint_clear, (remote_rip,)))
				seq.append((self.adapter.step_into, ()))
				seq.append((self.adapter.breakpoint_set, (remote_rip,)))
			else:
				seq.append((self.adapter.step_into, ()))
			# TODO: Cancel (and raise some exception)
			result = self.exec_adapter_sequence(seq)
			self.memory_dirty()
			return result
		else:
			raise NotImplementedError('step unimplemented for architecture %s' % self.bv.arch.name)

	def step_over(self):
		if not self.adapter:
			raise Exception('missing adapter')

		try:
			return self.adapter.step_over()
		except NotImplementedError:
			pass

		if self.bv.arch.name == 'x86_64':
			remote_rip = self.adapter.reg_read('rip')
			local_rip = self.memory_view.remote_addr_to_local(remote_rip)

			if self.bv.read(local_rip, 1):
				instxt = self.bv.get_disassembly(local_rip)
				inslen = self.bv.get_instruction_length(local_rip)
			else:
				data = self.adapter.mem_read(remote_rip, 16)
				(tokens, length) = self.bv.arch.get_instruction_text(data, local_rip)
				instxt = ''.join([x.text for x in tokens])
				inslen = length

			local_ripnext = local_rip + inslen
			remote_ripnext = remote_rip + inslen

			call = instxt.startswith('call ')
			bphere = local_rip in self.breakpoints
			bpnext = local_ripnext in self.breakpoints

			seq = []

			if not call:
				if bphere:
					seq.append((self.adapter.breakpoint_clear, (remote_rip,)))
					seq.append((self.adapter.step_into, ()))
					seq.append((self.adapter.breakpoint_set, (remote_rip,)))
				else:
					seq.append((self.adapter.step_into, ()))
			elif bphere and bpnext:
				seq.append((self.adapter.breakpoint_clear, (remote_rip,)))
				seq.append((self.adapter.go, ()))
				seq.append((self.adapter.breakpoint_set, (remote_rip,)))
			elif bphere and not bpnext:
				seq.append((self.adapter.breakpoint_clear, (remote_rip,)))
				seq.append((self.adapter.breakpoint_set, (remote_ripnext,)))
				seq.append((self.adapter.go, ()))
				seq.append((self.adapter.breakpoint_clear, (remote_ripnext,)))
				seq.append((self.adapter.breakpoint_set, (remote_rip,)))
			elif not bphere and bpnext:
				seq.append((self.adapter.go, ()))
			elif not bphere and not bpnext:
				seq.append((self.adapter.breakpoint_set, (remote_ripnext,)))
				seq.append((self.adapter.go, ()))
				seq.append((self.adapter.breakpoint_clear, (remote_ripnext,)))
			else:
				raise Exception('confused by call, bphere, bpnext state')
			# TODO: Cancel (and raise some exception)
			result = self.exec_adapter_sequence(seq)
			self.memory_dirty()
			return result

		else:
			raise NotImplementedError('step over unimplemented for architecture %s' % self.bv.arch.name)

	def step_return(self):
		if not self.adapter:
			raise Exception('missing adapter')

		if self.bv.arch.name == 'x86_64':
			remote_rip = self.adapter.reg_read('rip')
			local_rip = self.memory_view.remote_addr_to_local(remote_rip)

			# TODO: If we don't have a function loaded, walk the stack
			funcs = self.bv.get_functions_containing(local_rip)
			if len(funcs) != 0:
				mlil = funcs[0].mlil

				bphere = local_rip in self.breakpoints

				# Set a bp on every ret in the function and go
				old_bps = set()
				new_bps = set()
				for insn in mlil.instructions:
					if insn.operation == binaryninja.MediumLevelILOperation.MLIL_RET or insn.operation == binaryninja.MediumLevelILOperation.MLIL_TAILCALL:
						if insn.address in self.breakpoints:
							rets.add(self.memory_view.local_addr_to_remote(insn.address))
						else:
							new_bps.add(self.memory_view.local_addr_to_remote(insn.address))

				seq = []
				if bphere and not local_rip in new_bps and not local_rip in old_bps:
					seq.append((self.adapter.breakpoint_clear, (remote_rip,)))
				for bp in new_bps:
					seq.append((self.adapter.breakpoint_set, (bp,)))
				seq.append((self.adapter.go, ()))
				for bp in new_bps:
					seq.append((self.adapter.breakpoint_clear, (bp,)))
				if bphere and not local_rip in new_bps and not local_rip in old_bps:
					seq.append((self.adapter.breakpoint_set, (remote_rip,)))
				# TODO: Cancel (and raise some exception)
				result = self.exec_adapter_sequence(seq)
				self.memory_dirty()
				return result
			else:
				print("Can't find current function")
				return (None, None)

		else:
			raise NotImplementedError('step over unimplemented for architecture %s' % self.bv.arch.name)

	def breakpoint_set(self, remote_address):
		if not self.adapter:
			raise Exception('missing adapter')

		self.adapter.breakpoint_set(remote_address)
		local_address = self.memory_view.remote_addr_to_local(remote_address)

		# save it
		self.breakpoints[local_address] = True
		#print('breakpoint address=0x%X (remote=0x%X) set' % (local_address, remote_address))
		self.update_highlights()
		if self.ui is not None:
			self.ui.update_breakpoints()

		self.breakpoint_tag_add(local_address)

		return True

	def breakpoint_clear(self, remote_address):
		if not self.adapter:
			raise Exception('missing adapter')

		local_address = self.memory_view.remote_addr_to_local(remote_address)

		# find/remove address tag
		if local_address in self.breakpoints:
			# delete from adapter
			if self.adapter.breakpoint_clear(remote_address) != None:
				print('breakpoint address=0x%X (remote=0x%X) cleared' % (local_address, remote_address))
			else:
				print('ERROR: clearing breakpoint')

			# delete breakpoint tags from all functions containing this address
			self.breakpoint_tag_del([local_address])

			# delete from our list
			del self.breakpoints[local_address]

			self.update_highlights()
			if self.ui is not None:
				self.ui.update_breakpoints()
		else:
			print('ERROR: breakpoint not found in list')

	# execute a sequence of adapter commands, capturing the return of the last
	# blocking call
	def exec_adapter_sequence(self, seq):
		(reason, data) = (None, None)

		for (func, args) in seq:
			if func in [self.adapter.step_into, self.adapter.step_over, self.adapter.go]:
				(reason, data) = func(*args)
				if reason == DebugAdapter.STOP_REASON.PROCESS_EXITED or reason == DebugAdapter.STOP_REASON.BACKEND_DISCONNECTED:
					# Process is dead, stop sequence
					break
			else:
				func(*args)

		return (reason, data)
