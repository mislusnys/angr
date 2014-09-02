from collections import defaultdict

import networkx

import logging
import simuvex
import angr
from .exit_wrapper import SimExitWrapper
from .cfg_base import CFGBase

l = logging.getLogger(name="angr.cfg")

class CFG(CFGBase):
    def __init__(self):
        CFGBase.__init__(self)

    def copy(self):
        new_cfg = CFG()
        new_cfg._cfg = networkx.DiGraph(self._cfg)
        new_cfg._bbl_dict = self._bbl_dict.copy()
        new_cfg._edge_map = self._edge_map.copy()
        new_cfg._loop_back_edges = self._loop_back_edges[::]
        new_cfg._overlapped_loop_headers = self._overlapped_loop_headers[::]
        new_cfg._function_manager = self._function_manager
        return new_cfg

    # Construct the CFG from an angr. binary object
    def construct(self, binary, project, avoid_runs=None):
    	avoid_runs = [ ] if avoid_runs is None else avoid_runs

        # Create the function manager
        self._function_manager = angr.FunctionManager(project, binary)

        # Re-create the DiGraph
        self._cfg = networkx.DiGraph()

        # Traverse all the IRSBs, and put them to a dict
        # It's actually a multi-dict, as each SIRSB might have different states
        # on different call predicates
        self._bbl_dict = {}
        entry_point = binary.entry_point
        l.debug("Entry point = 0x%x", entry_point)

        # Crawl the binary, create CFG and fill all the refs inside project!
        loaded_state = project.initial_state(mode="static")
        entry_point_exit = project.exit_to(addr=entry_point,
                                           state=loaded_state.copy(),
                                           jumpkind="Ijk_boring")
        exit_wrapper = SimExitWrapper(entry_point_exit)
        remaining_exits = [exit_wrapper]
        traced_sim_blocks = defaultdict(set)
        traced_sim_blocks[exit_wrapper.call_stack_suffix()].add(
            entry_point_exit.concretize())

        self._loop_back_edges = []
        self._overlapped_loop_headers = []

        # For each call, we are always getting two exits: an Ijk_Call that
        # stands for the real call exit, and an Ijk_Ret that is a simulated exit
        # for the retn address. There are certain cases that the control flow
        # never returns to the next instruction of a callsite due to
        # imprecision of the concrete execution. So we save those simulated
        # exits here to increase our code coverage. Of course the real retn from
        # that call always precedes those "fake" retns.
        # Tuple --> (Initial state, call_stack, bbl_stack)
        fake_func_retn_exits = {}
        # A dict to log edges and the jumpkind between each basic block
        exit_targets = defaultdict(list)
        # A dict to record all blocks that returns to a specific address
        retn_target_sources = defaultdict(list)
        # Iteratively analyze every exit
        while len(remaining_exits) > 0:
            current_exit_wrapper = remaining_exits.pop()
            current_exit = current_exit_wrapper.sim_exit()
            call_stack_suffix = current_exit_wrapper.call_stack_suffix()
            addr = current_exit.concretize()
            initial_state = current_exit.state

            try:
                sim_run = project.sim_run(current_exit)
            except simuvex.s_irsb.SimIRSBError as ex:
                # It's a tragedy that we came across some instructions that VEX
                # does not support. I'll create a terminating stub there
                l.error("SimIRSBError occurred(%s). Creating a PathTerminator.", ex)
                sim_run = \
                    simuvex.procedures.SimProcedures["stubs"]["PathTerminator"](
                        initial_state, addr=addr)
            except angr.errors.AngrError as ex:
                segment = project.ld.main_bin.in_which_segment(addr)
                l.error("AngrError %s when creating SimRun at 0x%x (segment %s)",
                        ex, addr, segment)
                # We might be on a wrong branch, and is likely to encounter the
                # "No bytes in memory xxx" exception
                # Just ignore it
                sim_run = None

            if sim_run is not None:
                # We will put this block into our dict only if it doesn't exist
                # in our basic block list, aka we haven't traced it in the
                # specified context
                if call_stack_suffix + (addr,) not in self._bbl_dict:
                    # Adding the new sim_run to our dict
                    self._bbl_dict[call_stack_suffix + (addr,)] = sim_run

                    if addr not in avoid_runs:
                        # Generate exits
                        tmp_exits = sim_run.exits()
                    else:
                        tmp_exits = []


                    if len(tmp_exits) == 0:
                        if isinstance(sim_run, \
                            simuvex.procedures.SimProcedures["stubs"]["PathTerminator"]):
                            # If there is no valid exit in this branch and it's not
                            # intentional (e.g. caused by a SimProcedure that does not
                            # do_return) , we should make it
                            # return to its callsite. However, we don't want to use its
                            # state as it might be corrupted. Just create a link in the
                            # exit_targets map.
                            retn_target = current_exit_wrapper.call_stack().get_ret_target()
                            if retn_target is not None:
                                new_call_stack = current_exit_wrapper.call_stack_copy()
                                exit_target_tpl = new_call_stack.stack_suffix() + (retn_target,)
                                exit_targets[call_stack_suffix + (addr,)].append(
                                    (exit_target_tpl, 'Ijk_Ret'))
                        else:
                            # This is intentional. We shall remove all the fake
                            # returns generated before along this path.

                            # Build the tuples that we want to remove from
                            # the dict fake_func_retn_exits
                            tpls_to_remove = []
                            call_stack_copy = current_exit_wrapper.call_stack_copy()
                            while call_stack_copy.get_ret_target() is not None:
                                ret_target = call_stack_copy.get_ret_target()
                                # Remove the current call stack frame
                                call_stack_copy.ret(ret_target)
                                call_stack_suffix = call_stack_copy.stack_suffix()
                                tpl = call_stack_suffix + (ret_target,)
                                tpls_to_remove.append(tpl)
                            # Remove those tuples from the dict
                            for tpl in tpls_to_remove:
                                if tpl in fake_func_retn_exits:
                                    del fake_func_retn_exits[tpl]
                                    l.debug("Removed (%s) from FakeExits dict.", \
                                            ",".join([hex(i) if i is not None else 'None' for i in tpl]))
                else:
                    # Remember to empty it!
                    tmp_exits = []

                # TODO: Fill the mem/code references!

                # If there is a call exit, we shouldn't put the default exit (which
                # is artificial) into the CFG. The exits will be Ijk_Call and
                # Ijk_Ret, and Ijk_Call always goes first
                is_call_exit = False

                # For debugging purpose!
                tmp_exit_status = {}
                for ex in tmp_exits:
                    tmp_exit_status[ex] = ""

                    new_initial_state = ex.state.copy()
                    new_jumpkind = ex.jumpkind

                    if new_jumpkind == "Ijk_Call":
                        is_call_exit = True

                    try:
                        new_addr = ex.concretize()
                    except simuvex.SimValueError:
                        # It cannot be concretized currently. Maybe we could handle
                        # it later, maybe it just cannot be concretized
                        continue

                   # Get the new call stack of target block
                    if new_jumpkind == "Ijk_Call":
                        new_call_stack = current_exit_wrapper.call_stack_copy()
                        # Notice that in ARM, there are some freaking instructions
                        # like
                        # BLEQ <address>
                        # It should give us three exits: Ijk_Call, Ijk_Boring, and
                        # Ijk_Ret. The last exit is simulated.
                        # Notice: We assume the last exit is the simulated one
                        retn_target_addr = tmp_exits[-1].concretize()
                        new_call_stack.call(addr, new_addr, \
                                retn_target=retn_target_addr)
                        self._function_manager.call_to( \
                            function_addr=current_exit_wrapper.current_func_addr(), \
                            from_addr=addr, to_addr=new_addr, \
                            retn_addr=retn_target_addr)
                    elif new_jumpkind == "Ijk_Ret" and not is_call_exit:
                        new_call_stack = current_exit_wrapper.call_stack_copy()
                        new_call_stack.ret(new_addr)
                        self._function_manager.return_from( \
                            function_addr=current_exit_wrapper.current_func_addr(), \
                            from_addr=addr, to_addr=new_addr)
                    else:
                        # Normal control flow transition
                        new_call_stack = current_exit_wrapper.call_stack()
                        self._function_manager.transit_to( \
                            function_addr=current_exit_wrapper.current_func_addr(), \
                            from_addr=addr, \
                            to_addr=new_addr)
                    new_call_stack_suffix = new_call_stack.stack_suffix()

                    new_tpl = new_call_stack_suffix + (new_addr,)

                    # Loop detection
                    if isinstance(sim_run, simuvex.SimIRSB):
                        # Loop detection only applies to SimIRSBs
                        # The most f****** case: An IRSB branches to itself
                        if new_tpl == call_stack_suffix + (addr,):
                            l.debug("%s is branching to itself. That's a loop.", sim_run)
                            self._loop_back_edges.append((sim_run, sim_run))
                        elif new_jumpkind != "Ijk_Call" and new_jumpkind != "Ijk_Ret" and \
                                current_exit_wrapper.bbl_in_stack( \
                                                                new_call_stack_suffix, new_addr):
                            '''
                            There are two cases:
                            # The loop header we found is a single IRSB that doesn't overlap with
                            other IRSBs
                            or
                            # The loop header we found is a subset of the original loop header IRSB,
                            as IRSBa could be inside IRSBb if they don't start at the same address but
                            end at the same address
                            We should take good care of these two cases.
                            ''' #pylint:disable=W0105
                            # First check if this is an overlapped loop header
                            next_irsb = self._bbl_dict[new_tpl]
                            assert next_irsb is not None
                            other_preds = set()
                            for k_tpl, v_lst in exit_targets.items():
                                a = k_tpl[-1]
                                for v_tpl in v_lst:
                                    b = v_tpl[-2] # The last item is the jumpkind :)
                                    if b == next_irsb.addr and a != sim_run.addr:
                                        other_preds.add(self._bbl_dict[k_tpl])
                            if len(other_preds) > 0:
                                is_overlapping = False
                                for p in other_preds:
                                    if isinstance(p, simuvex.SimIRSB):
                                        if p.addr + p.irsb.size() == sim_run.addr + sim_run.irsb.size():
                                            # Overlapping!
                                            is_overlapping = True
                                            break
                                if is_overlapping:
                                    # Case 2, it's overlapped with another loop header
                                    # Pending. We should remove all exits from sim_run
                                    self._overlapped_loop_headers.append(sim_run)
                                    l.debug("Found an overlapped loop header %s", sim_run)
                                else:
                                    # Case 1
                                    self._loop_back_edges.append((sim_run, next_irsb))
                                    l.debug("Found a loop, back edge %s --> %s", sim_run, next_irsb)
                            else:
                                # Case 1, it's not over lapping with any other things
                                self._loop_back_edges.append((sim_run, next_irsb))
                                l.debug("Found a loop, back edge %s --> %s", sim_run, next_irsb)

                    # Generate the new BBL stack of target block
                    if new_jumpkind == "Ijk_Call":
                        new_bbl_stack = current_exit_wrapper.bbl_stack_copy()
                        new_bbl_stack.call(new_call_stack_suffix)
                        new_bbl_stack.push(new_call_stack_suffix, new_addr)
                    elif new_jumpkind == "Ijk_Ret" and not is_call_exit:
                        new_bbl_stack = current_exit_wrapper.bbl_stack_copy()
                        new_bbl_stack.ret(call_stack_suffix)
                    else:
                        new_bbl_stack = current_exit_wrapper.bbl_stack_copy()
                        new_bbl_stack.push(new_call_stack_suffix, new_addr)

                    # Generate new exits
                    if new_jumpkind == "Ijk_Ret" and not is_call_exit:
                        # This is the real retn exit
                        # Remember this retn!
                        retn_target_sources[new_addr].append(call_stack_suffix + (addr,))
                        # Check if this retn is inside our fake_func_retn_exits set
                        if new_tpl in fake_func_retn_exits:
                            del fake_func_retn_exits[new_tpl]

                    if new_jumpkind == "Ijk_Ret" and is_call_exit:
                        # This is the default "fake" retn that generated at each
                        # call. Save them first, but don't process them right
                        # away
                        fake_func_retn_exits[new_tpl] = \
                            (new_initial_state, new_call_stack, new_bbl_stack)
                        tmp_exit_status[ex] = "Appended to fake_func_retn_exits"
                    elif new_addr not in traced_sim_blocks[new_call_stack_suffix]:
                        traced_sim_blocks[new_call_stack_suffix].add(new_addr)
                        new_exit = project.exit_to(addr=new_addr,
                                                        state=new_initial_state,
                                                        jumpkind=ex.jumpkind)
                        new_exit_wrapper = SimExitWrapper(new_exit, new_call_stack, new_bbl_stack)
                        remaining_exits.append(new_exit_wrapper)

                        tmp_exit_status[ex] = "Appended"
                    elif new_addr in traced_sim_blocks[new_call_stack_suffix] \
                        and new_jumpkind == "Ijk_Ret":
                        # This is a corner case for the f****** ARM instruction
                        # like
                        # BLEQ <address>
                        # If we have analyzed the boring exit before returning
                        # from that called address, we will lose the link between
                        # the last block of the function being called and the
                        # basic block it returns to.
                        # We cannot reanalyze the basic block as we are not
                        # flow-sensitive, but we can still record the connection
                        # and make for it afterwards.
                        pass

                    if not is_call_exit or new_jumpkind != "Ijk_Ret":
                        exit_targets[call_stack_suffix + (addr,)].append((new_tpl, new_jumpkind))
                    else:
                        # This is the fake return!
                        exit_targets[call_stack_suffix + (addr,)].append((new_tpl, "Ijk_FakeRet"))

                # debugging!
                l.debug("Basic block %s %s", sim_run, "->".join([hex(i) for i in call_stack_suffix if i is not None]))
                l.debug("(Function %s)" % project.ld.main_bin.function_name(int(sim_run.id_str,16)))
                l.debug("|    Has simulated retn: %s", is_call_exit)
                for ex in tmp_exits:
                    if is_call_exit and ex.jumpkind == "Ijk_Ret":
                        exit_type_str = "Simulated Ret"
                    else:
                        exit_type_str = "-"
                    try:
                        l.debug("|    target: 0x%08x %s [%s] %s", ex.concretize(), tmp_exit_status[ex], exit_type_str, ex.jumpkind)
                    except simuvex.SimValueError:
                        l.debug("|    target cannot be concretized. %s [%s] %s", tmp_exit_status[ex], exit_type_str, ex.jumpkind)
                l.debug("len(remaining_exits) = %d, len(fake_func_retn_exits) = %d", len(remaining_exits), len(fake_func_retn_exits))

            while len(remaining_exits) == 0 and len(fake_func_retn_exits) > 0:
                # We don't have any exits remaining. Let's pop a fake exit to
                # process
                fake_exit_tuple = fake_func_retn_exits.keys()[0]
                fake_exit_state, fake_exit_call_stack, fake_exit_bbl_stack = \
                    fake_func_retn_exits.pop(fake_exit_tuple)
                fake_exit_addr = fake_exit_tuple[len(fake_exit_tuple) - 1]
                # Let's check whether this address has been traced before.
                targets = filter(lambda r: r == fake_exit_tuple,
                                 exit_targets)
                if len(targets) > 0:
                    # That block has been traced before. Let's forget about it
                    l.debug("Target 0x%08x has been traced before." + \
                            "Trying the next one...", fake_exit_addr)
                    continue
                new_exit = project.exit_to(addr=fake_exit_addr,
                    state=fake_exit_state,
                    jumpkind="Ijk_Ret")
                new_exit_wrapper = SimExitWrapper(new_exit, fake_exit_call_stack, fake_exit_bbl_stack)
                remaining_exits.append(new_exit_wrapper)
                l.debug("Tracing a missing retn exit 0x%08x, %s", fake_exit_addr, "->".join([hex(i) for i in fake_exit_tuple if i is not None]))
                break

        # Save the exit_targets dict
        self._edge_map = exit_targets

        # The corner case: add a node to the graph if there is only one block
        if len(self._bbl_dict) == 1:
            self._cfg.add_node(self._bbl_dict[self._bbl_dict.keys()[0]])

        # Adding edges
        for tpl, targets in exit_targets.items():
            basic_block = self._bbl_dict[tpl] # Cannot fail :)
            for ex, jumpkind in targets:
                if ex in self._bbl_dict:
                    target_bbl = self._bbl_dict[ex]
                    self._cfg.add_edge(basic_block, target_bbl, jumpkind=jumpkind)

                    # Add edges for possibly missing returns
                    if basic_block.addr in retn_target_sources:
                        for src_irsb_key in \
                                retn_target_sources[basic_block.addr]:
                            self._cfg.add_edge(self._bbl_dict[src_irsb_key],
                                               basic_block, jumpkind="Ijk_Ret")
                else:
                    # Debugging output
                    if ex[0] is None:
                        s = "([None -> None] 0x%08x)" % (ex[2])
                    elif ex[1] is None:
                        s = "([None -> 0x%x] 0x%08x)" % (ex[1], ex[2])
                    else:
                        s = "([0x%x -> 0x%x] 0x%08x)" % (ex[0], ex[1], ex[2])
                    l.warning("Key %s does not exist.", s)

    def _get_block_addr(self, b): #pylint:disable=R0201
        if isinstance(b, simuvex.SimIRSB):
            return b.first_imark.addr
        elif isinstance(b, simuvex.SimProcedure):
            return b.addr
        else:
            raise Exception("Unsupported block type %s" % type(b))

    def remove_cycles(self):
        l.debug("Removing cycles...")
        l.debug("There are %d loop back edges.", len(self._loop_back_edges))
        l.debug("And there are %d overlapping loop headers.", len(self._overlapped_loop_headers))
        # First break all detected loops
        for b1, b2 in self._loop_back_edges:
            if self._cfg.has_edge(b1, b2):
                l.debug("Removing loop back edge %s -> %s", b1, b2)
                self._cfg.remove_edge(b1, b2)
        # Then remove all outedges from overlapped loop headers
        for b in self._overlapped_loop_headers:
            successors = self._cfg.successors(b)
            for succ in successors:
                self._cfg.remove_edge(b, succ)
                l.debug("Removing partial loop header edge %s -> %s", b, succ)
        return
        # DFS in the graph, assign an index for each of the block
        indices = {} #pylint:disable=W0101
        counter = 0
        for node in networkx.dfs_preorder_nodes(self._cfg):
            indices[node] = counter
            counter += 1
        # Find all strongly connected components
        scc_list = networkx.strongly_connected_components(self._cfg)
        for scc in [i for i in scc_list if len(i) > 1]:
            # We break the edge between the block that has least index and
            # its predecessor
            # There should be only one predecessor in the graph though
            least_index = indices[scc[0]]
            least_index_node = scc[0]
            for n in scc:
                if indices[n] < least_index:
                    least_index = indices[n]
                    least_index_node = n
            l.debug("Starting node: %s", least_index_node)
            for pred in self._cfg.predecessors(least_index_node):
                if pred in scc:
                    l.debug("Breaking edge between %s and %s", pred, least_index_node)
                    self._cfg.remove_edge(pred, least_index_node)

    #def output(self):
    #    print "Edges:"
    #    for edge in self.cfg.edges():
    #        x = edge[0]
    #        y = edge[1]
    #        print "(%x -> %x)" % (self._get_block_addr(x),
    #                              self._get_block_addr(y))